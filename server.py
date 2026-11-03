"""
Asset Management System server.

Each connection carries one request and one reply:

    {"action": "LOGIN", "username": ..., "password": ...} -> replies with a token
    {"action": "REGISTER", "asset": {...}}   -> validates and stores the asset
    {"action": "LIST"}                       -> replies with every stored asset

Replies always contain "status". Failures look like this:

    {"status": "ERROR", "code": "DUPLICATE_ID", "detail": "..."}

Design goals of this file:
  - Concurrency: each client has its own thread, so a slow client does not
    delay the others.
  - Fault isolation: nothing a client sends, and no failure while handling a
    client, can stop the server. In a distributed system clients fail
    independently, so one client's failure must stay that client's problem.
  - Persistence: see store.py.
"""

import datetime   # the server stamps each new asset with today's date
import logging    # records errors without stopping the server
import os         # used to find the assets file next to this script
import socket     # provides the networking building blocks (sockets)
import sys        # sys.exit() reports startup failures to the operating system
import threading  # gives each connected client its own thread
from typing import Optional

# Import the shared constants/helpers used by every part of the prototype.
from auth import USERS_FILE, SessionManager, UserStore
from common import (
    BAD_REQUEST,
    HOST,
    INTERNAL,
    PORT,
    TOO_LARGE,
    UNAUTHORISED,
    MessageTooLargeError,
    RequestError,
    receive_message,
    send_message,
)
from idempotency import RequestCache
from store import AssetStore

# Stored next to this script, so the server finds the same data whichever
# folder it is started from.
ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets.json")

# The fields a client must supply when registering an asset.
REQUIRED_ASSET_FIELDS = ("asset_id", "description", "category", "location", "status")

# How often the accept loop wakes up. See the comment in run_server().
ACCEPT_POLL_SECONDS = 1.0

# How long a client may stay silent before the server gives up on it. This is
# longer than the client's own timeout, so that a healthy client always gives up
# and retries first.
CLIENT_TIMEOUT_SECONDS = 10

# Actions that change data. Repeats of these must not be carried out twice.
STATE_CHANGING_ACTIONS = ("REGISTER",)

# A uuid4 is 36 characters. The limit stops a client filling the request cache
# with very long keys.
MAX_REQUEST_ID_LENGTH = 100

logger = logging.getLogger("asset_server")


class ServerContext:
    """The shared state each request needs, passed around as one object."""

    def __init__(self, store: AssetStore, request_cache: RequestCache,
                 users: UserStore, sessions: SessionManager) -> None:
        self.store = store
        self.request_cache = request_cache
        self.users = users
        self.sessions = sessions


def error_reply(code: str, detail: str) -> dict:
    """Build an ERROR reply in the one format every client relies on."""
    return {"status": "ERROR", "code": code, "detail": detail}


def validate_new_asset(asset: object) -> dict:
    """Check a client-supplied asset and return a clean copy ready to store.

    Validation happens on the server even though clients may check too.
    WHY: the server cannot trust clients. Anyone can write their own program
    that speaks this protocol, so a check made only in a client is a convenience
    for its user, not a guarantee about the data.
    """
    if not isinstance(asset, dict):
        raise RequestError(BAD_REQUEST, "'asset' must be an object")

    clean = {}
    for field in REQUIRED_ASSET_FIELDS:
        if field not in asset:
            raise RequestError(BAD_REQUEST, f"Missing required field: {field}")

        value = asset[field]
        if not isinstance(value, str):
            raise RequestError(BAD_REQUEST, f"Field must be a string: {field}")

        value = value.strip()
        if not value:
            raise RequestError(BAD_REQUEST, f"Field must not be empty: {field}")

        clean[field] = value

    # The date comes from this machine, and any date sent by the client is
    # ignored. WHY: there is no shared clock in a distributed system. Client
    # clocks can be wrong or in other time zones, so letting one authority
    # assign the date keeps records consistent with each other.
    #
    # Building `clean` from a fixed field list also means unexpected fields
    # sent by a client are dropped rather than stored.
    clean["date_registered"] = datetime.date.today().isoformat()
    return clean


def _execute(request: dict, context: ServerContext) -> dict:
    """Carry out the action in a request and return the reply."""
    action = request.get("action")

    try:
        if action == "REGISTER":
            stored = context.store.register(validate_new_asset(request.get("asset")))

            # Echo back what was stored, because the server may have changed it
            # (trimmed spaces, added the date). The client should show the
            # record that really exists, not the one it hoped to create.
            return {"status": "OK", "stored": stored}

        if action == "LIST":
            return {"status": "OK", "assets": context.store.list_all()}

        return error_reply(BAD_REQUEST, f"Unknown action: {action!r}")

    except RequestError as exc:
        # An expected rejection: the server is healthy and the client gets told
        # why. Unexpected exceptions are deliberately not caught here, and
        # are handled by handle_client() instead.
        return error_reply(exc.code, exc.detail)


def _login(request: dict, context: ServerContext) -> dict:
    """Check a username and password, and start a session if they are correct."""
    username = request.get("username")
    password = request.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return error_reply(BAD_REQUEST, "'username' and 'password' must be strings")

    role = context.users.authenticate(username, password)
    if role is None:
        # The same message whichever part was wrong, so an attacker cannot use
        # this reply to find out which usernames exist.
        return error_reply(UNAUTHORISED, "Invalid username or password")

    token = context.sessions.create(username, role)
    return {"status": "OK", "token": token, "username": username, "role": role}


def process_request(request: dict, context: ServerContext) -> dict:
    """Carry out one decoded request and return the reply.

    This function does no networking, so the server's rules can be unit tested
    without opening a socket.
    """
    if request.get("action") == "LOGIN":
        return _login(request, context)

    # Read-only actions are harmless to repeat, and caching their potentially
    # large replies would waste memory, so only state changes use the cache.
    if request.get("action") not in STATE_CHANGING_ACTIONS:
        return _execute(request, context)

    # Required rather than optional: without an ID, the server could not keep
    # its promise that a retried request is not carried out twice.
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= MAX_REQUEST_ID_LENGTH:
        return error_reply(
            BAD_REQUEST,
            f"'request_id' must be a string of 1 to {MAX_REQUEST_ID_LENGTH} characters",
        )

    return context.request_cache.run_once(request_id, lambda: _execute(request, context))


def _read_and_process(conn: socket.socket, addr: tuple, context: ServerContext) -> Optional[dict]:
    """Read one request and return the reply, or None if no reply can be sent."""
    try:
        request = receive_message(conn)
    except MessageTooLargeError as exc:
        return error_reply(TOO_LARGE, str(exc))
    except ValueError as exc:
        # Covers invalid UTF-8, invalid JSON and JSON that is not an object.
        # The connection itself still works, so the client can be told.
        return error_reply(BAD_REQUEST, f"Invalid message: {exc}")
    except socket.timeout:
        # An idle client. Dropping it frees this thread for good. The check must
        # come before OSError below, because socket.timeout is a kind of OSError.
        logger.warning("Client %s timed out before sending a full request", addr)
        return None
    except OSError as exc:
        # The connection failed, for example the client crashed or disconnected
        # part-way through. Nobody is left to read a reply, and attempting one
        # would simply raise another error, which is what used to crash the
        # server. All that remains is to record it.
        logger.warning("Client %s disconnected before sending a request: %s", addr, exc)
        return None

    return process_request(request, context)


def _send_reply(conn: socket.socket, addr: tuple, reply: dict) -> None:
    """Send a reply, accepting that the client may no longer be there."""
    try:
        try:
            send_message(conn, reply)
        except MessageTooLargeError as exc:
            # This happens if a reply, such as a very long LIST, exceeds the cap.
            send_message(conn, error_reply(TOO_LARGE, str(exc)))
    except OSError as exc:
        # The work is already done, and only the confirmation failed. This is
        # the classic distributed systems ambiguity: the client cannot tell
        # whether its request succeeded.
        logger.warning("Could not send reply to %s: %s", addr, exc)


def handle_client(conn: socket.socket, addr: tuple, context: ServerContext) -> None:
    """Serve one client from start to finish. Runs on that client's own thread.

    This is the fault isolation boundary: no exception may escape it. On a
    separate thread an escaped exception would not stop the server, but it would
    leave no useful record and the client no reply, so failures are handled here.
    """
    try:
        # `with conn:` guarantees the connection is closed automatically once
        # this block finishes, even if an error happens inside it.
        with conn:
            # WHY: threads stop an idle client from blocking other clients, but
            # without a timeout it would still hold a thread and a socket forever,
            # and enough of them would exhaust the server.
            #
            # This limits each network operation, not the whole conversation, so
            # a client sending one byte at a time can still hold a thread for
            # longer. That is a known limitation.
            conn.settimeout(CLIENT_TIMEOUT_SECONDS)

            try:
                reply = _read_and_process(conn, addr, context)
            except Exception:
                # Catching everything is deliberate at this one boundary, as
                # this is a bug or a failure such as a full disk. Log the full
                # traceback for the operator, but tell the client only that it
                # failed: internal details can reveal information to attackers.
                logger.exception("Unexpected error handling client %s", addr)
                reply = error_reply(INTERNAL, "The server could not complete the request")

            if reply is not None:
                _send_reply(conn, addr, reply)
    except Exception:
        # Last resort, for example if closing the connection fails.
        logger.exception("Unexpected error cleaning up client %s", addr)


def create_listening_socket(host: str, port: int) -> socket.socket:
    """Create the TCP/IPv4 socket that waits for new connections."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        # The goal is to restart quickly without the port being "in use", while
        # still getting an error if another server really is running. The
        # option for this differs by platform: on Windows, SO_REUSEADDR would
        # let two servers share a port without any error, and clients would
        # reach one or the other unpredictably. SO_EXCLUSIVEADDRUSE prevents
        # that. Elsewhere, SO_REUSEADDR does not allow two active listeners.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        server_sock.bind((host, port))
        server_sock.listen()
    except OSError:
        server_sock.close()
        raise

    return server_sock


def run_server(server_sock: socket.socket, context: ServerContext) -> None:
    """Accept clients until interrupted, giving each one its own thread."""
    # accept() normally waits forever, and on Windows a thread stuck in it does
    # not notice Ctrl+C. Waking up regularly lets the interrupt be delivered.
    server_sock.settimeout(ACCEPT_POLL_SECONDS)

    while True:
        try:
            conn, addr = server_sock.accept()
        except socket.timeout:
            continue  # nobody connected during this interval; just wait again
        except OSError:
            # A connection can fail between arriving and being accepted. That
            # affects that client only, so keep serving everyone else.
            logger.exception("Failed to accept a connection")
            continue

        logger.info("Connection from %s", addr)

        # WHY threads: a server handling one client at a time would stall
        # everyone whenever one client was slow. Handling is mostly waiting
        # on the network, and Python threads run concurrently while waiting.
        #
        # daemon=True means an unfinished client thread does not stop the
        # server process from exiting when the operator asks it to stop.
        thread = threading.Thread(target=handle_client, args=(conn, addr, context), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # The operating system refused another thread (too many clients).
            # Turn away this client rather than stopping the whole server.
            logger.exception("Could not start a thread for client %s", addr)
            conn.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        # threadName in the log makes the concurrent handling visible.
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )

    # Startup failures end the program, because the server cannot work without
    # its data or its port. The operator gets a clear message, not a traceback.
    try:
        store = AssetStore(ASSETS_FILE)
    except (OSError, ValueError) as exc:
        logger.error("Could not load %s: %s", ASSETS_FILE, exc)
        logger.error("Fix or move that file, then start the server again.")
        sys.exit(1)

    try:
        users = UserStore(USERS_FILE)
    except (OSError, ValueError) as exc:
        logger.error("Could not load user accounts: %s", exc)
        logger.error("Create one with: python create_user.py <username> admin")
        sys.exit(1)

    try:
        server_sock = create_listening_socket(HOST, PORT)
    except OSError as exc:
        logger.error("Could not listen on %s:%s: %s", HOST, PORT, exc)
        logger.error("Another copy of the server may already be running.")
        sys.exit(1)

    with server_sock:
        logger.info("Asset Management server listening on %s:%s", HOST, PORT)
        logger.info("Press Ctrl+C to stop.")
        try:
            run_server(server_sock, ServerContext(store, RequestCache(), users, SessionManager()))
        except KeyboardInterrupt:
            logger.info("Server stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

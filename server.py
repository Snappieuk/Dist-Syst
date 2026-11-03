"""
Asset Management System server.

Listens for connections and understands two request actions:

    {"action": "REGISTER", "asset": {...}}   -> stores the asset, replies with a status
    {"action": "LIST"}                       -> replies with every stored asset

Each client has its own thread, so a slow client does not delay the others.
Assets are saved to a file (see store.py).
"""

import logging    # records errors without stopping the server
import os         # used to find the assets file next to this script
import socket     # provides the networking building blocks (sockets)
import threading  # gives each connected client its own thread

# Import the shared constants/helpers used by every part of the prototype.
from common import (
    HOST,
    PORT,
    RequestError,
    receive_message,
    send_message,
)
from store import AssetStore

# Stored next to this script, so the server finds the same data whichever
# folder it is started from.
ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets.json")

# How often the accept loop wakes up. See the comment in run_server().
ACCEPT_POLL_SECONDS = 1.0

logger = logging.getLogger("asset_server")


def handle_client(conn: socket.socket, addr: tuple, store: AssetStore) -> None:
    # `with conn:` guarantees the connection is closed automatically once
    # this block finishes, even if an error happens inside it.
    with conn:
        try:
            # Wait for, and decode, one complete message from this client.
            request = receive_message(conn)
        except (ConnectionError, ValueError) as exc:
            # ValueError covers JSON that doesn't parse; ConnectionError
            # covers a client that disconnects mid-message.
            send_message(conn, {"status": "ERROR", "detail": str(exc)})
            return  # nothing more we can do with this connection

        # Every request must say what it wants done via an "action" field.
        action = request.get("action")

        if action == "REGISTER":
            # Pull the asset details out of the request.
            asset = request.get("asset")

            # TODO (student): validate `asset` properly before storing it
            # (e.g. required fields present, sensible types) and decide what
            # should happen if it is invalid.
            try:
                stored = store.register(asset)  # saves it to the assets file too
            except RequestError as exc:
                send_message(conn, {"status": "ERROR", "code": exc.code, "detail": exc.detail})
                return

            # Tell the client the registration succeeded, and echo back
            # what was stored so it can be confirmed on screen.
            send_message(conn, {"status": "OK", "stored": stored})

        elif action == "LIST":
            # Send back the full current list of assets as-is.
            send_message(conn, {"status": "OK", "assets": store.list_all()})

        else:
            # Any action we don't recognise is treated as an error.
            send_message(conn, {"status": "ERROR", "detail": f"Unknown action: {action!r}"})


def run_server(server_sock: socket.socket, store: AssetStore) -> None:
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
        thread = threading.Thread(target=handle_client, args=(conn, addr, store), daemon=True)
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

    store = AssetStore(ASSETS_FILE)

    # Create a TCP/IPv4 socket. `with` closes it automatically on exit.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        # Allow the port to be reused immediately after the server stops,
        # instead of Windows/Linux holding on to it for a short while.
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind the socket to our chosen host/port, then start listening
        # for incoming connections (this does not accept any yet).
        server_sock.bind((HOST, PORT))
        server_sock.listen()

        logger.info("Asset Management server listening on %s:%s", HOST, PORT)
        logger.info("Press Ctrl+C to stop.")
        try:
            run_server(server_sock, store)
        except KeyboardInterrupt:
            logger.info("Server stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

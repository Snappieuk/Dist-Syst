"""
Asset Management System server.

    python server.py

Understands these actions:
    {"action": "LOGIN", "username": "...", "password": "..."}  -> a session token
    {"action": "REGISTER", "request_id": "...", "asset": {...}}
    {"action": "LIST", "query": "...", "filters": {"location": "..."}, "page": 1, "page_size": 20}
    {"action": "EDIT", "request_id": "...", "asset_id": "...", "expected_version": 1,
     "changes": {"description": "...", "category": "...", "location": "...", "status": "..."}}
    {"action": "DELETE", "request_id": "...", "asset_id": "...", "expected_version": 1}

Every request must include "protocol_version", every request except LOGIN must
include the session "token", and every change must include a unique "request_id".
Only users with the admin role may DELETE. Every reply has "status": "OK" or "status": "ERROR" with a
stable "code", a "message" and a "context".
"""

import datetime     # creates the server's timestamps
import json         # the data file is JSON
import logging      # records errors and events with timestamps
import os           # saves the data file safely
import re           # checks the format of asset IDs
import socket       # provides the networking building blocks (sockets)
import threading    # one thread per client, and a lock for the shared data

import common
import users
from common import (
    ASSET_FIELDS,
    BAD_REQUEST,
    CONFLICT,
    DUPLICATE_ASSET,
    EDITABLE_FIELDS,
    FORBIDDEN,
    INTERNAL,
    NOT_FOUND,
    PROTOCOL_VERSION,
    STATUSES,
    TOO_LARGE,
    UNAUTHENTICATED,
    UNKNOWN_ACTION,
    UNSUPPORTED_VERSION,
    MessageTooLargeError,
    error_reply,
    receive_message,
    send_message,
)

SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)

# An asset ID is 1 to 32 letters, digits, dashes or underscores, starting with a
# letter or digit, e.g. AST-0001.
ASSET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")

# The longest text accepted in any field.
MAX_FIELD_LENGTH = 200

# How many recent request IDs are remembered, so that retries are recognised. Old ones
# are forgotten so the data file can't grow forever; a retry comes within seconds.
MAX_REMEMBERED_REQUESTS = 1000

# The most assets one page of a LIST reply may hold.
MAX_PAGE_SIZE = 100

# Actions that change the data, and so must carry a request_id.
CHANGE_ACTIONS = ("REGISTER", "EDIT", "DELETE")

# The roles allowed to perform each action. Anything not listed is open to every
# logged-in user.
ALLOWED_ROLES = {"DELETE": ("admin",)}

logger = logging.getLogger("asset_server")


class InvalidRequest(ValueError):
    """A request breaks a rule. Carries the error code and the field concerned."""

    def __init__(self, message: str, code: str = BAD_REQUEST, **context) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


def utc_now() -> str:
    """The server's current time, in the one format used for every timestamp:
    UTC ISO 8601 to the second, e.g. 2026-11-05T21:14:03Z. Clients' clocks may be
    wrong or in other time zones, so timestamps are only ever made here."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_text(value: object, field: str) -> str:
    """Return a field's text without surrounding spaces, or raise InvalidRequest."""
    if value is None:
        raise InvalidRequest(f"{field} is required", field=field)
    if not isinstance(value, str):
        raise InvalidRequest(f"{field} must be text", field=field)
    value = value.strip()
    if not value:
        raise InvalidRequest(f"{field} must not be empty", field=field)
    if len(value) > MAX_FIELD_LENGTH:
        raise InvalidRequest(f"{field} must be at most {MAX_FIELD_LENGTH} characters", field=field)
    return value


def check_status(value: object) -> str:
    """Return the standard spelling of an allowed status, or raise InvalidRequest."""
    text = check_text(value, "status")
    for status in STATUSES:
        if text.lower() == status.lower():
            return status
    raise InvalidRequest(f"status must be one of: {', '.join(STATUSES)}", field="status", allowed=list(STATUSES))


def check_version(value: object) -> int:
    """Return the expected_version a client sent, or raise InvalidRequest."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest("expected_version must be a whole number of at least 1", field="expected_version")
    return value


def check_request_id(value: object) -> str:
    """Return a change's request_id, or raise InvalidRequest."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise InvalidRequest("Every change needs a request_id of 1-64 characters", field="request_id")
    return value


def fingerprint(request: dict) -> str:
    """What a request asks for, ignoring the parts that may differ between retries."""
    ignored = ("protocol_version", "request_id", "token")
    return json.dumps({key: value for key, value in request.items() if key not in ignored}, sort_keys=True)


def validate_asset(asset: object) -> dict:
    """Return a clean copy of a client's asset, or raise InvalidRequest.

    This happens on the server even if a client checks too, because anyone can write
    a program that speaks this protocol. Fields the client is not allowed to set,
    such as timestamps, are dropped.
    """
    if not isinstance(asset, dict):
        raise InvalidRequest("asset must be an object", field="asset")
    clean = {field: check_text(asset.get(field), field) for field in ASSET_FIELDS}
    if not ASSET_ID_PATTERN.fullmatch(clean["asset_id"]):
        raise InvalidRequest(
            "asset_id must be 1-32 letters, digits, dashes or underscores, starting with a letter or digit",
            field="asset_id",
        )
    clean["status"] = check_status(clean["status"])
    return clean


class AssetServer:
    """One asset server: its data and the rules for changing it."""

    def __init__(self, config: dict, role: str = "primary") -> None:
        self.config = config
        self.role = role
        self.address = (config["servers"][role]["host"], config["servers"][role]["port"])
        self.max_bytes = config["max_message_bytes"]
        self.timeout = config["server_timeout_seconds"]
        self.data_file = common.config_path(config, config["data_files"][role])
        self.session_seconds = config["session_seconds"]
        self.secret = users.read_secret(config)  # signs and checks session tokens

        # Every client has its own thread, so two requests can arrive at the same
        # moment. The lock makes each read or change of the data one indivisible
        # step, so they cannot interfere with each other.
        self.lock = threading.Lock()

        self.assets = {}    # every asset, by asset_id, including soft-deleted ones
        self.requests = {}  # recent request IDs: {request_id: {"fingerprint", "reply"}}
        self.load()

    # ---- Storage --------------------------------------------------------------

    def load(self) -> None:
        """Read the data file. It starts empty if there is no file yet.

        A damaged file stops the server rather than starting empty, because the next
        save would overwrite whatever could still be recovered.
        """
        try:
            with open(self.data_file, encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return
        if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            raise ValueError(f"{self.data_file} does not contain valid asset data")
        self.assets = {asset["asset_id"]: asset for asset in data["assets"]}
        self.requests = data.get("requests", {})

    def save(self) -> None:
        """Write all the data to disk. The caller must hold the lock.

        Writing straight over the real file would empty it first, so a crash part-way
        through would lose everything. Instead the data goes into a temporary file,
        which is forced onto the disk, then os.replace() swaps it into place in one
        atomic step. The file is always a complete old or a complete new version.
        """
        temp_path = self.data_file + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump({"assets": list(self.assets.values()), "requests": self.requests}, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, self.data_file)

    def commit(self, undo) -> None:
        """Save a change just made in memory. The caller must hold the lock.

        If the save fails, `undo` puts memory back as it was, so memory and disk still
        agree and the client, which will be told this failed, never sees the change.
        """
        try:
            self.save()
        except OSError:
            undo()
            raise

    # ---- Requests -------------------------------------------------------------

    def process(self, request: dict) -> dict:
        """Carry out one decoded request and return the reply. No networking happens
        here, so the rules can be tested without a socket."""
        version = request.get("protocol_version")
        if version not in SUPPORTED_VERSIONS:
            return error_reply(
                UNSUPPORTED_VERSION,
                f"Protocol version {version!r} is not supported",
                supported_versions=list(SUPPORTED_VERSIONS),
            )

        action = request.get("action")
        handlers = {
            "REGISTER": self.register,
            "EDIT": self.edit,
            "DELETE": self.delete,
            "LIST": self.list_assets,
        }
        if action != "LOGIN" and action not in handlers:
            return error_reply(UNKNOWN_ACTION, f"Unknown action: {action!r}", action=action)
        try:
            if action == "LOGIN":
                return self.login(request)
            user = self.authorise(request)
            handler = handlers[action]
            if action in CHANGE_ACTIONS:
                return self.change(request, handler, user)
            return handler(request, user)
        except InvalidRequest as exc:
            return error_reply(exc.code, str(exc), **exc.context)

    # ---- Accounts -------------------------------------------------------------

    def login(self, request: dict) -> dict:
        """Check a username and password, and issue a time-limited session token.

        An unknown username gets the same reply, after the same slow hashing, as a wrong
        password, so neither the reply nor its timing reveals which usernames exist.
        """
        username = request.get("username")
        password = request.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise InvalidRequest("username and password must be text", field="username")
        username = username.strip().lower()
        record = users.load_users(self.config).get(username)
        if record is None:
            users.hash_password(password)  # same work as a real check
            matched = False
        else:
            matched = users.password_matches(record, password)
        if not matched:
            logger.warning("Failed login for %r", username)
            raise InvalidRequest("Wrong username or password", code=UNAUTHENTICATED)
        token, expires = users.issue_token(username, self.secret, self.session_seconds)
        logger.info("%s logged in", username)
        return {
            "status": "OK",
            "token": token,
            "username": username,
            "role": record["role"],
            "expires_at": datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def authorise(self, request: dict) -> dict:
        """Return the user making a request, or raise InvalidRequest.

        The token proves who the user is; their role is looked up now rather than read
        from the token, so changing or removing an account takes effect at once.
        """
        try:
            username = users.read_token(request.get("token"), self.secret)
        except users.TokenError as exc:
            raise InvalidRequest(str(exc), code=UNAUTHENTICATED) from exc
        record = users.load_users(self.config).get(username)
        if record is None:
            raise InvalidRequest("This account no longer exists. Please log in again.", code=UNAUTHENTICATED)
        user = {"username": username, "role": record["role"]}

        allowed = ALLOWED_ROLES.get(request.get("action"))
        if allowed and user["role"] not in allowed:
            logger.warning("%s (%s) was refused %s", username, user["role"], request.get("action"))
            raise InvalidRequest(
                f"Only {' or '.join(allowed)} users may {request.get('action')}",
                code=FORBIDDEN,
                role=user["role"],
                allowed_roles=list(allowed),
            )
        return user

    # ---- Changes --------------------------------------------------------------

    def change(self, request: dict, handler, user: dict) -> dict:
        """Apply a change exactly once, however many times the request is sent.

        The handler makes the change in memory and returns its reply and a way to undo
        it. The request ID and reply are remembered, then both are saved together, so
        a retry after a lost reply gets the original reply and the change isn't made
        twice. Everything happens under the lock, so two copies of one request
        arriving at once can't both be applied.
        """
        request_id = check_request_id(request.get("request_id"))
        with self.lock:
            previous = self.requests.get(request_id)
            if previous is not None:
                if previous["fingerprint"] != fingerprint(request):
                    raise InvalidRequest("This request_id was already used for a different request", field="request_id")
                return previous["reply"]

            reply, undo = handler(request, user)

            def undo_all() -> None:
                undo()
                del self.requests[request_id]

            self.requests[request_id] = {"fingerprint": fingerprint(request), "reply": reply}
            self.commit(undo_all)
            while len(self.requests) > MAX_REMEMBERED_REQUESTS:
                del self.requests[next(iter(self.requests))]  # forget the oldest
        return reply

    def current_asset(self, asset_id: object, expected_version: object) -> dict:
        """Return the asset a change applies to. The caller must hold the lock.

        The change must name the version the client last saw. If someone else has
        changed the asset since, it is refused with CONFLICT rather than silently
        overwriting a change its author never saw (optimistic concurrency control).
        """
        asset_id = check_text(asset_id, "asset_id")
        expected_version = check_version(expected_version)
        asset = self.assets.get(asset_id)
        if asset is None or asset.get("deleted"):
            raise InvalidRequest(f"No asset has the ID {asset_id}", code=NOT_FOUND, asset_id=asset_id)
        if asset["version"] != expected_version:
            raise InvalidRequest(
                f"{asset_id} was changed by someone else. Refresh and try again.",
                code=CONFLICT,
                asset_id=asset_id,
                expected_version=expected_version,
                current_version=asset["version"],
            )
        return asset

    def register(self, request: dict, user: dict) -> tuple:
        """Store a new asset, stamped with the server's time. The caller holds the lock."""
        asset = validate_asset(request.get("asset"))
        # IDs are compared ignoring capitals, so AST-1 and ast-1 can't both exist. A
        # deleted asset keeps its ID, so the ID can't be reused for something else.
        taken = {asset_id.lower() for asset_id in self.assets}
        if asset["asset_id"].lower() in taken:
            raise InvalidRequest(
                f"An asset with the ID {asset['asset_id']} already exists",
                code=DUPLICATE_ASSET,
                field="asset_id",
                asset_id=asset["asset_id"],
            )
        now = utc_now()
        asset.update(version=1, created_at=now, updated_at=now)
        self.assets[asset["asset_id"]] = asset
        logger.info("%s registered %s", user["username"], asset["asset_id"])
        return {"status": "OK", "stored": asset}, lambda: self.assets.pop(asset["asset_id"])

    def edit(self, request: dict, user: dict) -> tuple:
        """Change some of an asset's description, category, location and status.
        The caller holds the lock."""
        changes = request.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise InvalidRequest("changes must be an object with at least one field", field="changes")
        for field in changes:
            if field not in EDITABLE_FIELDS:
                raise InvalidRequest(
                    f"{field} cannot be edited", field=field, editable=list(EDITABLE_FIELDS)
                )
        clean = {field: check_text(value, field) for field, value in changes.items()}
        if "status" in clean:
            clean["status"] = check_status(clean["status"])

        previous = self.current_asset(request.get("asset_id"), request.get("expected_version"))
        # A new record replaces the old one, rather than the old one being edited, so
        # undoing is just putting the old record back.
        updated = dict(previous, **clean, version=previous["version"] + 1, updated_at=utc_now())
        self.assets[updated["asset_id"]] = updated
        logger.info("%s edited %s: %s", user["username"], updated["asset_id"], ", ".join(clean))
        return {"status": "OK", "stored": updated}, lambda: self.assets.__setitem__(updated["asset_id"], previous)

    def delete(self, request: dict, user: dict) -> tuple:
        """Soft-delete an asset: the record is kept, marked deleted with the server's
        time, and left out of normal results. The caller holds the lock."""
        previous = self.current_asset(request.get("asset_id"), request.get("expected_version"))
        now = utc_now()
        deleted = dict(previous, deleted=True, deleted_at=now, updated_at=now, version=previous["version"] + 1)
        self.assets[deleted["asset_id"]] = deleted
        logger.info("%s deleted %s", user["username"], deleted["asset_id"])
        reply = {"status": "OK", "deleted": deleted["asset_id"], "stored": deleted}
        return reply, lambda: self.assets.__setitem__(deleted["asset_id"], previous)

    def list_assets(self, request: dict, user: dict) -> dict:
        """Return the assets that have not been deleted and match the search, one page
        at a time if a page_size is given.

        Optional parameters:
          query      text that must appear in any of the asset's fields
          filters    {field: text}: the text must appear in that field
          page       which page to return, from 1
          page_size  how many assets per page, at most MAX_PAGE_SIZE
        Matching ignores capitals. Without page_size, every match is returned.
        The reply always says how many assets matched in total, and which page this is.
        """
        query = request.get("query")
        if query is not None:
            query = check_text(query, "query").lower()
        filters = request.get("filters") or {}
        if not isinstance(filters, dict):
            raise InvalidRequest("filters must be an object", field="filters")
        for field in filters:
            if field not in ASSET_FIELDS:
                raise InvalidRequest(f"Cannot search by {field}", field=field, searchable=list(ASSET_FIELDS))
        wanted = {field: check_text(text, field).lower() for field, text in filters.items()}

        def matches(asset: dict) -> bool:
            if asset.get("deleted"):
                return False
            if query and not any(query in asset[field].lower() for field in ASSET_FIELDS):
                return False
            return all(text in asset[field].lower() for field, text in wanted.items())

        with self.lock:
            found = [asset for asset in self.assets.values() if matches(asset)]

        total = len(found)
        page_size = request.get("page_size")
        if page_size is None:
            page_size = max(total, 1)
        elif not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise InvalidRequest(f"page_size must be from 1 to {MAX_PAGE_SIZE}", field="page_size")
        page = request.get("page", 1)
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise InvalidRequest("page must be a whole number of at least 1", field="page")

        start = (page - 1) * page_size
        return {
            "status": "OK",
            "assets": found[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, -(-total // page_size)),  # total / page_size, rounded up
        }

    # ---- Networking -----------------------------------------------------------

    def read_and_process(self, conn: socket.socket) -> dict:
        """Read one request and work out the reply, turning bad input into an error
        reply instead of an exception."""
        try:
            request = receive_message(conn, self.max_bytes)
        except MessageTooLargeError as exc:
            return error_reply(TOO_LARGE, str(exc), max_bytes=self.max_bytes)
        except ValueError as exc:
            return error_reply(BAD_REQUEST, str(exc))
        try:
            return self.process(request)
        except Exception:
            logger.exception("Could not complete a %s request", request.get("action"))
            return error_reply(INTERNAL, "The server could not complete the request")

    def handle_connection(self, conn: socket.socket, addr: tuple = None) -> None:
        """Serve one connection: one request, one reply. Runs on the client's own thread.

        Nothing one client does may affect the server or other clients, so every
        exception stops here. The timeout means an idle or stalled client is
        disconnected instead of holding its thread forever.
        """
        try:
            with conn:
                conn.settimeout(self.timeout)
                reply = self.read_and_process(conn)
                send_message(conn, reply, self.max_bytes)
        except OSError as exc:
            # The client went away or went quiet. There is nobody to reply to.
            logger.warning("Lost connection to %s: %s", addr, exc)
        except Exception:
            logger.exception("Unexpected error serving %s", addr)

    def open_socket(self) -> socket.socket:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(self.address)
        server_sock.listen()
        return server_sock

    def serve(self, stop: threading.Event = None) -> None:
        """Accept connections until `stop` is set (or forever)."""
        stop = stop or threading.Event()
        with self.open_socket() as server_sock:
            server_sock.settimeout(0.2)  # wake up regularly to check `stop`
            logger.info("Asset Management %s server listening on %s:%s", self.role, *self.address)
            while not stop.is_set():
                try:
                    conn, addr = server_sock.accept()
                except socket.timeout:
                    continue
                # Each client gets its own thread, so a slow client doesn't hold up the others.
                threading.Thread(target=self.handle_connection, args=(conn, addr), daemon=True).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = AssetServer(common.load_config())
    except FileNotFoundError as exc:
        logger.error("Could not start the server: %s. Create an account first: python users.py add admin admin", exc)
        return
    except (OSError, ValueError) as exc:
        logger.error("Could not start the server: %s", exc)
        return
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Server stopped.")


if __name__ == "__main__":
    main()

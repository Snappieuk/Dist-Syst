"""
Asset Management System server.

    python server.py

Understands these actions:
    {"action": "REGISTER", "asset": {...}}   -> validates and stores the asset, replies with it
    {"action": "LIST"}                       -> replies with every stored asset

Every request must include "protocol_version". Every reply has "status": "OK"
or "status": "ERROR" with a stable "code", a "message" and a "context".
"""

import datetime     # creates the server's timestamps
import json         # the data file is JSON
import logging      # records errors and events with timestamps
import os           # saves the data file safely
import re           # checks the format of asset IDs
import socket       # provides the networking building blocks (sockets)
import threading    # one thread per client, and a lock for the shared data

import common
from common import (
    ASSET_FIELDS,
    BAD_REQUEST,
    DUPLICATE_ASSET,
    INTERNAL,
    PROTOCOL_VERSION,
    STATUSES,
    TOO_LARGE,
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

        # Every client has its own thread, so two requests can arrive at the same
        # moment. The lock makes each read or change of the data one indivisible
        # step, so they cannot interfere with each other.
        self.lock = threading.Lock()

        self.assets = {}  # every asset, by asset_id
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

    def save(self) -> None:
        """Write all the data to disk. The caller must hold the lock.

        Writing straight over the real file would empty it first, so a crash part-way
        through would lose everything. Instead the data goes into a temporary file,
        which is forced onto the disk, then os.replace() swaps it into place in one
        atomic step. The file is always a complete old or a complete new version.
        """
        temp_path = self.data_file + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump({"assets": list(self.assets.values())}, file, indent=2)
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
        handler = {"REGISTER": self.register, "LIST": self.list_assets}.get(action)
        if handler is None:
            return error_reply(UNKNOWN_ACTION, f"Unknown action: {action!r}", action=action)
        try:
            return handler(request)
        except InvalidRequest as exc:
            return error_reply(exc.code, str(exc), **exc.context)

    def register(self, request: dict) -> dict:
        """Store a new asset, stamped with the server's time."""
        asset = validate_asset(request.get("asset"))
        with self.lock:
            # IDs are compared ignoring capitals, so AST-1 and ast-1 can't both exist.
            taken = {asset_id.lower() for asset_id in self.assets}
            if asset["asset_id"].lower() in taken:
                raise InvalidRequest(
                    f"An asset with the ID {asset['asset_id']} already exists",
                    code=DUPLICATE_ASSET,
                    field="asset_id",
                    asset_id=asset["asset_id"],
                )
            now = utc_now()
            asset["created_at"] = now
            asset["updated_at"] = now
            self.assets[asset["asset_id"]] = asset
            self.commit(lambda: self.assets.pop(asset["asset_id"]))
        logger.info("Registered %s", asset["asset_id"])
        return {"status": "OK", "stored": asset}

    def list_assets(self, request: dict) -> dict:
        """Return every asset."""
        with self.lock:
            return {"status": "OK", "assets": list(self.assets.values())}

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
    except (OSError, ValueError) as exc:
        logger.error("Could not start the server: %s", exc)
        return
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Server stopped.")


if __name__ == "__main__":
    main()

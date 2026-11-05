"""
Asset Management System server.

    python server.py

Understands these actions:
    {"action": "REGISTER", "asset": {...}}   -> stores the asset, replies with it
    {"action": "LIST"}                       -> replies with every stored asset

Every request must include "protocol_version". Every reply has "status": "OK"
or "status": "ERROR" with a stable "code", a "message" and a "context".
"""

import logging      # records errors and events with timestamps
import socket       # provides the networking building blocks (sockets)
import threading    # one thread per client, and a lock for the shared data

import common
from common import (
    BAD_REQUEST,
    INTERNAL,
    PROTOCOL_VERSION,
    TOO_LARGE,
    UNKNOWN_ACTION,
    UNSUPPORTED_VERSION,
    MessageTooLargeError,
    error_reply,
    receive_message,
    send_message,
)

SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)

logger = logging.getLogger("asset_server")


class AssetServer:
    """One asset server: its data and the rules for changing it."""

    def __init__(self, config: dict, role: str = "primary") -> None:
        self.config = config
        self.role = role
        self.address = (config["servers"][role]["host"], config["servers"][role]["port"])
        self.max_bytes = config["max_message_bytes"]
        self.timeout = config["server_timeout_seconds"]
        self.assets = []  # registered assets, as a list of dicts

        # Every client has its own thread, so two requests can arrive at the same
        # moment. The lock makes each read or change of the data one indivisible
        # step, so they cannot interfere with each other.
        self.lock = threading.Lock()

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
        if action == "REGISTER":
            asset = request.get("asset")
            if not isinstance(asset, dict):
                return error_reply(BAD_REQUEST, "'asset' must be an object", field="asset")
            with self.lock:
                self.assets.append(asset)
            return {"status": "OK", "stored": asset}
        if action == "LIST":
            with self.lock:
                return {"status": "OK", "assets": list(self.assets)}
        return error_reply(UNKNOWN_ACTION, f"Unknown action: {action!r}", action=action)

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
            server_sock.settimeout(0.5)  # wake up regularly to check `stop`
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
    server = AssetServer(common.load_config())
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Server stopped.")


if __name__ == "__main__":
    main()

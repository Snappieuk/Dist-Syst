"""
Asset Management System server.

    python server.py

Understands these actions:
    {"action": "REGISTER", "asset": {...}}   -> stores the asset, replies with it
    {"action": "LIST"}                       -> replies with every stored asset

Every request must include "protocol_version". Every reply has "status": "OK"
or "status": "ERROR" with a stable "code", a "message" and a "context".
"""

import socket       # provides the networking building blocks (sockets)
import threading    # lets tests stop the server cleanly

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


class AssetServer:
    """One asset server: its data and the rules for changing it."""

    def __init__(self, config: dict, role: str = "primary") -> None:
        self.config = config
        self.role = role
        self.address = (config["servers"][role]["host"], config["servers"][role]["port"])
        self.max_bytes = config["max_message_bytes"]
        self.assets = []  # registered assets, as a list of dicts

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
            self.assets.append(asset)
            return {"status": "OK", "stored": asset}
        if action == "LIST":
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
            return error_reply(INTERNAL, "The server could not complete the request")

    def handle_connection(self, conn: socket.socket) -> None:
        """Serve one connection: one request, one reply."""
        with conn:
            try:
                reply = self.read_and_process(conn)
                send_message(conn, reply, self.max_bytes)
            except OSError as exc:
                # The client went away part-way through. There is nobody to reply to.
                print(f"Lost connection to a client: {exc}")

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
            print(f"Asset Management {self.role} server listening on {self.address[0]}:{self.address[1]}")
            while not stop.is_set():
                try:
                    conn, _addr = server_sock.accept()
                except socket.timeout:
                    continue
                self.handle_connection(conn)


def main() -> None:
    server = AssetServer(common.load_config())
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Server stopped.")


if __name__ == "__main__":
    main()

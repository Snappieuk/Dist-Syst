"""
Shared helpers for the Asset Management System.

Messages are exchanged as JSON (an external data representation) so that
the server and client programs do not need to be written in the same
language, or run on the same type of machine, to understand each other.

This module is the single place that defines the wire format, the protocol
version, the error codes and how settings are loaded from config.json.
"""

import getpass   # asks for a password without showing it
import json      # turns Python dicts into JSON text and back
import os        # finds config.json next to this file
import socket    # the networking building blocks (sockets)
import uuid      # creates the unique ID sent with every change

HEADER_LENGTH_BYTES = 4  # every message is prefixed with a 4-byte length header

# Every request states which version of the protocol it speaks. The server rejects
# any other version, so an old client and a new server fail clearly instead of
# misunderstanding each other.
PROTOCOL_VERSION = 1

# Stable error codes. Clients check these, never the wording of the message.
BAD_REQUEST = "BAD_REQUEST"                    # malformed request or invalid data
UNKNOWN_ACTION = "UNKNOWN_ACTION"              # the action is not one the server supports
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"    # missing or unsupported protocol_version
TOO_LARGE = "TOO_LARGE"                        # the message is over the size limit
INTERNAL = "INTERNAL"                          # the server failed; the request may be fine
DUPLICATE_ASSET = "DUPLICATE_ASSET"            # an asset with that asset_id already exists
NOT_FOUND = "NOT_FOUND"                        # no current asset has that asset_id
CONFLICT = "CONFLICT"                          # the asset changed since the client read it
UNAUTHENTICATED = "UNAUTHENTICATED"            # wrong login, or a missing, invalid or expired token
FORBIDDEN = "FORBIDDEN"                        # the user's role does not allow this action

# The fields a client supplies for an asset, and the only statuses allowed.
ASSET_FIELDS = ("asset_id", "description", "category", "location", "status")
STATUSES = ("In Use", "Not In Use")

# The fields EDIT may change. The asset_id identifies the asset, so it never changes.
EDITABLE_FIELDS = ("description", "category", "location", "status")

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class MessageTooLargeError(ValueError):
    """A message is bigger than the configured limit."""


def load_config(path: str = CONFIG_FILE) -> dict:
    """Read the settings from config.json.

    Host names, ports and other machine-specific settings live in this file, so the
    servers and clients can be moved to different machines by editing it rather than
    the code. File paths in it are relative to the folder that holds the file.
    """
    with open(path, encoding="utf-8") as file:
        config = json.load(file)
    config["folder"] = os.path.dirname(os.path.abspath(path))
    return config


def config_path(config: dict, name: str) -> str:
    """Return the full path of a file named in the configuration."""
    return os.path.join(config["folder"], name)


def error_reply(code: str, message: str, **context) -> dict:
    """Build an ERROR reply. Every error has a stable code, a message for people and,
    where useful, context such as the field or asset ID it concerns."""
    return {"status": "ERROR", "code": code, "message": message, "context": context}


def send_message(sock: socket.socket, message: dict, max_bytes: int) -> None:
    """Encode `message` as UTF-8 JSON and send it with a 4-byte big-endian length
    prefix, so the receiver knows exactly where the message ends."""
    payload = json.dumps(message).encode("utf-8")
    if len(payload) > max_bytes:
        raise MessageTooLargeError(f"The message is {len(payload)} bytes; the limit is {max_bytes}")
    header = len(payload).to_bytes(HEADER_LENGTH_BYTES, byteorder="big")
    sock.sendall(header + payload)


def _recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly `num_bytes` from `sock`, or raise ConnectionError."""
    chunks = []
    remaining = num_bytes
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise ConnectionError("Connection closed before a full message was received")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_message(sock: socket.socket, max_bytes: int) -> dict:
    """Read one length-prefixed JSON message from `sock`.

    The length in the header is checked before any of the message is read, so a
    sender claiming a huge size cannot make the receiver allocate memory for it.
    Raises MessageTooLargeError, or ValueError if the bytes are not a JSON object.
    """
    header = _recv_exact(sock, HEADER_LENGTH_BYTES)
    length = int.from_bytes(header, byteorder="big")
    if length > max_bytes:
        raise MessageTooLargeError(f"The message is {length} bytes; the limit is {max_bytes}")
    payload = _recv_exact(sock, length)
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"The message is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("The message must be a JSON object")
    return message


def new_request_id() -> str:
    """Return a new unique ID for a state-changing request (REGISTER, EDIT, DELETE).

    If a reply is lost, the client sends the same request again with the same ID, and
    the server recognises it and returns the original result instead of making the
    change twice.
    """
    return uuid.uuid4().hex


def login(username: str, password: str, config: dict = None) -> dict:
    """Log in, returning the server's reply. On success it holds "token", which must be
    sent with every later request, and the user's "role"."""
    return send_request({"action": "LOGIN", "username": username, "password": password}, config)


def log_in_from_prompt(config: dict = None) -> str:
    """Ask for a username and password at the command line and log in. Returns the
    session token, or None after printing why the login failed."""
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    try:
        reply = login(username, password, config)
    except (OSError, ValueError) as exc:
        print(f"Could not reach the server: {exc}")
        return None
    if reply.get("status") != "OK":
        print(f"Login failed: {reply.get('message')}")
        return None
    return reply["token"]


def send_request(request: dict, config: dict = None) -> dict:
    """Send one request to the server and return its reply.

    The protocol version is added here, so every client sends it without having to
    remember to.
    """
    config = config or load_config()
    server = config["servers"]["primary"]
    message = {**request, "protocol_version": PROTOCOL_VERSION}
    # The timeout applies to connecting and to every read, so a stalled server
    # cannot leave the client waiting forever.
    timeout = config["client_timeout_seconds"]
    with socket.create_connection((server["host"], server["port"]), timeout=timeout) as sock:
        send_message(sock, message, config["max_message_bytes"])
        return receive_message(sock, config["max_message_bytes"])

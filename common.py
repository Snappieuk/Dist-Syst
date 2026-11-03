"""
Shared helpers for the Asset Management System prototype.

Messages are exchanged as JSON (an external data representation) so that
the server and client programs do not need to be written in the same
language, or run on the same type of machine, to understand each other.

This module is the one place that defines the "wire format", so the rules
about what counts as a valid message live here rather than being repeated
(and possibly disagreeing) in every program.
"""

import json      # used to turn a Python dict into a JSON string, and back
import socket    # only imported here for the type hints below (socket.socket)

HEADER_LENGTH_BYTES = 4  # every message is prefixed with a 4-byte length header

# The largest message either side will send or accept (1 MiB).
#
# WHY: the 4-byte header can claim up to 4 GB. The header comes from another
# machine, so it must be treated as untrusted input. Without a cap, a faulty
# or malicious client could claim 4 GB and make a server thread try to
# collect that much data, which is a cheap denial-of-service attack. Checking
# the claim BEFORE reading the payload means the attacker gets no memory at all.
MAX_MESSAGE_BYTES = 1024 * 1024

HOST = "127.0.0.1"  # "localhost" - server and clients all run on this same machine
PORT = 65432        # arbitrary unused port number; server and clients must agree on it


class ProtocolError(ValueError):
    """The bytes received are not a valid message.

    Subclassing ValueError keeps existing `except ValueError` handlers working
    and matches what json.loads() and bytes.decode() raise for bad data, so
    callers can treat every kind of malformed message in the same way.
    """


class MessageTooLargeError(ProtocolError):
    """A message is bigger than MAX_MESSAGE_BYTES.

    This is a separate type so the server can reply TOO_LARGE rather than the
    more general BAD_REQUEST.
    """


def send_message(sock: socket.socket, message: dict) -> None:
    """Encode `message` as JSON and send it over `sock`.

    The JSON payload is prefixed with a 4-byte big-endian length so the
    receiving end knows exactly how many bytes to read for one message,
    even if TCP delivers the data in more than one chunk. This is a very
    small example of an external data representation: both ends agree on
    a byte-level format (length-prefixed UTF-8 JSON) regardless of what
    language or platform either end is written in.
    """
    # Turn the Python dict into a JSON string, then into raw bytes (UTF-8).
    payload = json.dumps(message).encode("utf-8")

    # Apply the same limit when sending as when receiving. Failing here gives
    # the sender a clear error, instead of the other end rejecting the message
    # after it has been sent across the network.
    if len(payload) > MAX_MESSAGE_BYTES:
        raise MessageTooLargeError(
            f"Message is {len(payload)} bytes; the limit is {MAX_MESSAGE_BYTES}"
        )

    # Work out how many bytes `payload` is, and pack that number itself
    # into exactly 4 bytes (big-endian = most significant byte first).
    # This is the "header" the receiver will read first.
    header = len(payload).to_bytes(HEADER_LENGTH_BYTES, byteorder="big")

    # Send the header immediately followed by the payload in one go.
    # sendall() keeps sending until every byte has actually gone out.
    sock.sendall(header + payload)


def _recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly `num_bytes` from `sock`, or raise ConnectionError."""
    chunks = []          # list of byte-chunks received so far
    remaining = num_bytes  # how many more bytes we still need to read

    # TCP does not guarantee recv() returns everything in one call, so we
    # keep calling it in a loop until we have collected `num_bytes` bytes.
    while remaining > 0:
        chunk = sock.recv(remaining)  # ask for (at most) the remaining bytes

        if not chunk:
            # recv() returning empty bytes means the other end closed the
            # connection before sending everything it promised to.
            raise ConnectionError("Connection closed before a full message was received")

        chunks.append(chunk)          # remember this piece
        remaining -= len(chunk)       # we need that many fewer bytes now

    return b"".join(chunks)  # stitch all the pieces back into one bytes object


def receive_message(sock: socket.socket) -> dict:
    """Read one length-prefixed JSON message from `sock`.

    Raises:
        OSError (including ConnectionError): the connection failed, so no
            reply can be delivered.
        MessageTooLargeError: the header claims more than MAX_MESSAGE_BYTES.
        ValueError (including ProtocolError): the connection is working but
            the data is invalid, so the sender can be told what was wrong.
    """
    # First, read exactly the 4-byte header written by send_message().
    header = _recv_exact(sock, HEADER_LENGTH_BYTES)

    # Convert those 4 bytes back into the integer length they represent.
    length = int.from_bytes(header, byteorder="big")

    # Check the claimed size before reading any of the payload (see the
    # comment on MAX_MESSAGE_BYTES for why the order matters).
    if length > MAX_MESSAGE_BYTES:
        raise MessageTooLargeError(
            f"Message claims {length} bytes; the limit is {MAX_MESSAGE_BYTES}"
        )

    # Now read exactly `length` bytes - that is the JSON payload itself.
    payload = _recv_exact(sock, length)

    # Turn the raw bytes back into text, then parse the JSON.
    try:
        message = json.loads(payload.decode("utf-8"))
    except RecursionError as exc:
        # Very deeply nested JSON such as [[[[... can fit inside the size limit
        # but exhaust the parser's stack. That is bad input, not a server fault.
        raise ProtocolError("Message is too deeply nested") from exc

    # `[1, 2]` and `"hello"` are valid JSON but not valid messages. Checking
    # here means every caller can safely assume it has a dict and call .get().
    if not isinstance(message, dict):
        raise ProtocolError("Message must be a JSON object")

    return message

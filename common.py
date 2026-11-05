"""
Shared helpers for the Asset Management System prototype.

Messages are exchanged as JSON (an external data representation) so that
the server and client programs do not need to be written in the same
language, or run on the same type of machine, to understand each other.

This module is the one place that defines the "wire format", so the rules
about what counts as a valid message live here rather than being repeated
(and possibly disagreeing) in every program.
"""

import json         # used to turn a Python dict into a JSON string, and back
import socket       # provides the networking building blocks (sockets)
import time         # waits while the backup takes over
import unicodedata  # recognises letters and digits in any language, for tags
import uuid         # creates the random request ID sent with each registration

HEADER_LENGTH_BYTES = 4  # every message is prefixed with a 4-byte length header

# The largest message either side will send or accept (10 MiB). This is enough
# for a LIST reply with tens of thousands of assets. A larger list gets a TOO_LARGE
# reply instead, which also stops the servers syncing, because there is no paging.
#
# WHY: the 4-byte header can claim up to 4 GB. The header comes from another
# machine, so it must be treated as untrusted input. Without a cap, a faulty
# client could claim 4 GB and make a server thread try to collect that much data.
# Checking the claim BEFORE reading the payload means no memory is used for it.
MAX_MESSAGE_BYTES = 10 * 1024 * 1024

# The address of each server: the primary first, then the backup. Every program
# must agree on this list. 127.0.0.1 means "this machine"; to use separate
# machines, replace it with each server's IP address.
SERVERS = [
    ("127.0.0.1", 65432),  # primary
    ("127.0.0.1", 65433),  # backup
]

# How long request_from() waits on each network step before giving up. Clients
# use it for their requests, and the primary uses it when copying to the backup.
TIMEOUT_SECONDS = 5

# How long the servers wait for each other's heartbeats. This is short, because a
# heartbeat needs no work from the other server, so a slow one means trouble.
HEARTBEAT_TIMEOUT_SECONDS = 1

# How long a client keeps trying while no server is accepting changes. This is long
# enough for the backup to notice that the primary has failed and take over.
FAILOVER_WAIT_SECONDS = 15

# Error codes, sent as the "code" field of an ERROR reply. "detail" is written
# for people and may be reworded, so programs should act on the code instead.
BAD_REQUEST = "BAD_REQUEST"  # the request is malformed or fails validation
NOT_FOUND = "NOT_FOUND"      # no asset has that asset_id on this server
TOO_LARGE = "TOO_LARGE"      # the message is bigger than MAX_MESSAGE_BYTES
INTERNAL = "INTERNAL"        # the server failed; the request itself may be fine
DUPLICATE_ASSET = "DUPLICATE_ASSET"  # the same item is already registered
NOT_PRIMARY = "NOT_PRIMARY"  # this server is on standby; try the other one

# Error codes used only between the two servers.
NOT_STANDBY = "NOT_STANDBY"  # the backup has taken over, so it cannot receive copies
OUT_OF_SYNC = "OUT_OF_SYNC"  # the backup missed a change, so it needs all the data

# The two standard asset statuses. Registration also accepts other text.
IN_USE = "In Use"
NOT_IN_USE = "Not In Use"


class MessageTooLargeError(ValueError):
    """A message is bigger than MAX_MESSAGE_BYTES.

    It has its own type so the server can reply TOO_LARGE. It is a ValueError
    because that is what the other kinds of invalid message raise too.
    """


# Words too common to be useful, so they are left out of description keywords.
COMMON_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _split_words(value: str) -> list:
    """Split text into words, in any language.

    Letters, digits and combining marks (the Unicode categories starting L, N and
    M) belong to a word, and everything else separates words. Marks are included
    because some languages need them inside a word, such as the vowel signs in Hindi.
    NFC normalisation stores an accented letter one way, however it was typed.
    """
    normalised = unicodedata.normalize("NFC", value)
    kept = (char if unicodedata.category(char)[0] in "LNM" else " " for char in normalised)
    return "".join(kept).split()


def normalise_text(value: str) -> str:
    """Reduce text to a form that ignores differences in capitals, spacing and
    punctuation, so "Dell-Laptop" and "dell  laptop" compare as equal."""
    return " ".join(word.lower() for word in _split_words(value)) or value.strip().lower()


def _phrase_to_tags(value: str) -> list:
    """Split free text into lowercase keywords, leaving out stop words.

    A value that is not a string gives an empty list.
    """
    if not isinstance(value, str):
        return []

    clean = []
    for token in _split_words(value):
        token = token.lower()
        if token not in COMMON_STOP_WORDS:
            clean.append(token)
    return clean


def generate_asset_tags(asset: dict) -> list:
    """Build an asset's tags from its other fields, in lowercase with no repeats.

    The tags are, in order: the category, location and status as whole phrases,
    the asset ID, then up to five keywords from the description. Anything other
    than a dict gives an empty list.
    """
    if not isinstance(asset, dict):
        return []

    tags = []
    seen = set()

    def add_tag(tag: str) -> None:
        if not tag:
            return
        normalised = tag.strip().lower()
        if normalised and normalised not in seen:
            tags.append(normalised)
            seen.add(normalised)

    # Whole phrases, with punctuation replaced by single spaces, so
    # "Site A - IT Store" becomes "site a it store".
    for field in ("category", "location", "status"):
        value = asset.get(field)
        if isinstance(value, str):
            add_tag(" ".join(_split_words(value)))

    # Punctuation in the ID becomes hyphens, so "AST_0001" becomes "ast-0001".
    asset_id = asset.get("asset_id")
    if asset_id is not None:
        add_tag("-".join(_split_words(str(asset_id))))

    # The first five keywords. Any that repeat an earlier tag are skipped, so
    # this can add fewer than five.
    description = asset.get("description", "")
    for token in _phrase_to_tags(description)[:5]:
        add_tag(token)

    return tags


def format_asset_tags(tags: list) -> str:
    """Join tags into one comma-separated string for the GUI's table."""
    if not tags:
        return ""
    return ", ".join(str(tag) for tag in tags)


def new_register_request(asset: dict) -> dict:
    """Build a REGISTER request with a new random request ID.

    The server stores the request ID with the asset. If the request is sent again, the
    server recognises it and replies with that asset, rather than storing a second
    copy, which makes retrying safe. To retry, send the same request again.
    """
    return {"action": "REGISTER", "request_id": uuid.uuid4().hex, "asset": asset}


def include_asset(assets: list, stored: object) -> list:
    """Return `assets`, adding the just-registered `stored` asset if it is missing.

    A client's list may come from a different server than its registration did,
    and that server may not have synced yet. The registration still succeeded, so
    the client should show the asset it was told was stored.
    """
    if not isinstance(stored, dict):
        return assets
    if any(asset.get("asset_id") == stored.get("asset_id") for asset in assets):
        return assets
    return assets + [stored]


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

    Raises OSError if the connection fails, so no reply can be delivered. Raises
    ValueError if the connection works but the data is invalid, so the sender can
    be told what was wrong. JSON nested too deeply to parse raises RecursionError.
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
    message = json.loads(payload.decode("utf-8"))

    # `[1, 2]` and `"hello"` are valid JSON but not valid messages. Checking
    # here means every caller can safely assume it has a dict and call .get().
    if not isinstance(message, dict):
        raise ValueError("Message must be a JSON object")

    return message


def request_from(address: tuple, request: dict, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Send one request to the server at `address` and return its reply."""
    # create_connection() is used because it takes a timeout. Without one, a
    # stalled server would leave the program waiting forever.
    with socket.create_connection(address, timeout=timeout) as sock:
        send_message(sock, request)
        return receive_message(sock)


def send_request(request: dict) -> dict:
    """Send a request to the first server that answers. Used by every client.

    This is failover: if the primary is down, the client moves on to the backup
    without the user having to do anything. Trying a request again on the backup is
    safe even if the primary did the work and only its reply was lost, because the
    backup would recognise a repeated request or item.

    The next server is also tried after two kinds of ERROR reply. INTERNAL, such as
    one server's disk being full, is safe to retry because a server undoes a change
    it could not save. NOT_FOUND may only mean this server has not got the asset yet.
    If no server does better, the last such reply is returned.

    If a server replies NOT_PRIMARY and no other has done the work, the backup has not
    taken over yet, so this waits and tries again, for up to FAILOVER_WAIT_SECONDS.

    Raises ConnectionError with a clear message if no server can be reached, and
    ValueError if the request is too large or a reply is not a valid message.
    """
    deadline = time.monotonic() + FAILOVER_WAIT_SECONDS

    while True:
        last_error = None
        retryable_reply = None
        not_primary_reply = None

        for address in SERVERS:
            try:
                reply = request_from(address, request)
            except OSError as exc:
                # OSError covers a refused connection, a timeout and a dropped connection.
                last_error = exc
                continue

            if reply.get("code") in (INTERNAL, NOT_FOUND):
                retryable_reply = reply
            elif reply.get("code") == NOT_PRIMARY:
                not_primary_reply = reply
            else:
                return reply

        if retryable_reply is not None:
            return retryable_reply

        if not_primary_reply is None:
            break  # no server could be reached, so there is nothing to wait for

        if time.monotonic() >= deadline:
            return not_primary_reply
        time.sleep(1)

    raise ConnectionError(
        f"Could not reach any asset server ({last_error}).\n"
        "Check that server.py is running, then try again."
    )

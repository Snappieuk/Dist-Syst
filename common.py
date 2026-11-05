"""
Shared helpers for the Asset Management System.

Messages are exchanged as JSON (an external data representation) so that
the server and client programs do not need to be written in the same
language, or run on the same type of machine, to understand each other.

This module is the one place that defines the "wire format", so the rules
about what counts as a valid message live here rather than being repeated
(and possibly disagreeing) in every program.
"""

import json         # used to turn a Python dict into a JSON string, and back
import logging      # each program's log, on screen and in its own file
import logging.handlers  # replaces a log file with a new one when it gets too big
import os           # finds and creates the shared secret file
import secrets      # generates the shared secret
import socket      # provides the networking building blocks (sockets)
import sys          # records crashes in the log
import time         # waits while the backup takes over
import unicodedata  # recognises letters and digits in any language, for tags
import uuid         # creates the random request ID sent with each registration

HEADER_LENGTH_BYTES = 4  # every message is prefixed with a 4-byte length header

# The largest message either side will send or accept (10 MiB). This is enough
# for a LIST reply with tens of thousands of assets. A larger list gets a TOO_LARGE
# reply instead, and the primary can no longer reconcile with a returning backup, because
# there is no paging. Copying each new change to the backup still works.
#
# WHY: the 4-byte header can claim up to 4 GB. The header comes from another
# machine, so it must be treated as untrusted input. Without a cap, a faulty
# client could claim 4 GB and make a server thread try to collect that much data.
# Checking the claim BEFORE reading the payload means no memory is used for it.
MAX_MESSAGE_BYTES = 10 * 1024 * 1024

# The address of each server: the primary first, then the backup. Every program
# must agree on this list. 127.0.0.1 means "this machine", so by default everything
# runs on one computer.
DEFAULT_SERVERS = [
    ("127.0.0.1", 65432),  # primary
    ("127.0.0.1", 65433),  # backup
]

# To run the servers on separate machines, set this environment variable to the same
# list on every machine, for example ASSET_SERVERS=assets-1:65432,assets-2:65433.
# Hostnames are better than IP addresses, because a machine can then change its address
# without every program being reconfigured. Nothing in the code has to change.
SERVERS_VARIABLE = "ASSET_SERVERS"


def parse_servers(text: str) -> list:
    """Turn "host:port,host:port" into a list of (host, port) addresses.

    Raises ValueError, naming the bad entry, if the text is not one or two addresses.
    """
    servers = []
    for entry in text.split(","):
        entry = entry.strip()
        host, colon, port = entry.rpartition(":")
        if not colon or not host or not (port.isascii() and port.isdigit()) or not 0 < int(port) < 65536:
            raise ValueError(f"{SERVERS_VARIABLE}: {entry!r} should look like host:port")
        servers.append((host, int(port)))

    if len(servers) > 2:
        raise ValueError(f"{SERVERS_VARIABLE} lists at most two servers: the primary, then the backup")
    return servers


SERVERS = parse_servers(os.environ[SERVERS_VARIABLE]) if os.environ.get(SERVERS_VARIABLE) else DEFAULT_SERVERS

# The key every program sends to prove it belongs to this system. It is created the first
# time any program runs. To use several machines, copy this file to each of them.
SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret.key")

_secret = None  # the key, once read, so the file is only read once

# Where each program keeps its log file, named after it: primary.log, backup.log, web.log
# and start.log. A file that reaches LOG_FILE_BYTES is renamed to name.log.1, the older
# ones move up to name.log.2 and so on, and only LOG_FILE_COPIES old files are kept.
LOG_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_NAMES = ("primary", "backup", "web", "start")
LOG_FILE_BYTES = 1024 * 1024
LOG_FILE_COPIES = 5

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
CONFLICT = "CONFLICT"        # someone else changed the asset since the client read it
NOT_PRIMARY = "NOT_PRIMARY"  # this server is on standby; try the other one
UNAUTHORIZED = "UNAUTHORIZED"  # the request did not include the shared secret
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"  # no protocol_version, or one the server doesn't speak

# The version of the message format this program speaks, sent in every request. A
# server only accepts the versions it knows, so a program written for a different
# format gets a clear error instead of being misunderstood. Change this, and add the
# new number to SUPPORTED_PROTOCOL_VERSIONS, whenever requests or replies change in a
# way older programs couldn't handle.
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)

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


def log_path(name: str) -> str:
    return os.path.join(LOG_FOLDER, name + ".log")


def configure_logging(name: str) -> None:
    """Send this program's log to the console and to its own file, logs/<name>.log.

    Each program has its own file because they are separate processes. Two processes
    writing to one file can mix up each other's lines, and can't safely take turns
    renaming it when it fills up. Each line is stamped with the time, the level, the
    program and the thread, so files from different programs can be lined up.

    Passwords and the shared secret are never logged. If the file can't be opened, the
    program still runs, logging to the console only.
    """
    handlers = [logging.StreamHandler()]
    problem = None
    try:
        os.makedirs(LOG_FOLDER, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_path(name), maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_COPIES, encoding="utf-8"
        ))
    except OSError as exc:
        problem = exc

    # force=True replaces any earlier set-up, so calling this twice can't log lines twice.
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [{name}] [%(threadName)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    log = logging.getLogger(name)
    if problem is None:
        log.info("Logging to %s", log_path(name))
    else:
        log.warning("Logging to the console only, because the log file could not be opened: %s", problem)

    def log_crash(kind, value, traceback) -> None:
        # A crash would otherwise only reach the console, which may already be closed.
        # The log shows it on the console too, so Python doesn't print it a second time.
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, traceback)
        else:
            log.critical("Stopped by an unexpected error", exc_info=(kind, value, traceback))

    sys.excepthook = log_crash


def shared_secret() -> str:
    """Return the key that proves a program belongs to this system, creating it if this
    is the first program to run.

    Exclusive creation means that if the servers start at the same moment, only one
    creates the file. The others wait for it to finish, then read the same key. Other
    users of the machine cannot read it on Linux and macOS.
    """
    global _secret

    if _secret is None:
        try:
            file = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _secret = _read_secret_file()
        else:
            _secret = secrets.token_hex(32)
            with os.fdopen(file, "w") as secret_file:
                secret_file.write(_secret)
    return _secret


def _read_secret_file() -> str:
    """Read the secret file, allowing a moment for another program to finish writing it."""
    for _ in range(50):
        with open(SECRET_FILE, encoding="utf-8") as file:
            secret = file.read().strip()
        if secret:
            return secret
        time.sleep(0.1)
    raise ValueError(f"{SECRET_FILE} is empty")


def request_from(address: tuple, request: dict, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Send one request to the server at `address` and return its reply.

    Every request carries the shared secret, because the server refuses any without it,
    and the version of the message format it uses.
    """
    # The servers only listen on IPv4, so a name is looked up as IPv4 only. Otherwise a
    # name with an IPv6 address too, such as "localhost", is tried over IPv6 first, and
    # on Windows each failed attempt takes 2 seconds, longer than a heartbeat may take.
    # A name that can't be found raises socket.gaierror, an OSError like a refusal.
    host, port = address
    ipv4_address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]

    # create_connection() is used because it takes a timeout. Without one, a
    # stalled server would leave the program waiting forever.
    with socket.create_connection(ipv4_address, timeout=timeout) as sock:
        send_message(sock, {**request, "auth": shared_secret(), "protocol_version": PROTOCOL_VERSION})
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

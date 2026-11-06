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

# The address of each server: the primary first, then each backup in the order they take
# over. Every program must agree on this list. 127.0.0.1 means "this machine", so by default everything
# runs on one computer.
DEFAULT_SERVERS = [
    ("127.0.0.1", 65432),  # primary
    ("127.0.0.1", 65433),  # backup
]

# The most servers the list can hold. Each server gives out asset IDs with its own letter,
# and server.ID_LETTERS has this many.
MAX_SERVERS = 25

# Where web.py listens. 0.0.0.0 means every network the machine is on, so the page can be
# opened from other devices as well as from this one. Use 127.0.0.1 to allow only this machine.
DEFAULT_WEB_ADDRESS = ("0.0.0.0", 8080)

# Settings can come from three places, and a later one overrides an earlier one:
#   1. the defaults in this file
#   2. config.json beside the code, if it exists (see config.example.json), or the file
#      named by the ASSET_CONFIG environment variable
#   3. environment variables, such as ASSET_SERVERS=assets-1:65432,assets-2:65433
# To run the servers on separate machines, give every machine the same server list.
# Hostnames are better than IP addresses, because a machine can then change its address
# without every program being reconfigured. Nothing in the code has to change.
CONFIG_FILE = os.environ.get("ASSET_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SERVERS_VARIABLE = "ASSET_SERVERS"
WEB_ADDRESS_VARIABLE = "ASSET_WEB_ADDRESS"

# What config.json may contain. Anything else is rejected, so a misspelt setting is
# noticed instead of silently ignored.
CONFIG_TIMEOUTS = ("timeout_seconds", "heartbeat_timeout_seconds", "failover_wait_seconds")
CONFIG_KEYS = ("servers", "web_address") + CONFIG_TIMEOUTS


def parse_address(text: str, setting: str) -> tuple:
    """Turn "host:port" into a (host, port) address, or raise ValueError naming `setting`."""
    entry = text.strip()
    host, colon, port = entry.rpartition(":")
    if not colon or not host or not (port.isascii() and port.isdigit()) or not 0 < int(port) < 65536:
        raise ValueError(f"{setting}: {entry!r} should look like host:port")
    return host, int(port)


def parse_servers(text: str, setting: str = SERVERS_VARIABLE) -> list:
    """Turn "host:port,host:port" into a list of (host, port) addresses.

    Raises ValueError, naming the bad entry, if an address is wrong or there are more
    than MAX_SERVERS.
    """
    servers = [parse_address(entry, setting) for entry in text.split(",")]
    if len(servers) > MAX_SERVERS:
        raise ValueError(f"{setting} lists {len(servers)} servers, but there can be at most {MAX_SERVERS}")
    return servers


def load_config(path: str) -> dict:
    """Read the settings file, returning only the settings it contains, already checked.

    A missing file is normal and gives no settings. A file that can't be understood
    raises ValueError naming the file and the problem, so a mistake is never ignored.
    """
    try:
        with open(path, encoding="utf-8") as file:
            raw = json.load(file)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None

    if not isinstance(raw, dict):
        raise ValueError(f"{path} must hold a JSON object")
    unknown = sorted(set(raw) - set(CONFIG_KEYS))
    if unknown:
        raise ValueError(f"{path} has unknown settings: {', '.join(unknown)}. It may contain: {', '.join(CONFIG_KEYS)}")

    settings = {}
    if "servers" in raw:
        entries = raw["servers"]
        if not isinstance(entries, list) or not entries or not all(isinstance(entry, str) for entry in entries):
            raise ValueError(f'{path}: "servers" must be a list such as ["assets-1:65432", "assets-2:65433"]')
        settings["servers"] = parse_servers(",".join(entries), f"{path} servers")
    if "web_address" in raw:
        if not isinstance(raw["web_address"], str):
            raise ValueError(f'{path}: "web_address" must be text such as "127.0.0.1:8080"')
        settings["web_address"] = parse_address(raw["web_address"], f"{path} web_address")
    for name in CONFIG_TIMEOUTS:
        if name in raw:
            value = raw[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 300:
                raise ValueError(f'{path}: "{name}" must be a number of seconds above 0 and at most 300')
            settings[name] = value
    return settings


_config = load_config(CONFIG_FILE)

SERVERS = (parse_servers(os.environ[SERVERS_VARIABLE]) if os.environ.get(SERVERS_VARIABLE)
           else _config.get("servers", DEFAULT_SERVERS))
WEB_ADDRESS = (parse_address(os.environ[WEB_ADDRESS_VARIABLE], WEB_ADDRESS_VARIABLE)
               if os.environ.get(WEB_ADDRESS_VARIABLE) else _config.get("web_address", DEFAULT_WEB_ADDRESS))


def address_to_connect_to(address: tuple) -> tuple:
    """The address to use to reach a program on THIS machine. A program listening on
    0.0.0.0 or :: listens everywhere, but those can't be connected to (Windows refuses),
    so this machine uses 127.0.0.1 for them."""
    host, port = address
    return ("127.0.0.1" if host in ("0.0.0.0", "::", "") else host, port)

# Everything the programs create as they run goes in this one folder: the data files,
# the web accounts, the shared secret, the certificate and the logs. Keeping it all in
# one place keeps the code folder tidy, and git ignores the whole folder. Each program
# creates the folder when it first writes something there.
DATA_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def make_folder_for(path: str) -> None:
    """Create the folder a file is about to be written in, if it doesn't exist yet."""
    os.makedirs(os.path.dirname(path), exist_ok=True)


# The key every program sends to prove it belongs to this system. It is created the first
# time any program runs. To use several machines, copy this file to each of them.
SECRET_FILE = os.path.join(DATA_FOLDER, "secret.key")

_secret = None  # the key, once read, so the file is only read once

# The certificate that encrypts every connection to a server, with its private key, in one
# file. Setup creates it, or the first program to run if setup hasn't. Like secret.key, it
# must be copied to every machine. Clients trust this one certificate and nothing else, so
# a server with any other certificate is refused, even one a public authority signed.
TLS_FILE = os.path.join(DATA_FOLDER, "tls.pem")
TLS_CERTIFICATE_DAYS = 3650  # about 10 years, after which setup --reset makes a new one

_tls_contexts = {}  # the client and server settings, once loaded, by side

# Where each program keeps its log file, named after it: primary.log, backup.log (and
# backup2.log and so on, if there are more servers), web.log and start.log. A file that reaches LOG_FILE_BYTES is renamed to name.log.1, the older
# ones move up to name.log.2 and so on, and only LOG_FILE_COPIES old files are kept.
LOG_FOLDER = os.path.join(DATA_FOLDER, "logs")
LOG_FILE_BYTES = 1024 * 1024
LOG_FILE_COPIES = 5

# How long request_from() waits on each network step before giving up. Clients
# use it for their requests, and the primary uses it when copying to the backup.
TIMEOUT_SECONDS = _config.get("timeout_seconds", 5)

# How long the servers wait for each other's heartbeats. This is short, because a
# heartbeat needs no work from the other server, so a slow one means trouble.
HEARTBEAT_TIMEOUT_SECONDS = _config.get("heartbeat_timeout_seconds", 1)

# How long a client keeps trying while no server is accepting changes. This is long
# enough for the backup to notice that the primary has failed and take over.
FAILOVER_WAIT_SECONDS = _config.get("failover_wait_seconds", 15)

# Error codes, sent as the "code" field of an ERROR reply. "detail" is written
# for people and may be reworded, so programs should act on the code instead.
BAD_REQUEST = "BAD_REQUEST"  # the request is malformed or fails validation
NOT_FOUND = "NOT_FOUND"      # no asset has that asset_id on this server
TOO_LARGE = "TOO_LARGE"      # the message is bigger than MAX_MESSAGE_BYTES
INTERNAL = "INTERNAL"        # the server failed; the request itself may be fine
DUPLICATE_ASSET = "DUPLICATE_ASSET"  # the same item is already registered
CONFLICT = "CONFLICT"        # someone else changed the asset since the client read it
NOT_PRIMARY = "NOT_PRIMARY"  # this server is on standby; try the other one
UNAUTHORIZED = "UNAUTHORIZED"  # no shared secret, and no valid login either
FORBIDDEN = "FORBIDDEN"      # a logged-in user is not allowed to do that (only admins can delete)

# A GUI finds servers by broadcasting this text over UDP to a server's port. A server
# answers with a JSON object naming this service. It is not secret and reveals no data.
DISCOVERY_REQUEST = b"ASSET_MANAGEMENT_DISCOVER"
SERVICE_NAME = "asset-management"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"  # no protocol_version, or one the server doesn't speak

# The version of the message format this program speaks, sent in every request. A
# server only accepts the versions it knows, so a program written for a different
# format gets a clear error instead of being misunderstood. Change this, and add the
# new number to SUPPORTED_PROTOCOL_VERSIONS, whenever requests or replies change in a
# way older programs couldn't handle.
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)

# Error codes used only between the servers.
NOT_STANDBY = "NOT_STANDBY"  # the backup has taken over, so it cannot receive copies
OUT_OF_SYNC = "OUT_OF_SYNC"  # the backup missed a change, so it needs all the data

# The only two statuses an asset can have. Every client offers just these, and the
# server rejects anything else, because any program could send other text.
# The tag every made-up asset gets, such as those from tools/fill.py and tools/demo.py.
SAMPLE_TAG = "sample data"

IN_USE = "In Use"
NOT_IN_USE = "Not In Use"
STATUSES = (IN_USE, NOT_IN_USE)


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

    # Made-up assets, such as tools/fill.py's, so they are easy to find and remove.
    if asset.get("sample") is True:
        add_tag(SAMPLE_TAG)

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


def audit_log_path(name: str) -> str:
    return os.path.join(LOG_FOLDER, name + "-audit.log")


# The audit log records who changed what, one JSON object per line, in its own file beside
# each program's normal log. It is kept apart so that it holds only these records, in a
# form a program can read back, and isn't mixed in with everyday messages.
audit_logger = logging.getLogger("audit")
audit_logger.propagate = False  # never mixed into the normal log


def utc_now() -> str:
    """The current time in UTC, in ISO 8601 form, such as 2026-11-05T21:14:03Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def audit(action: str, **details) -> None:
    """Add one record to this program's audit log, stamped with the time.

    The record is written as JSON, so a value someone typed, even one with a line break,
    stays inside its own record and can't fake another.
    """
    audit_logger.info(json.dumps({"time": utc_now(), "action": action, **details}))


def configure_logging(name: str) -> None:
    """Send this program's log to the console and to its own file, logs/<name>.log, and
    its audit records to logs/<name>-audit.log.

    Each program has its own file because they are separate processes. Two processes
    writing to one file can mix up each other's lines, and can't safely take turns
    renaming it when it fills up. Each line is stamped with the time, the level, the
    program and the thread, so files from different programs can be lined up.

    Passwords and the shared secret are never logged. If the file can't be opened, the
    program still runs, logging to the console only.
    """
    handlers = [logging.StreamHandler()]
    audit_handlers = []
    problem = None
    try:
        os.makedirs(LOG_FOLDER, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_path(name), maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_COPIES, encoding="utf-8"
        ))
        audit_handlers.append(logging.handlers.RotatingFileHandler(
            audit_log_path(name), maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_COPIES, encoding="utf-8"
        ))
    except OSError as exc:
        problem = exc

    # Audit records go only to the audit file, each line exactly as audit() wrote it.
    for handler in audit_logger.handlers:
        handler.close()
    audit_logger.handlers[:] = audit_handlers
    for handler in audit_handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False

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
        make_folder_for(SECRET_FILE)
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


def new_tls_pem() -> bytes:
    """Create a new private key and a self-signed certificate for it, as PEM text.

    The cryptography package is only needed here, so a machine that was given a copy of
    tls.pem doesn't need it installed. The certificate names "localhost" and 127.0.0.1,
    but clients don't check names, because they trust only this exact certificate, and
    the servers may be reached by any name.
    """
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Asset Management System")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed: it vouches for itself
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))  # allows for clocks that differ slightly
        .not_valid_after(now + datetime.timedelta(days=TLS_CERTIFICATE_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return private + certificate.public_bytes(serialization.Encoding.PEM)


def ensure_tls_file() -> str:
    """Return the path of the certificate file, creating it if this is the first program
    to run. Exclusive creation means that if the servers start at the same moment, only
    one creates it, and the others wait for it to be complete, so both use the same one."""
    make_folder_for(TLS_FILE)
    try:
        file = os.open(TLS_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):
            with open(TLS_FILE, "rb") as existing:
                if b"END CERTIFICATE" in existing.read():
                    return TLS_FILE
            time.sleep(0.1)
        raise ValueError(f"{TLS_FILE} has no certificate in it")
    with os.fdopen(file, "wb") as tls_file:
        tls_file.write(new_tls_pem())
    return TLS_FILE


def tls_context(server_side: bool) -> "ssl.SSLContext":
    """The TLS settings for one side of a connection, loaded once and then reused.

    A server presents the certificate. A client accepts only that same certificate, and
    skips the usual check of the server's name, because trusting one exact certificate
    is a stronger check, and the servers may be reached by any name.
    """
    import ssl

    context = _tls_contexts.get(server_side)
    if context is None:
        path = ensure_tls_file()
        if server_side:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(path)
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(path)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        _tls_contexts[server_side] = context
    return context


def forget_tls_settings() -> None:
    """Load the certificate again next time, for example after TLS_FILE changes."""
    _tls_contexts.clear()


def connect(address: tuple, timeout: float = TIMEOUT_SECONDS) -> socket.socket:
    """Open an encrypted connection to the server at `address`.

    Raises OSError if it can't be reached, or ssl.SSLError, which is also an OSError, if it
    doesn't have this system's certificate, so either counts as unreachable to callers.
    """
    # The servers only listen on IPv4, so a name is looked up as IPv4 only. Otherwise a
    # name with an IPv6 address too, such as "localhost", is tried over IPv6 first, and
    # on Windows each failed attempt takes 2 seconds, longer than a heartbeat may take.
    # A name that can't be found raises socket.gaierror, an OSError like a refusal.
    host, port = address
    ipv4_address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]

    # create_connection() is used because it takes a timeout. Without one, a
    # stalled server would leave the program waiting forever.
    plain = socket.create_connection(ipv4_address, timeout=timeout)
    try:
        return tls_context(server_side=False).wrap_socket(plain, server_hostname=host)
    except BaseException:
        plain.close()
        raise


def request_from(address: tuple, request: dict, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Send one request to the server at `address` and return its reply.

    Every request carries the shared secret, because the server refuses any without it,
    and the version of the message format it uses. The connection is encrypted, so
    neither the secret nor the data can be read by anyone watching the network.
    """
    with connect(address, timeout) as sock:
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

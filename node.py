"""
The code shared by both servers: the stored data, the rules for changing it, and
the network loop that serves requests.

server.py (the primary) and backup.py (the backup) are separate programs, each run
as its own process with its own data file, possibly on different machines. This file
holds only what they have in common. What makes each one different is in its own file.

Requests from clients:

    {"action": "REGISTER", "request_id": "...", "asset": {...}}
    {"action": "LIST"}
    {"action": "SET_STATUS", "asset_id": "...", "status": "...", "expected_version": 1}
    {"action": "DELETE", "asset_id": "...", "expected_version": 1}

Every request also carries the shared secret in "auth", which common.request_from() adds.
Replies always contain "status", and "served_by" names the server that answered.
Failures look like this:

    {"status": "ERROR", "code": "NOT_FOUND", "detail": "...", "served_by": "primary"}
"""

import datetime   # the server stamps each new asset with today's date
import hmac       # compares the shared secret safely
import json      # the assets file uses JSON, the same format as the messages
import logging    # records what happens, including errors, without stopping
import os         # os.replace() and os.fsync() make saving safe
import socket     # provides the networking building blocks (sockets)
import sys        # sys.exit() reports startup failures
import threading  # gives each client a thread, and provides the lock

from common import (
    BAD_REQUEST,
    CONFLICT,
    DUPLICATE_ASSET,
    INTERNAL,
    NOT_FOUND,
    NOT_PRIMARY,
    STATUSES,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOO_LARGE,
    UNAUTHORIZED,
    UNSUPPORTED_VERSION,
    MessageTooLargeError,
    audit,
    configure_logging,
    generate_asset_tags,
    normalise_text,
    receive_message,
    send_message,
    shared_secret,
    tls_context,
)

# The fields a client must supply when registering an asset.
CLIENT_FIELDS = ("description", "category", "location", "status")

# The fields every stored asset must have.
STORED_FIELDS = ("asset_id",) + CLIENT_FIELDS

# Two assets with the same values in these fields count as the same item. Status is
# left out, because it describes the item's current use rather than which item it is.
IDENTITY_FIELDS = ("description", "category", "location")

# The fields a LIST search looks in.
SEARCH_FIELDS = ("asset_id", "description", "category", "location", "status")

# The longest value allowed in each field, in characters. Without a cap, a single
# huge asset would push every LIST reply over the message limit. It also keeps every
# REGISTER reply well under that limit, so a client always hears the result.
MAX_FIELD_LENGTH = 1000

# How long a server waits on each read from, or send to, a client before giving up on
# it. Without this, an idle client would hold a thread and a connection forever.
CLIENT_TIMEOUT_SECONDS = 10

# How long a server waits after a STOP request before ending, so its reply gets out,
# and the exit code it ends with, which tells start.py it didn't stop normally.
STOP_DELAY_SECONDS = 0.2
STOPPED_EXIT_CODE = 1

logger = logging.getLogger("asset_server")

# ---- What makes this node the primary or the backup, set by that program ----

ROLE = "primary"         # the name of this node, sent with every reply
ID_PREFIX = "AST-P"      # the range of asset IDs this node may issue
ASSETS_FILE = None       # this node's own data file
accepts_writes = True    # whether this node currently accepts changes from clients
on_change = None         # called with each change, while holding the lock
role_actions = {}        # requests only this role understands: {action: function}

# ---- The data ----

assets = []        # every registered asset, as a plain list of dicts

# The ID of every deleted asset, and the request that registered it, kept for good.
# This stops an ID being issued again, and stops a late retry restoring the asset.
deleted_ids = {}

# When each asset was deleted, by ID, as a UTC timestamp. It is kept apart from
# deleted_ids so data files from before timestamps were added still load unchanged.
# IDs deleted before then have no entry.
deleted_at = {}

# How many changes have been made. The primary numbers its changes in the one order
# they happened, and the backup applies them in that same order.
last_seq = 0

# One lock protects all of the above and the file, because every client has its own
# thread. Holding it makes "check, change, save, copy to the backup" one indivisible
# step, so simultaneous requests take effect strictly one after another.
assets_lock = threading.Lock()

# The request each thread is working on, so that the audit record of a change can say
# which request made it and for whom, without passing it through every function.
current = threading.local()

# The longest web username accepted in a request's "user" field.
MAX_USER_LENGTH = 64


def utc_timestamp() -> str:
    """The current time in UTC, in ISO 8601 form, such as 2026-11-05T21:14:03Z. UTC means
    times from servers in different time zones can be compared directly."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_data() -> dict:
    """Read the data file, returning its assets, deleted IDs, deletion times and last
    sequence number. It starts empty if the file does not exist yet.

    A corrupt file raises an error rather than starting empty, because the next
    save would then overwrite whatever could still be recovered.
    """
    try:
        with open(ASSETS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        data = []  # normal on the first run

    # Files written before delete was added hold just the list of assets.
    if isinstance(data, list):
        return {"assets": data, "deleted": {}, "deleted_at": {}, "seq": 0}

    if (
        not isinstance(data, dict)
        or not isinstance(data.get("assets"), list)
        or not isinstance(data.get("deleted"), dict)
        or not isinstance(data.get("seq"), int)
        # Files written before timestamps were added have no deletion times.
        or not isinstance(data.get("deleted_at", {}), dict)
    ):
        raise ValueError(f"{ASSETS_FILE} does not contain valid asset data")
    return {"assets": data["assets"], "deleted": data["deleted"],
            "deleted_at": data.get("deleted_at", {}), "seq": data["seq"]}


def load_assets() -> tuple:
    """Read the data file, returning the assets, deleted IDs and last sequence number."""
    data = load_data()
    return data["assets"], data["deleted"], data["seq"]


def save_assets() -> None:
    """Write all the data to disk safely. The caller must hold the lock.

    Opening the real file for writing would empty it immediately, so a crash
    part-way through would lose everything. Instead, the data goes into a
    temporary file, and os.replace() swaps it into place in one atomic step. The
    file on disk is therefore always a complete old or complete new version.
    """
    temp_path = ASSETS_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump({"seq": last_seq, "assets": assets, "deleted": deleted_ids, "deleted_at": deleted_at}, file, indent=2)

        # flush() and fsync() push the data out of memory buffers and onto the
        # disk, so it survives a power cut as well as a program crash.
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_path, ASSETS_FILE)
    flush_data_folder()


def flush_data_folder() -> None:
    """Force the swap made by os.replace() onto the disk, on Linux and macOS.

    There, the swap is recorded in the folder, so a power cut could undo it unless
    the folder is flushed too. Windows does not allow a folder to be opened this way.

    A failure is only logged, because some file systems do not support this. The
    new file is already in place by now, so reporting a failed save would be wrong.
    """
    if os.name != "posix":
        return

    try:
        folder = os.open(os.path.dirname(ASSETS_FILE), os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except OSError as exc:
        logger.warning("Could not flush the data folder: %s", exc)


def error_reply(code: str, detail: str, **extra) -> dict:
    """Build an ERROR reply in the one format every client relies on."""
    return {"status": "ERROR", "code": code, "detail": detail, **extra}


def not_primary_reply() -> dict:
    return error_reply(NOT_PRIMARY, f"The {ROLE} server is not accepting changes right now")


def check_text(value: object, name: str) -> str:
    """Return `value` without surrounding spaces, or raise ValueError if it is not
    an acceptable field value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Field must be a non-empty string: {name}")
    if len(value.strip()) > MAX_FIELD_LENGTH:
        raise ValueError(f"Field must be at most {MAX_FIELD_LENGTH} characters: {name}")
    return value.strip()


def check_status(value: object) -> str:
    """Return a valid status, or raise ValueError if it isn't one of STATUSES."""
    status = check_text(value, "status")
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    return status


def check_expected_version(request: dict) -> object:
    """Return a request's expected_version, or None if it has none."""
    expected = request.get("expected_version")
    if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
        raise ValueError("expected_version must be a whole number")
    return expected


def identity_of(asset: dict) -> tuple:
    """Return what makes an asset the same item as another, ignoring differences in
    capitals, spacing and punctuation."""
    return tuple(normalise_text(asset[field]) for field in IDENTITY_FIELDS)


def validate_asset(asset: object) -> dict:
    """Return a clean copy of a client's asset with its date added, or raise
    ValueError if it is invalid.

    This happens on the server even if a client also checks, because anyone can
    write a program that speaks this protocol. Only the server can guarantee it.
    """
    if not isinstance(asset, dict):
        raise ValueError("'asset' must be an object")

    # Copying only the expected fields means any extra fields are dropped, including
    # any asset_id the client sent.
    clean = {field: check_text(asset.get(field), field) for field in CLIENT_FIELDS}
    clean["status"] = check_status(clean["status"])

    # The date and times come from the server, and any the client sent are ignored,
    # because client clocks can be wrong or in other time zones.
    clean["date_registered"] = datetime.date.today().isoformat()
    return clean


def find_asset(asset_id: str) -> object:
    """Return the position of an asset in `assets`, or None. The caller must hold
    the lock."""
    for index, asset in enumerate(assets):
        if asset["asset_id"] == asset_id:
            return index
    return None


def generate_asset_id() -> str:
    """Return an asset ID that has never been used. The caller must hold the lock.

    Each candidate is checked against every stored and deleted ID, and is only issued
    if it is unused. Holding the lock means nothing can take it before it is stored.

    Each server issues IDs from its own range (AST-P... or AST-B...). The other server
    may not be reachable to ask, but this means it can never issue the same ID.
    """
    used = {asset["asset_id"] for asset in assets} | set(deleted_ids)
    number = len(used) + 1
    while True:
        candidate = f"{ID_PREFIX}{number:06d}"
        if candidate not in used:
            return candidate
        number += 1


def commit_change(change: dict, undo) -> None:
    """Number a change that was just made in memory, save it, and pass it on. The
    caller must hold the lock.

    If the save fails, `undo` puts memory back, so memory and disk still agree. The
    client will be told this failed, so the change must not appear in later replies.
    """
    global last_seq

    last_seq += 1
    try:
        save_assets()
    except OSError:
        last_seq -= 1
        undo()
        raise

    # Recorded once the change is safely saved, so the audit log never lists a change
    # that didn't happen. Changes copied from the primary aren't recorded again here.
    request = getattr(current, "request", None) or {}
    audit(
        request.get("action", "CHANGE"),
        asset_id=change["asset"]["asset_id"] if "asset" in change else change.get("deleted"),
        request_id=request.get("request_id"),
        user=request.get("user"),
        server=ROLE,
        seq=last_seq,
    )

    if on_change is not None:
        on_change(dict(change, seq=last_seq))


def register_asset(clean: dict, request_id: object) -> dict:
    """Store a validated asset under a new ID and return the reply.

    Together with validate_asset() and save_assets(), this gives the ACID
    properties for each change:
      Atomicity:   it is stored and saved, or not stored at all (the undo).
      Consistency: only valid assets with unique IDs and identities are ever stored.
      Isolation:   the lock stops simultaneous requests interfering.
      Durability:  the reply is only sent after the save has reached the disk.

    Two checks stop duplicates. A repeat of the same request (same request_id) is a
    client retrying, so it gets the asset already stored. A different request for the
    same item is rejected as a duplicate.
    """
    with assets_lock:
        if not accepts_writes:
            return not_primary_reply()

        identity = identity_of(clean)

        if request_id is not None:
            if request_id in deleted_ids.values():
                return error_reply(BAD_REQUEST, "This registration's asset has since been deleted")
            for existing in assets:
                if existing.get("request_id") == request_id:
                    if identity_of(existing) != identity:
                        return error_reply(BAD_REQUEST, "This request_id was already used for a different asset")
                    return {"status": "OK", "stored": existing}

        for existing in assets:
            if identity_of(existing) == identity:
                return error_reply(
                    DUPLICATE_ASSET,
                    f"This item is already registered as {existing['asset_id']}",
                    existing_id=existing["asset_id"],
                )

        now = utc_timestamp()
        asset = dict(clean, asset_id=generate_asset_id(), request_id=request_id, version=1,
                     created_at=now, updated_at=now)
        asset["tags"] = generate_asset_tags(asset)

        assets.append(asset)
        commit_change({"asset": asset}, assets.pop)

    # Echo back what was stored, because the server changed it (trimmed spaces,
    # dropped extra fields, added the ID, date, tags and version).
    return {"status": "OK", "stored": asset}


def version_conflict(asset: dict, expected_version: object) -> object:
    """Return a CONFLICT reply if the asset has changed since the client read it.

    This is optimistic concurrency control. Without it, two people looking at the
    same asset could each make a change, and the second would silently overwrite the
    first without its author ever having seen it.
    """
    if expected_version is None or expected_version == asset.get("version", 0):
        return None
    return error_reply(CONFLICT, f"{asset['asset_id']} was changed by someone else. Check it and try again.")


def set_asset_status(asset_id: str, status: str, expected_version: object = None) -> dict:
    """Change an asset's status and return the reply.

    The tags include the status, so they are regenerated. A new record replaces the
    old one rather than the old one being edited, because a LIST reply may be being
    sent from the old one on another thread, outside the lock.
    """
    with assets_lock:
        if not accepts_writes:
            return not_primary_reply()

        index = find_asset(asset_id)
        if index is None:
            return error_reply(NOT_FOUND, f"No asset has the ID {asset_id!r}")

        previous = assets[index]
        if previous["status"] == status:
            return {"status": "OK", "stored": previous}  # nothing to change

        conflict = version_conflict(previous, expected_version)
        if conflict:
            return conflict

        updated = dict(previous, status=status, version=previous.get("version", 0) + 1, updated_at=utc_timestamp())
        updated["tags"] = generate_asset_tags(updated)

        assets[index] = updated
        commit_change({"asset": updated}, lambda: assets.__setitem__(index, previous))

    return {"status": "OK", "stored": updated}


def check_changes(changes: object) -> dict:
    """Return a clean copy of an EDIT's changes, or raise ValueError if they are invalid.
    Only the fields a client registers can be changed, and each is checked as it would be
    on registration."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("'changes' must be an object with at least one field to change")
    unknown = sorted(set(changes) - set(CLIENT_FIELDS))
    if unknown:
        raise ValueError(f"These fields can't be changed: {', '.join(unknown)}. "
                         f"Only these can: {', '.join(CLIENT_FIELDS)}")
    clean = {field: check_text(value, field) for field, value in changes.items()}
    if "status" in clean:
        clean["status"] = check_status(clean["status"])
    return clean


def edit_asset(asset_id: str, changes: dict, expected_version: object = None) -> dict:
    """Change some of an asset's fields and return the reply, with the updated record.

    Like a status change, a new record replaces the old one, and asking for what is
    already true changes nothing. The changed asset must not become a duplicate of
    another one, just as a new asset mustn't.
    """
    with assets_lock:
        if not accepts_writes:
            return not_primary_reply()

        index = find_asset(asset_id)
        if index is None:
            return error_reply(NOT_FOUND, f"No asset has the ID {asset_id!r}")

        previous = assets[index]
        if all(previous.get(field) == value for field, value in changes.items()):
            return {"status": "OK", "stored": previous}  # nothing to change

        conflict = version_conflict(previous, expected_version)
        if conflict:
            return conflict

        updated = dict(previous, **changes)
        identity = identity_of(updated)
        for other in assets:
            if other is not previous and identity_of(other) == identity:
                return error_reply(
                    DUPLICATE_ASSET,
                    f"This would make it the same item as {other['asset_id']}",
                    existing_id=other["asset_id"],
                )

        updated["version"] = previous.get("version", 0) + 1
        updated["updated_at"] = utc_timestamp()
        updated["tags"] = generate_asset_tags(updated)

        assets[index] = updated
        commit_change({"asset": updated}, lambda: assets.__setitem__(index, previous))

    return {"status": "OK", "stored": updated}


def delete_asset(asset_id: str, expected_version: object = None) -> dict:
    """Remove an asset for good and return the reply.

    Deleting something already deleted counts as success. A client whose reply was
    lost may send this again, and what it asked for is true.
    """
    with assets_lock:
        if not accepts_writes:
            return not_primary_reply()

        if asset_id in deleted_ids:
            return {"status": "OK", "deleted": asset_id}

        index = find_asset(asset_id)
        if index is None:
            return error_reply(NOT_FOUND, f"No asset has the ID {asset_id!r}")

        conflict = version_conflict(assets[index], expected_version)
        if conflict:
            return conflict

        previous = assets.pop(index)
        request_id = previous.get("request_id")
        deleted_ids[asset_id] = request_id
        deleted_at[asset_id] = utc_timestamp()

        def undo() -> None:
            assets.insert(index, previous)
            del deleted_ids[asset_id]
            del deleted_at[asset_id]

        commit_change({"deleted": asset_id, "request_id": request_id, "deleted_at": deleted_at[asset_id]}, undo)

    return {"status": "OK", "deleted": asset_id}


def check_search(request: dict) -> dict:
    """Return a LIST request's search options, leaving out any it didn't send, or raise
    ValueError if one is invalid."""
    options = {}
    for name in ("query", "status"):
        if request.get(name) is not None:
            if not isinstance(request[name], str) or len(request[name]) > MAX_FIELD_LENGTH:
                raise ValueError(f"{name} must be text of at most {MAX_FIELD_LENGTH} characters")
            options[name] = request[name]
    for name, smallest in (("offset", 0), ("limit", 1)):
        value = request.get(name)
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value < smallest:
                raise ValueError(f"{name} must be a whole number of at least {smallest}")
            options[name] = value
    return options


def search_assets(candidates: list, query: str = "", status: str = None, offset: int = 0, limit: int = None) -> dict:
    """Return one page of the assets that match, and how many match in total.

    `query` is found anywhere in the ID, description, category, location or status,
    ignoring capitals and surrounding spaces. `status` must match exactly. With no
    options, every asset is returned, in the order they are stored, as LIST always has.
    """
    wanted = query.strip().casefold()
    matches = [
        asset for asset in candidates
        if (status is None or asset.get("status") == status)
        and (not wanted or any(wanted in str(asset.get(field, "")).casefold() for field in SEARCH_FIELDS))
    ]
    page = matches[offset:] if limit is None else matches[offset:offset + limit]
    return {"assets": page, "total": len(matches)}


def process_request(request: dict) -> dict:
    """Carry out one decoded request and return the reply.

    This does no networking, so the rules can be tested without a socket.
    """
    action = request.get("action")

    if action == "LIST":
        try:
            options = check_search(request)
        except ValueError as exc:
            return error_reply(BAD_REQUEST, str(exc))

        # Copy the list while holding the lock. It is searched and sent after the lock
        # is released, so one slow client cannot hold up everyone else.
        with assets_lock:
            snapshot = list(assets)
        return {"status": "OK", **search_assets(snapshot, **options)}

    if action == "PING":
        # The heartbeat between servers. It takes no lock, so a server that is busy
        # saving is not mistaken for one that has failed.
        return {"status": "OK", "accepts_writes": accepts_writes}

    if action == "STOP":
        # Used by demo.py to show a failover. The server ends abruptly, without telling
        # the other server or finishing anything, exactly like a crash or a power cut.
        # The short delay lets this reply reach the sender first.
        logger.warning("Received STOP, so stopping now, as if this server had crashed.")
        threading.Timer(STOP_DELAY_SECONDS, os._exit, args=(STOPPED_EXIT_CODE,)).start()
        return {"status": "OK"}

    if action in role_actions:
        return role_actions[action](request)

    # "user" names the web user a change is made for, for the audit log. It is optional,
    # because the GUI and command-line clients have no login.
    user = request.get("user")
    if user is not None and (not isinstance(user, str) or not user.strip() or len(user) > MAX_USER_LENGTH):
        return error_reply(BAD_REQUEST, f"user must be non-empty text of at most {MAX_USER_LENGTH} characters")
    current.request = request

    try:
        if action == "REGISTER":
            clean = validate_asset(request.get("asset"))
            request_id = request.get("request_id")
            if request_id is not None:
                request_id = check_text(request_id, "request_id")
            return register_asset(clean, request_id)

        if action == "SET_STATUS":
            asset_id = check_text(request.get("asset_id"), "asset_id")
            status = check_status(request.get("status"))
            return set_asset_status(asset_id, status, check_expected_version(request))

        if action == "EDIT":
            asset_id = check_text(request.get("asset_id"), "asset_id")
            changes = check_changes(request.get("changes"))
            return edit_asset(asset_id, changes, check_expected_version(request))

        if action == "DELETE":
            asset_id = check_text(request.get("asset_id"), "asset_id")
            return delete_asset(asset_id, check_expected_version(request))
    except ValueError as exc:
        return error_reply(BAD_REQUEST, str(exc))
    finally:
        current.request = None  # so nothing done later on this thread is credited to it

    return error_reply(BAD_REQUEST, f"Unknown action: {action!r}")


def is_authorized(request: dict) -> bool:
    """Check that a request carries the shared secret.

    compare_digest() takes the same time wherever the values differ, so timing replies
    cannot reveal the secret one character at a time.
    """
    sent = request.get("auth")
    if not isinstance(sent, str):
        return False
    return hmac.compare_digest(sent.encode("utf-8", "surrogatepass"), shared_secret().encode("utf-8"))


def read_and_process(conn: socket.socket) -> dict:
    """Read one request and work out the reply. Connection failures are left to
    the caller, because no reply can be sent over a failed connection."""
    try:
        request = receive_message(conn)
    except MessageTooLargeError as exc:
        return error_reply(TOO_LARGE, str(exc))
    except (ValueError, RecursionError) as exc:
        # Invalid text, invalid JSON, JSON that is not an object, or JSON so
        # deeply nested that it exhausts the parser. The connection still works,
        # so the client can be told.
        return error_reply(BAD_REQUEST, f"Invalid message: {exc}")

    if not is_authorized(request):
        return error_reply(UNAUTHORIZED, "This program does not have the system's secret.key file")

    # Checked after the secret, so only programs of this system learn which versions exist.
    version = request.get("protocol_version")
    if isinstance(version, bool) or version not in SUPPORTED_PROTOCOL_VERSIONS:
        return error_reply(
            UNSUPPORTED_VERSION,
            f"protocol_version {version!r} is not supported. This server supports: "
            f"{', '.join(map(str, SUPPORTED_PROTOCOL_VERSIONS))}",
            supported_versions=list(SUPPORTED_PROTOCOL_VERSIONS),
        )

    try:
        return process_request(request)
    except Exception:
        # A bug, or a failure such as a full disk. Log the details for whoever
        # runs the server, and tell the client only that it failed, because
        # internal details can help an attacker.
        logger.exception("Could not complete a request")
        return error_reply(INTERNAL, "The server could not complete the request")


def handle_client(conn: socket.socket, addr: tuple) -> None:
    """Serve one client. Runs on that client's own thread.

    Nothing a client does may affect the rest of the server, so every exception
    stops here. Clients fail independently of the server, so one client's failure
    must stay that client's problem.
    """
    try:
        conn.settimeout(CLIENT_TIMEOUT_SECONDS)
        try:
            # Everything is encrypted. A program that doesn't speak TLS, or a scanner,
            # fails here, before a single byte of a request has been read.
            conn = tls_context(server_side=True).wrap_socket(conn, server_side=True)
        except (OSError, ValueError) as exc:
            conn.close()
            logger.warning("Refused a connection from %s that failed the TLS handshake: %s", addr, exc)
            return

        # `with conn:` closes the connection when this block ends, even on error.
        with conn:
            reply = read_and_process(conn)
            reply["served_by"] = ROLE
            try:
                send_message(conn, reply)
            except MessageTooLargeError as exc:
                # A LIST of too many assets. send_message() checks the size before
                # sending anything, so the client can still be told what happened,
                # rather than seeing a dropped connection that looks like an outage.
                send_message(conn, error_reply(TOO_LARGE, f"The reply is too large to send. {exc}", served_by=ROLE))
    except OSError as exc:
        # The client disconnected or timed out. Nobody is left to read a reply,
        # so all that remains is to record it. (Trying to reply on the dead
        # connection is what used to crash the server.)
        logger.warning("Lost connection to %s: %s", addr, exc)
    except Exception:
        logger.exception("Unexpected error serving %s", addr)


def create_listening_socket(host: str, port: int) -> socket.socket:
    """Create the TCP/IPv4 socket that waits for new connections."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # SO_REUSEADDR allows a quick restart without the port being reported as
    # "in use". On Windows it would also let two servers share a port without
    # any error, so SO_EXCLUSIVEADDRUSE is used there instead.
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    server_sock.bind((host, port))
    server_sock.listen()
    return server_sock


def run_server(server_sock: socket.socket) -> None:
    """Accept clients forever, giving each one its own thread."""
    # accept() normally waits forever, and on Windows it then does not notice
    # Ctrl+C. Waking up every second lets the interrupt get through.
    server_sock.settimeout(1)

    while True:
        try:
            conn, addr = server_sock.accept()
        except OSError:
            # Usually the one-second timeout above. It can also be a connection
            # that failed while arriving. Neither stops the server.
            continue

        # Debug level, because the other server connects every second.
        logger.debug("Connection from %s", addr)

        # A separate thread means a slow client does not delay the others.
        # daemon=True means these threads do not stop the program from exiting.
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


def listening_address(address: tuple) -> tuple:
    """Return the address this server should listen on.

    Normally that is its own entry in SERVERS. Another machine may reach it by a name
    that this machine can't listen on, such as a name that points at a router or a
    container. Setting ASSET_LISTEN_HOST=0.0.0.0 then listens on every network
    connection this machine has, on the same port.
    """
    return (os.environ.get("ASSET_LISTEN_HOST") or address[0], address[1])


def start_up(address: tuple) -> socket.socket:
    """Set up logging, load this node's data and open its port.

    The server cannot work without its data and its port, so problems with either
    end the program, with a clear message rather than a traceback.
    """
    global last_seq

    # The log goes to the console and to logs/primary.log or logs/backup.log. The thread
    # name in each line makes the concurrent handling visible.
    configure_logging(ROLE)

    try:
        data = load_data()
        assets.extend(data["assets"])
        deleted_ids.update(data["deleted"])
        deleted_at.update(data["deleted_at"])
        last_seq = data["seq"]
        address = listening_address(address)
        server_sock = create_listening_socket(*address)
    except (OSError, ValueError) as exc:
        logger.error("Could not start the server: %s", exc)
        logger.error("Check that %s is valid and that no other copy of the server is running.", ASSETS_FILE)
        sys.exit(1)

    logger.info("Asset Management %s server listening on %s:%s", ROLE, *address)
    logger.info("Press Ctrl+C to stop.")
    return server_sock

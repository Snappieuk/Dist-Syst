"""
Asset Management System server.

Each connection carries one request and one reply:

    {"action": "REGISTER", "request_id": "...", "asset": {...}}
                                             -> validates and stores the asset, creating its ID
    {"action": "LIST"}                       -> replies with every stored asset

Replies always contain "status". Failures look like this:

    {"status": "ERROR", "code": "BAD_REQUEST", "detail": "..."}

Each client is handled on its own thread, and assets are saved to a JSON file
after every change, so they survive a crash or restart.

For redundancy, a second copy can run as a backup:

    python server.py            (the primary)
    python server.py --backup   (the backup)

Clients use the backup if the primary is down, and the two servers regularly copy
new assets from each other.
"""

import datetime   # the server stamps each new asset with today's date
import json       # the assets file uses JSON, the same format as the messages
import logging    # records what happens, including errors, without stopping
import os         # os.path finds the data files; os.replace() and os.fsync() make saving safe
import socket     # provides the networking building blocks (sockets)
import sys        # sys.argv holds the --backup option; sys.exit() reports startup failures
import threading  # gives each client a thread, and provides the lock
import time       # waits between syncs with the other server

# Import the shared constants/helpers used by every part of the prototype.
from common import (
    BAD_REQUEST,
    INTERNAL,
    SERVERS,
    TOO_LARGE,
    MessageTooLargeError,
    generate_asset_tags,
    receive_message,
    request_from,
    send_message,
)

# Stored next to this script, so the server finds the same data whichever
# folder it is started from.
ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets.json")

# The backup keeps its own copy, so the two servers can run from the same folder.
BACKUP_ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_backup.json")

# The fields a client must supply when registering an asset.
CLIENT_FIELDS = ("description", "category", "location", "status")

# The fields every stored asset must have.
STORED_FIELDS = ("asset_id",) + CLIENT_FIELDS

# The range of asset IDs this server may issue. The backup has its own.
ID_PREFIX = "AST-P"

# The longest value allowed in each of those fields, in characters. There is no
# delete, so without a cap a single huge asset would push every LIST reply over
# the message limit for good. It also keeps every REGISTER reply well under that
# limit, so a client always hears the result of a registration that was saved.
MAX_FIELD_LENGTH = 1000

# How long the server waits on each read from, or send to, a client before giving
# up on it. Without this, an idle client would hold a thread and a connection forever.
CLIENT_TIMEOUT_SECONDS = 10

# How often each server copies new assets from the other one.
SYNC_INTERVAL_SECONDS = 2

logger = logging.getLogger("asset_server")

assets = []  # every registered asset, as a plain list of dicts

# One lock protects `assets` and the file, because every client has its own
# thread. Without it, two clients could both see that an ID is free and both
# store it. Holding the lock makes "check, store, save" one indivisible step.
assets_lock = threading.Lock()


def load_assets() -> list:
    """Read the assets file, or start empty if it does not exist yet.

    A corrupt file raises an error rather than starting empty, because the next
    save would then overwrite whatever could still be recovered.
    """
    try:
        with open(ASSETS_FILE, "r", encoding="utf-8") as file:
            records = json.load(file)
    except FileNotFoundError:
        return []  # normal on the first run

    if not isinstance(records, list):
        raise ValueError(f"{ASSETS_FILE} does not contain a list of assets")
    return records


def save_assets() -> None:
    """Write every asset to disk safely. The caller must hold the lock.

    Opening the real file for writing would empty it immediately, so a crash
    part-way through would lose everything. Instead, the data goes into a
    temporary file, and os.replace() swaps it into place in one atomic step. The
    file on disk is therefore always a complete old or complete new version.
    """
    temp_path = ASSETS_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(assets, file, indent=2)

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


def error_reply(code: str, detail: str) -> dict:
    """Build an ERROR reply in the one format every client relies on."""
    return {"status": "ERROR", "code": code, "detail": detail}


def check_text(value: object, name: str) -> str:
    """Return `value` without surrounding spaces, or raise ValueError if it is not
    an acceptable field value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Field must be a non-empty string: {name}")
    if len(value.strip()) > MAX_FIELD_LENGTH:
        raise ValueError(f"Field must be at most {MAX_FIELD_LENGTH} characters: {name}")
    return value.strip()


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
    clean = {}
    for field in CLIENT_FIELDS:
        clean[field] = check_text(asset.get(field), field)

    # The date comes from the server, and anything the client sent is ignored,
    # because client clocks can be wrong or in other time zones.
    clean["date_registered"] = datetime.date.today().isoformat()
    return clean


def generate_asset_id() -> str:
    """Return an asset ID that has never been used. The caller must hold the lock.

    Each candidate is checked against every stored ID, and is only issued if it is
    unused. Holding the lock means nothing can take it before it is stored.

    Each server issues IDs from its own range (AST-P... or AST-B...). The other server
    may not be reachable to ask, but this means it can never issue the same ID.
    """
    used = {asset["asset_id"] for asset in assets}
    number = len(used) + 1
    while True:
        candidate = f"{ID_PREFIX}{number:06d}"
        if candidate not in used:
            return candidate
        number += 1


def register_asset(clean: dict, request_id: object) -> dict:
    """Store a validated asset under a new ID and return the reply.

    Together with validate_asset() and save_assets(), this gives the ACID
    properties for each registration:
      Atomicity:   it is stored and saved, or not stored at all (the undo below).
      Consistency: only valid assets with unique IDs are ever stored.
      Isolation:   the lock stops concurrent registrations interfering.
      Durability:  the reply is only sent after the save has reached the disk.

    A repeat of the same request is a client retrying, so it gets the asset already
    stored rather than a second copy.
    """
    with assets_lock:
        if request_id is not None:
            for existing in assets:
                if existing.get("request_id") == request_id:
                    if any(existing[field] != clean[field] for field in ("description", "category", "location")):
                        return error_reply(BAD_REQUEST, "This request_id was already used for a different asset")
                    return {"status": "OK", "stored": existing}

        asset = dict(clean, asset_id=generate_asset_id(), request_id=request_id)

        # Tags come from the server too, so every client's assets are tagged the same way.
        asset["tags"] = generate_asset_tags(asset)

        assets.append(asset)
        try:
            save_assets()
        except OSError:
            # Undo the change, so memory and disk still agree. The client will be
            # told this failed, so the asset must not appear in later listings.
            assets.pop()
            raise

    # Echo back what was stored, because the server changed it (trimmed spaces,
    # dropped extra fields, added the ID, date and tags).
    return {"status": "OK", "stored": asset}


def process_request(request: dict) -> dict:
    """Carry out one decoded request and return the reply.

    This does no networking, so the rules can be tested without a socket.
    """
    action = request.get("action")

    if action == "REGISTER":
        try:
            clean = validate_asset(request.get("asset"))
            request_id = request.get("request_id")
            if request_id is not None:
                request_id = check_text(request_id, "request_id")
        except ValueError as exc:
            return error_reply(BAD_REQUEST, str(exc))
        return register_asset(clean, request_id)

    if action == "LIST":
        # Copy the list while holding the lock. It is sent after the lock is
        # released, so one slow client cannot hold up everyone else.
        with assets_lock:
            return {"status": "OK", "assets": list(assets)}

    return error_reply(BAD_REQUEST, f"Unknown action: {action!r}")


def is_valid_peer_asset(record: object) -> bool:
    """Check a record from the other server before it is stored.

    Records saved before tagging was added have no tags, so tags and the date are
    optional, but they must have the right type if present.
    """
    if not isinstance(record, dict):
        return False

    try:
        for field in STORED_FIELDS:
            check_text(record.get(field), field)
    except ValueError:
        return False

    tags = record.get("tags", [])
    return (
        isinstance(tags, list)
        and all(isinstance(tag, str) for tag in tags)
        and isinstance(record.get("date_registered", ""), str)
    )


def merge_assets(peer_assets: list, prefer_peer: bool) -> int:
    """Add the other server's assets to this one's, returning how many changed.

    Clients can only add assets, never change or remove them, so combining two lists
    is simple: anything missing is copied across. The one awkward case is when each
    server accepted a different asset with the same ID while they could not reach
    each other. Then the backup gives way to the primary (`prefer_peer` is True on
    the backup), so both end up with the same records, possibly in a different order.

    The caller must pass only valid records (see is_valid_peer_asset).
    """
    with assets_lock:
        # A dict keeps its order and makes each asset quick to find by ID.
        merged = {asset["asset_id"]: asset for asset in assets}

        changed = 0
        for peer_asset in peer_assets:
            my_asset = merged.get(peer_asset["asset_id"])
            if my_asset is None or (prefer_peer and my_asset != peer_asset):
                merged[peer_asset["asset_id"]] = peer_asset
                changed += 1

        if changed:
            previous = list(assets)
            assets[:] = merged.values()
            try:
                save_assets()
            except OSError:
                # Keep memory and disk in agreement, as in register_asset(). The
                # next sync will simply try again.
                assets[:] = previous
                raise

        return changed


def sync_once(peer: tuple, prefer_peer: bool) -> tuple:
    """Copy assets from the other server once, returning how many records changed
    and how many invalid ones were skipped.

    An ERROR reply raises ValueError rather than counting as an empty list, so a
    sync that is failing does not look like there is simply nothing to copy. One bad
    record is skipped, so that it does not stop all the others being copied.
    """
    reply = request_from(peer, {"action": "LIST"})
    if reply.get("status") != "OK":
        raise ValueError(f"It replied with an error: {reply.get('detail')}")

    records = reply.get("assets")
    if not isinstance(records, list):
        raise ValueError("Its reply has no list of assets")

    valid_records = [record for record in records if is_valid_peer_asset(record)]
    return merge_assets(valid_records, prefer_peer), len(records) - len(valid_records)


def sync_with_peer(peer: tuple, prefer_peer: bool) -> None:
    """Keep copying assets from the other server. Runs on its own thread forever.

    WHY THE SERVER ASKS, RATHER THAN BEING TOLD: each server simply fetches the
    other's list. A server that was down catches up by itself when it restarts, and
    a server whose partner is down carries on without having to wait for it.

    The trade-off is that the two can differ for a couple of seconds. This is called
    eventual consistency. An asset is safe in the file of the server that accepted
    it, and reaches the other server on the next sync after both are running.

    This repeats every couple of seconds, so a problem is only logged when it first
    appears or changes, not on every attempt.
    """
    last_problem = None
    last_skipped = 0

    while True:
        problem = None
        try:
            changed, skipped = sync_once(peer, prefer_peer)
            if changed:
                logger.info("Copied %s asset(s) from the other server", changed)
            if skipped and skipped != last_skipped:
                logger.warning("Skipped %s invalid record(s) from the other server", skipped)
            last_skipped = skipped
            if last_problem:
                logger.info("Syncing with the other server again")
        except Exception as exc:
            # Nothing may stop the syncing. An OSError means the other server is
            # unreachable or a save failed, and a ValueError means its reply was
            # unusable. Anything else is a bug, so its traceback is logged too.
            problem = str(exc)
            if problem != last_problem:
                logger.warning(
                    "Could not sync with the other server: %s",
                    problem,
                    exc_info=not isinstance(exc, (OSError, ValueError)),
                )
        last_problem = problem

        time.sleep(SYNC_INTERVAL_SECONDS)


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
        # `with conn:` closes the connection when this block ends, even on error.
        with conn:
            conn.settimeout(CLIENT_TIMEOUT_SECONDS)
            reply = read_and_process(conn)
            try:
                send_message(conn, reply)
            except MessageTooLargeError as exc:
                # A LIST of too many assets. send_message() checks the size before
                # sending anything, so the client can still be told what happened,
                # rather than seeing a dropped connection that looks like an outage.
                send_message(conn, error_reply(TOO_LARGE, f"The reply is too large to send. {exc}"))
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

        # Debug level, because the other server connects every couple of seconds.
        logger.debug("Connection from %s", addr)

        # A separate thread means a slow client does not delay the others.
        # daemon=True means these threads do not stop the program from exiting.
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


def main() -> None:
    global ASSETS_FILE, ID_PREFIX

    # The primary and backup are the same program. This option only decides which
    # address and data file it uses, which server it syncs with, and that the
    # backup is the one to give way in a conflict.
    is_backup = "--backup" in sys.argv
    address = SERVERS[1] if is_backup else SERVERS[0]
    peer = SERVERS[0] if is_backup else SERVERS[1]
    if is_backup:
        ASSETS_FILE = BACKUP_ASSETS_FILE
        ID_PREFIX = "AST-B"

    logging.basicConfig(
        level=logging.INFO,
        # threadName in the log makes the concurrent handling visible.
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )

    # The server cannot work without its data and its port, so problems with
    # either end the program, with a clear message rather than a traceback.
    try:
        assets.extend(load_assets())
        server_sock = create_listening_socket(*address)
    except (OSError, ValueError) as exc:
        logger.error("Could not start the server: %s", exc)
        logger.error("Check that %s is valid and that no other copy of the server is running.", ASSETS_FILE)
        sys.exit(1)

    with server_sock:
        role = "backup" if is_backup else "primary"
        logger.info("Asset Management %s server listening on %s:%s", role, *address)
        logger.info("Press Ctrl+C to stop.")

        # daemon=True means this thread does not stop the program from exiting.
        threading.Thread(target=sync_with_peer, args=(peer, is_backup), daemon=True).start()

        try:
            run_server(server_sock)
        except KeyboardInterrupt:
            logger.info("Server stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

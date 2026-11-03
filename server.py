"""
Asset Management System server.

Each connection carries one request and one reply:

    {"action": "REGISTER", "asset": {...}}   -> validates and stores the asset
    {"action": "LIST"}                       -> replies with every stored asset

Replies always contain "status". Failures look like this:

    {"status": "ERROR", "code": "DUPLICATE_ID", "detail": "..."}

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
import os         # os.replace() and os.fsync() make saving safe (see save_assets)
import socket     # provides the networking building blocks (sockets)
import sys        # sys.exit() reports startup failures to the operating system
import threading  # gives each client a thread, and provides the lock
import time       # waits between syncs with the other server

# Import the shared constants/helpers used by every part of the prototype.
from common import (
    BAD_REQUEST,
    DUPLICATE_ID,
    INTERNAL,
    SERVERS,
    TOO_LARGE,
    MessageTooLargeError,
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
REQUIRED_FIELDS = ("asset_id", "description", "category", "location", "status")

# How long a client may stay silent before the server gives up on it. Without
# this, an idle client would hold a thread and a connection forever.
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


def error_reply(code: str, detail: str) -> dict:
    """Build an ERROR reply in the one format every client relies on."""
    return {"status": "ERROR", "code": code, "detail": detail}


def validate_asset(asset: object) -> dict:
    """Return a clean copy of a client's asset, or raise ValueError if invalid.

    This happens on the server even if a client also checks, because anyone can
    write a program that speaks this protocol. Only the server can guarantee it.
    """
    if not isinstance(asset, dict):
        raise ValueError("'asset' must be an object")

    # Copying only the expected fields means any extra fields are dropped.
    clean = {}
    for field in REQUIRED_FIELDS:
        value = asset.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Field must be a non-empty string: {field}")
        clean[field] = value.strip()

    # The date comes from the server, and anything the client sent is ignored,
    # because client clocks can be wrong or in other time zones.
    clean["date_registered"] = datetime.date.today().isoformat()
    return clean


def register_asset(asset: dict) -> dict:
    """Store a validated asset and return the reply.

    Together with validate_asset() and save_assets(), this gives the ACID
    properties for each registration:
      Atomicity:   it is stored and saved, or not stored at all (the undo below).
      Consistency: only valid assets with unique IDs are ever stored.
      Isolation:   the lock stops concurrent registrations interfering.
      Durability:  the reply is only sent after the save has reached the disk.
    """
    with assets_lock:
        if any(existing["asset_id"] == asset["asset_id"] for existing in assets):
            return error_reply(DUPLICATE_ID, f"Asset ID {asset['asset_id']!r} is already registered")

        assets.append(asset)
        try:
            save_assets()
        except OSError:
            # Undo the change, so memory and disk still agree. The client will be
            # told this failed, so the asset must not appear in later listings.
            assets.pop()
            raise

    # Echo back what was stored, because the server may have changed it (trimmed
    # spaces, added the date).
    return {"status": "OK", "stored": asset}


def process_request(request: dict) -> dict:
    """Carry out one decoded request and return the reply.

    This does no networking, so the rules can be tested without a socket.
    """
    action = request.get("action")

    if action == "REGISTER":
        try:
            asset = validate_asset(request.get("asset"))
        except ValueError as exc:
            return error_reply(BAD_REQUEST, str(exc))
        return register_asset(asset)

    if action == "LIST":
        # Copy the list while holding the lock. It is sent after the lock is
        # released, so one slow client cannot hold up everyone else.
        with assets_lock:
            return {"status": "OK", "assets": list(assets)}

    return error_reply(BAD_REQUEST, f"Unknown action: {action!r}")


def merge_assets(peer_assets: list, prefer_peer: bool) -> int:
    """Add the other server's assets to this one's, returning how many changed.

    Assets are only ever added, never changed or removed, so combining two lists
    is simple: anything missing is copied across. The one awkward case is when each
    server accepted a different asset with the same ID while they could not reach
    each other. Then the backup gives way to the primary (`prefer_peer` is True on
    the backup), so that the two always end up with identical data.
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


def sync_with_peer(peer: tuple, prefer_peer: bool) -> None:
    """Keep copying assets from the other server. Runs on its own thread forever.

    WHY THE SERVER ASKS, RATHER THAN BEING TOLD: each server simply fetches the
    other's list. A server that was down catches up by itself when it restarts, and
    a server whose partner is down carries on without having to wait for it.

    The trade-off is that the two can differ for a couple of seconds. This is called
    eventual consistency. An asset is safe in the file of the server that accepted
    it, and reaches the other server on the next sync after both are running.
    """
    peer_was_reachable = True

    while True:
        try:
            reply = request_from(peer, {"action": "LIST"})
            changed = merge_assets(reply.get("assets", []), prefer_peer)
            if changed:
                logger.info("Copied %s asset(s) from the other server", changed)
            if not peer_was_reachable:
                logger.info("The other server is back")
            peer_was_reachable = True
        except OSError as exc:
            # Only report the change, not every failed attempt.
            if peer_was_reachable:
                logger.warning("Cannot reach the other server: %s", exc)
            peer_was_reachable = False
        except Exception:
            # Anything else, such as an invalid reply, must not stop the syncing.
            logger.exception("Could not sync with the other server")

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
            send_message(conn, read_and_process(conn))
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

    # Allow a quick restart without the port being reported as "in use". On
    # Windows, SO_REUSEADDR would also let two servers share a port without any
    # error, so SO_EXCLUSIVEADDRUSE is used there instead.
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
    global ASSETS_FILE

    # The primary and backup are the same program. This option only decides which
    # address and data file it uses.
    is_backup = "--backup" in sys.argv
    address = SERVERS[1] if is_backup else SERVERS[0]
    peer = SERVERS[0] if is_backup else SERVERS[1]
    if is_backup:
        ASSETS_FILE = BACKUP_ASSETS_FILE

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

"""
The primary server of the Asset Management System.

    python server.py

The primary is the one server that normally accepts changes. Because every change
goes through this one process, there is one agreed order of events, which is what
prevents race conditions and duplicates across the system.

Before a client is told that a change worked, the primary copies it to the backup
(synchronous replication), so the change is on both machines. If the backup is down,
the primary carries on alone, then brings the backup up to date when it returns.

backup.py is the separate program for the other server. The code they share, including
the rules for changing assets, is in node.py.
"""

import os         # os.path finds the data file
import threading  # runs the check on the backup alongside the clients
import time       # waits between checks on the backup

import common
import node
from common import HEARTBEAT_TIMEOUT_SECONDS, request_from

ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets.json")

# How often the primary checks whether the backup needs bringing up to date.
BACKUP_CHECK_SECONDS = 1

logger = node.logger

backup_address = None    # where the backup is, or None if there is not one
backup_in_sync = False   # whether the backup has every change. Protected by the lock.


def is_valid_peer_asset(record: object) -> bool:
    """Check a record from the backup before it is stored.

    Records saved before tagging was added have no tags, and those saved before
    status changes were added have no version, so these fields and the date are
    optional, but they must have the right type if present.
    """
    if not isinstance(record, dict):
        return False

    try:
        for field in node.STORED_FIELDS:
            node.check_text(record.get(field), field)
    except ValueError:
        return False

    tags = record.get("tags", [])
    version = record.get("version", 0)
    return (
        isinstance(tags, list)
        and all(isinstance(tag, str) for tag in tags)
        and isinstance(record.get("date_registered", ""), str)
        and isinstance(version, int)
        and not isinstance(version, bool)  # in Python, True counts as an int
    )


def merge_state(peer_assets: list, peer_deleted: dict) -> int:
    """Combine the backup's data into this server's, returning how many records
    changed. The caller must hold the lock.

    The two can only differ if the backup took over while the primary was down or cut
    off. The primary alone makes these decisions, then gives the backup the result, so
    the two cannot disagree afterwards:
      - A deleted asset stays deleted, even if the other server changed it meanwhile.
      - A missing asset is copied across.
      - Of two different copies, the one with the higher version wins.
      - With equal versions, each server made one change. The primary's copy wins.
    """
    newly_deleted = {key: value for key, value in peer_deleted.items() if key not in node.deleted_ids}
    all_deleted = {**node.deleted_ids, **newly_deleted}

    # A dict keeps its order and makes each asset quick to find by ID.
    merged = {asset["asset_id"]: asset for asset in node.assets if asset["asset_id"] not in all_deleted}
    changed = len(newly_deleted)

    for peer_asset in peer_assets:
        asset_id = peer_asset["asset_id"]
        if asset_id in all_deleted:
            continue

        my_asset = merged.get(asset_id)
        if my_asset is None or peer_asset.get("version", 0) > my_asset.get("version", 0):
            merged[asset_id] = peer_asset
            changed += 1

    node.assets[:] = merged.values()
    node.deleted_ids.clear()
    node.deleted_ids.update(all_deleted)
    return changed


def catch_up_backup() -> bool:
    """Reconcile with the backup and bring it up to date, returning whether it worked.

    The lock is held throughout, so no change can slip in part-way through. A client
    that arrives meanwhile simply waits a moment.

      1. HANDOVER: the backup stops accepting changes and sends all its data.
      2. Its data is merged into this server's, and saved.
      3. INSTALL_STATE: the backup replaces its data with the merged result.

    If this server fails between the steps, the backup notices the missed heartbeats
    and takes over again, with its data intact.
    """
    global backup_in_sync

    with node.assets_lock:
        previous = (list(node.assets), dict(node.deleted_ids), node.last_seq)
        try:
            reply = request_from(backup_address, {"action": "HANDOVER"})
            if reply.get("status") != "OK":
                raise ValueError(f"It replied with an error: {reply.get('detail')}")

            records, deleted, peer_seq = reply.get("assets"), reply.get("deleted"), reply.get("seq")
            if not isinstance(records, list) or not isinstance(deleted, dict) or not isinstance(peer_seq, int):
                raise ValueError("Its reply has no valid data")

            valid_records = [record for record in records if is_valid_peer_asset(record)]
            if len(valid_records) != len(records):
                logger.warning("Skipped %s invalid record(s) from the backup", len(records) - len(valid_records))

            changed = merge_state(valid_records, deleted)
            node.last_seq = max(node.last_seq, peer_seq) + 1
            try:
                node.save_assets()
            except OSError:
                # Keep memory and disk in agreement. The next check will try again.
                node.assets[:], restored_deleted, node.last_seq = previous
                node.deleted_ids.clear()
                node.deleted_ids.update(restored_deleted)
                raise

            reply = request_from(backup_address, {
                "action": "INSTALL_STATE",
                "assets": node.assets,
                "deleted": node.deleted_ids,
                "seq": node.last_seq,
            })
            if reply.get("status") != "OK":
                raise ValueError(f"It replied with an error: {reply.get('detail')}")
        except (OSError, ValueError) as exc:
            logger.warning("Could not bring the backup up to date: %s", exc)
            backup_in_sync = False
            return False

        backup_in_sync = True

    logger.info("The backup is in sync, at change %s (%s record(s) reconciled)", node.last_seq, changed)
    return True


def replicate_change(change: dict) -> None:
    """Copy one change to the backup before the client is told it worked. Called by
    node.commit_change() while holding the lock, so changes arrive in order.

    A failure does not fail the client's request, because the change is safe on this
    server, and the system must keep working while the backup is down.
    """
    global backup_in_sync

    if not backup_in_sync:
        return

    try:
        reply = request_from(backup_address, {"action": "REPLICATE", **change})
        if reply.get("status") != "OK":
            raise ValueError(f"It replied with an error: {reply.get('detail')}")
        logger.info("Change %s copied to the backup", change["seq"])
    except (OSError, ValueError) as exc:
        logger.warning("Could not copy change %s to the backup: %s", change["seq"], exc)
        backup_in_sync = False


def watch_backup() -> None:
    """Check on the backup forever, bringing it up to date whenever it needs it.
    Runs on its own thread.

    This also ends a split: if the network failed but both servers stayed running,
    the backup will have taken over, and this notices when it can be reached again.
    """
    global backup_in_sync

    while True:
        time.sleep(BACKUP_CHECK_SECONDS)
        try:
            reply = request_from(backup_address, {"action": "PING"}, timeout=HEARTBEAT_TIMEOUT_SECONDS)
        except (OSError, ValueError):
            with node.assets_lock:
                if backup_in_sync:
                    logger.warning("The backup cannot be reached. Carrying on alone.")
                backup_in_sync = False
            continue

        with node.assets_lock:
            if reply.get("accepts_writes"):
                backup_in_sync = False
            needs_catch_up = not backup_in_sync

        if needs_catch_up:
            catch_up_backup()


def main() -> None:
    global backup_address

    node.ROLE = "primary"
    node.ID_PREFIX = "AST-P"
    node.ASSETS_FILE = ASSETS_FILE
    node.accepts_writes = True
    node.on_change = replicate_change

    if len(common.SERVERS) > 1:
        backup_address = common.SERVERS[1]

    server_sock = node.start_up(common.SERVERS[0])
    with server_sock:
        if backup_address is not None:
            # The backup may have taken over while this server was down, so get its
            # changes before serving anyone. If it is not there, start alone.
            catch_up_backup()

            # daemon=True means this thread does not stop the program from exiting.
            threading.Thread(target=watch_backup, name="watch-backup", daemon=True).start()

        try:
            node.run_server(server_sock)
        except KeyboardInterrupt:
            logger.info("Server stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

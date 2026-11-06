"""
An asset server: the primary, or the backup.

    python server.py              runs the primary, the first address in the server list
    python server.py --backup 1   runs the backup, the second address in the list

Both are this one program, run as separate processes with their own data files, possibly
on different machines. The flag only says which entry in the server list this one is.

The primary is the one server that normally accepts changes. Because every change goes
through this one process, there is one agreed order of events, which is what prevents
race conditions and duplicates across the system. Before a client is told that a change
worked, the primary copies it to the backup (synchronous replication), so the change is
on both machines. If the backup is down, the primary carries on alone, then brings the
backup up to date when it returns.

The backup has two states:

  Standby:   it receives each change from the primary and applies it in order. It
             answers LIST, but refuses changes from clients with NOT_PRIMARY.
  Taken over: it has decided the primary has failed, and accepts changes itself.

It detects failure with heartbeats: it pings the primary every second, and takes over
after several pings in a row get no answer. When the primary returns, the primary
reconciles the two servers' data, and the backup goes back to standby.

The code every server shares, including the rules for changing assets, is in node.py.
"""

import argparse   # reads the --backup option
import os         # os.path finds the data file
import threading  # runs the heartbeats alongside the clients
import time       # waits between heartbeats

import common
import node
from common import HEARTBEAT_TIMEOUT_SECONDS, NOT_STANDBY, OUT_OF_SYNC, request_from

# Tests set this to keep the data somewhere else. Otherwise each server's file is chosen
# by data_file() below.
ASSETS_FILE = None

# How often the primary checks whether the backup needs bringing up to date.
BACKUP_CHECK_SECONDS = 1

# How long the primary keeps trying to sync with the backup when it starts, and how long
# it waits between tries. When both are started together, the backup may not be listening
# yet. Without retrying, changes made in the next second wouldn't be copied to it. The
# limit stays well under a client's timeout, because clients wait while this runs.
START_SYNC_SECONDS = 3
START_SYNC_RETRY_SECONDS = 0.2

# How often the backup pings the primary, and how many pings in a row must go unanswered
# before it takes over. Needing several means that one lost message does not cause a
# takeover.
HEARTBEAT_INTERVAL_SECONDS = 1
MISSED_HEARTBEATS_LIMIT = 3

logger = node.logger

backup_address = None    # where the backup is, or None if there is not one
backup_in_sync = False   # whether the backup has every change. Protected by the lock.
missed_heartbeats = 0    # how many pings in a row the primary has not answered


# ---- Which server this is ----

def name_of(index: int) -> str:
    """The name of the server at this position in the list, used in replies and logs."""
    return "primary" if index == 0 else "backup"


def data_file(index: int) -> str:
    """Where the server at this position in the list keeps its data."""
    return os.path.join(common.DATA_FOLDER, "assets.json" if index == 0 else f"assets_{name_of(index)}.json")


# ---- Following: how the backup applies what the primary sends ----

def apply_change(request: dict) -> dict:
    """Apply one change sent by the primary.

    The sequence number shows whether any change was missed. If so, applying this one
    could leave the two servers different without anyone knowing, so it is refused,
    and the primary sends all its data instead.
    """
    with node.assets_lock:
        if node.accepts_writes:
            return node.error_reply(NOT_STANDBY, "The backup has taken over")

        seq = request.get("seq")
        if seq != node.last_seq + 1:
            return node.error_reply(OUT_OF_SYNC, f"Expected change {node.last_seq + 1}, but received {seq}")

        previous = (list(node.assets), dict(node.deleted_ids), dict(node.deleted_at))

        if "asset" in request:
            asset = request["asset"]
            index = node.find_asset(asset["asset_id"])
            if index is None:
                node.assets.append(asset)
            else:
                node.assets[index] = asset
        else:
            asset_id = request["deleted"]
            index = node.find_asset(asset_id)
            if index is not None:
                node.assets.pop(index)
            node.deleted_ids[asset_id] = request.get("request_id")
            if isinstance(request.get("deleted_at"), str):
                node.deleted_at[asset_id] = request["deleted_at"]  # the primary's time, not this server's

        node.last_seq = seq
        try:
            node.save_assets()
        except OSError:
            # Keep memory and disk in agreement. The primary will send everything later.
            node.assets[:], restored_deleted, restored_times = previous
            restore_deleted(restored_deleted, restored_times)
            node.last_seq = seq - 1
            raise

        logger.info("Applied change %s from the primary", seq)

    return {"status": "OK", "seq": seq}


def hand_over(request: dict) -> dict:
    """Stop accepting changes, and send the primary all this server's data.

    Both happen while holding the lock, so no change can be accepted after the data
    is copied, and so none can be missed.
    """
    global missed_heartbeats

    with node.assets_lock:
        if node.accepts_writes:
            logger.warning("The primary is back. Returning to standby.")
        node.accepts_writes = False
        missed_heartbeats = 0
        return {
            "status": "OK",
            "assets": list(node.assets),
            "deleted": dict(node.deleted_ids),
            "deleted_at": dict(node.deleted_at),
            "seq": node.last_seq,
        }


def restore_deleted(deleted: dict, times: dict) -> None:
    """Put back the deleted IDs and their deletion times. The caller must hold the lock."""
    node.deleted_ids.clear()
    node.deleted_ids.update(deleted)
    node.deleted_at.clear()
    node.deleted_at.update(times)


def install_state(request: dict) -> dict:
    """Replace this server's data with the primary's reconciled copy."""
    with node.assets_lock:
        previous = (list(node.assets), dict(node.deleted_ids), dict(node.deleted_at), node.last_seq)

        node.assets[:] = request["assets"]
        restore_deleted(request["deleted"], request.get("deleted_at", {}))
        node.last_seq = request["seq"]
        try:
            node.save_assets()
        except OSError:
            node.assets[:], restored_deleted, restored_times, node.last_seq = previous
            restore_deleted(restored_deleted, restored_times)
            raise

        logger.info("In sync with the primary, at change %s", node.last_seq)

    return {"status": "OK"}


def monitor_primary() -> None:
    """Ping the primary forever, taking over if it stops answering. Runs on its own
    thread."""
    global missed_heartbeats

    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            request_from(common.SERVERS[0], {"action": "PING"}, timeout=HEARTBEAT_TIMEOUT_SECONDS)
        except (OSError, ValueError):
            with node.assets_lock:
                missed_heartbeats += 1
                if missed_heartbeats >= MISSED_HEARTBEATS_LIMIT and not node.accepts_writes:
                    node.accepts_writes = True
                    logger.warning(
                        "The primary missed %s heartbeats. TAKING OVER: now accepting changes.",
                        missed_heartbeats,
                    )
        else:
            with node.assets_lock:
                missed_heartbeats = 0


# ---- Leading: how the primary keeps the backup up to date ----

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
        and isinstance(record.get("created_at", ""), str)
        and isinstance(record.get("updated_at", ""), str)
        and isinstance(version, int)
        and not isinstance(version, bool)  # in Python, True counts as an int
    )


def merge_state(peer_assets: list, peer_deleted: dict, peer_deleted_at: dict = None) -> int:
    """Combine the backup's data into this server's, returning how many records
    changed. The caller must hold the lock.

    The two can only differ if the backup took over while the primary was down or cut
    off. The primary alone makes these decisions, then gives the backup the result, so
    the two cannot disagree afterwards:
      - A deleted asset stays deleted, even if the other server changed it meanwhile.
      - A missing asset is copied across.
      - Of two different copies, the one with the higher version wins.
      - With equal versions, each server made one change. The primary's copy wins.
      - If each server registered the same item, the primary's record is kept.
    A deletion keeps the time the backup recorded for it, if it sent one.
    """
    newly_deleted = {key: value for key, value in peer_deleted.items() if key not in node.deleted_ids}
    all_deleted = {**node.deleted_ids, **newly_deleted}
    for asset_id in newly_deleted:
        time_deleted = (peer_deleted_at or {}).get(asset_id)
        if isinstance(time_deleted, str):
            node.deleted_at[asset_id] = time_deleted

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

    # The same item registered on both servers, such as a client retrying on the
    # backup after losing the primary's reply. This server's own IDs sort first.
    by_preference = sorted(merged, key=lambda asset_id: (not asset_id.startswith(node.ID_PREFIX), asset_id))
    identities = set()
    for asset_id in by_preference:
        identity = node.identity_of(merged[asset_id])
        if identity in identities:
            all_deleted[asset_id] = merged.pop(asset_id).get("request_id")
            node.deleted_at[asset_id] = node.utc_timestamp()
            changed += 1
        identities.add(identity)

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
        previous = (list(node.assets), dict(node.deleted_ids), dict(node.deleted_at), node.last_seq)
        try:
            reply = request_from(backup_address, {"action": "HANDOVER"})
            if reply.get("status") != "OK":
                raise ValueError(f"It replied with an error: {reply.get('detail')}")

            records, deleted, peer_seq = reply.get("assets"), reply.get("deleted"), reply.get("seq")
            deleted_at = reply.get("deleted_at", {})
            if (not isinstance(records, list) or not isinstance(deleted, dict) or not isinstance(peer_seq, int)
                    or not isinstance(deleted_at, dict)):
                raise ValueError("Its reply has no valid data")

            valid_records = [record for record in records if is_valid_peer_asset(record)]
            if len(valid_records) != len(records):
                logger.warning("Skipped %s invalid record(s) from the backup", len(records) - len(valid_records))

            changed = merge_state(valid_records, deleted, deleted_at)
            node.last_seq = max(node.last_seq, peer_seq) + 1
            try:
                node.save_assets()
            except OSError:
                # Keep memory and disk in agreement. The next check will try again.
                node.assets[:], restored_deleted, restored_times, node.last_seq = previous
                restore_deleted(restored_deleted, restored_times)
                raise

            reply = request_from(backup_address, {
                "action": "INSTALL_STATE",
                "assets": node.assets,
                "deleted": node.deleted_ids,
                "deleted_at": node.deleted_at,
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


def sync_at_start() -> bool:
    """Try to sync with the backup for up to START_SYNC_SECONDS, returning whether it
    worked. If the backup isn't there by then, the primary starts alone, and watch_backup()
    syncs it when it appears."""
    deadline = time.monotonic() + START_SYNC_SECONDS
    while not catch_up_backup():
        if time.monotonic() >= deadline:
            logger.info("The backup isn't reachable yet, so starting alone for now.")
            return False
        time.sleep(START_SYNC_RETRY_SECONDS)
    return True


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


# ---- Starting up ----

def run_primary() -> None:
    global backup_address

    node.accepts_writes = True
    node.on_change = replicate_change

    if len(common.SERVERS) > 1:
        backup_address = common.SERVERS[1]

    server_sock = node.start_up(common.SERVERS[0])
    with server_sock:
        if backup_address is not None:
            # The backup may have taken over while this server was down, so get its
            # changes before serving anyone. If it is not there, start alone.
            sync_at_start()

            # daemon=True means this thread does not stop the program from exiting.
            threading.Thread(target=watch_backup, name="watch-backup", daemon=True).start()

        try:
            node.run_server(server_sock)
        except KeyboardInterrupt:
            logger.info("Server stopped.")


def run_backup() -> None:
    node.accepts_writes = False
    node.role_actions = {
        "REPLICATE": apply_change,
        "HANDOVER": hand_over,
        "INSTALL_STATE": install_state,
    }

    server_sock = node.start_up(common.SERVERS[1])
    with server_sock:
        # daemon=True means this thread does not stop the program from exiting.
        threading.Thread(target=monitor_primary, name="heartbeat", daemon=True).start()

        try:
            node.run_server(server_sock)
        except KeyboardInterrupt:
            logger.info("Server stopped.")


def main(argv: list = None) -> None:
    """Start this server. `argv` is the command-line options, given by tests, or None to
    read them from the command line."""
    parser = argparse.ArgumentParser(description="Run an asset server.")
    parser.add_argument("--backup", type=int, default=0, metavar="N",
                        help="run the backup, the entry after the primary in the server list (N=1)")
    index = parser.parse_args(argv).backup
    if index not in (0, 1) or index >= len(common.SERVERS):
        parser.error("--backup must be 1, and the server list must have an entry for it")

    node.ROLE = name_of(index)
    node.ID_PREFIX = "AST-P" if index == 0 else "AST-B"
    node.ASSETS_FILE = ASSETS_FILE or data_file(index)

    if index == 0:
        run_primary()
    else:
        run_backup()


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

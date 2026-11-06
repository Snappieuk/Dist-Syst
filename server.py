"""
An asset server. Every server in the system is this one program.

    python server.py              runs the primary, the first address in the server list
    python server.py --backup 1   runs the second address in the list
    python server.py --backup 2   runs the third, and so on

Each runs as its own process with its own data file, possibly on its own machine. The
flag only says which entry in the server list (in config.json) this server is. To add a
server, add its address to the end of the list on every machine and start it with the
next number.

One rule decides everything: the order of the list is the order of succession.

  - A server accepts changes only when no server before it in the list is answering.
    Normally that is the primary, so there is one agreed order of events, which is what
    prevents race conditions and duplicates.
  - The server accepting changes (the leader) copies each change to every server after
    it before telling the client it worked (synchronous replication), so the change is
    on all of them. One that is down is skipped, and brought up to date when it returns.
  - Every other server is on standby: it applies the copies it is sent and answers LIST,
    but refuses changes from clients with NOT_PRIMARY.

Each server pings the servers before it every second. If none of them answers for
several pings in a row, it takes over. When a server earlier in the list comes back, it
takes control again: it collects each later server's data, merges it into its own, and
sends the result back, which puts them on standby.

This is kept simple on purpose. There is no voting between servers, so if the network
splits, servers on each side can both accept changes until it heals. See "Known
limitations" in the README.

The code every server shares, including the rules for changing assets, is in node.py.
"""

import argparse   # reads the --backup option
import os         # os.path finds the data file
import threading  # runs the heartbeats alongside the clients
import time       # waits between heartbeats
from typing import Optional  # type hints only

import common
import node
from common import HEARTBEAT_TIMEOUT_SECONDS, NOT_STANDBY, OUT_OF_SYNC, request_from

# Tests set this to keep the data somewhere else. Otherwise each server's file is chosen
# by data_file() below.
ASSETS_FILE = None

# Each server gives out asset IDs from its own range, so two servers can never give the
# same ID: AST-P... from the primary, AST-B... from the first backup, then AST-C... and so
# on. That is also the most servers there can be.
ID_LETTERS = "PBCDEFGHIJKLMNOQRSTUVWXYZ"

# How often the leader checks whether each later server needs bringing up to date.
FOLLOWER_CHECK_SECONDS = 1

# How long the primary keeps trying to sync with the later servers when it starts, and how
# long it waits between tries. When all are started together, the others may not be
# listening yet. Without retrying, changes made in the next second wouldn't be copied to
# them. The limit stays well under a client's timeout, because clients wait while it runs.
START_SYNC_SECONDS = 3
START_SYNC_RETRY_SECONDS = 0.2

# How often a server pings the servers before it, and how many pings in a row must go
# unanswered before it takes over. Needing several means that one lost message does not
# cause a takeover.
HEARTBEAT_INTERVAL_SECONDS = 1
MISSED_HEARTBEATS_LIMIT = 3

logger = node.logger

my_index = 0           # this server's position in the server list
followers_in_sync = {}  # {position: whether it has every change} for each later server.
                        # Protected by the lock.
missed_heartbeats = 0   # how many pings in a row no earlier server has answered


# ---- Which server this is ----

def name_of(index: int) -> str:
    """The name of the server at this position in the list, used in replies and logs:
    primary, backup, then backup2, backup3 and so on."""
    if index == 0:
        return "primary"
    return "backup" if index == 1 else f"backup{index}"


def data_file(index: int) -> str:
    """Where the server at this position in the list keeps its data."""
    return os.path.join(common.DATA_FOLDER, "assets.json" if index == 0 else f"assets_{name_of(index)}.json")


# ---- Following: applying what the leader sends ----

def apply_change(request: dict) -> dict:
    """Apply one change sent by the leader.

    The sequence number shows whether any change was missed. If so, applying this one
    could leave the servers different without anyone knowing, so it is refused, and the
    leader sends all its data instead.
    """
    with node.assets_lock:
        if node.accepts_writes:
            return node.error_reply(NOT_STANDBY, f"The {node.ROLE} server has taken over")

        expected = node.last_seq + 1
        if request.get("seq") != expected:
            return node.error_reply(OUT_OF_SYNC, f"Expected change {expected}, but received {request.get('seq')}")
        seq = expected

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
                node.deleted_at[asset_id] = request["deleted_at"]  # the leader's time, not this server's

        node.last_seq = seq
        try:
            node.save_assets()
        except OSError:
            # Keep memory and disk in agreement. The leader will send everything later.
            node.assets[:], restored_deleted, restored_times = previous
            restore_deleted(restored_deleted, restored_times)
            node.last_seq = seq - 1
            raise

        logger.info("Applied change %s from the leader", seq)

    return {"status": "OK", "seq": seq}


def hand_over(request: dict) -> dict:
    """Stop accepting changes, and send all this server's data to an earlier server that
    is taking control.

    Both happen while holding the lock, so no change can be accepted after the data
    is copied, and so none can be missed.
    """
    global missed_heartbeats

    with node.assets_lock:
        if node.accepts_writes:
            logger.warning("A server earlier in the list is back. Returning to standby.")
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
    """Replace this server's data with the leader's reconciled copy."""
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

        logger.info("In sync with the leader, at change %s", node.last_seq)

    return {"status": "OK"}


def any_earlier_server_answers() -> bool:
    """Ping each server before this one in the list, stopping at the first that answers."""
    for address in common.SERVERS[:my_index]:
        try:
            request_from(address, {"action": "PING"}, timeout=HEARTBEAT_TIMEOUT_SECONDS)
            return True
        except (OSError, ValueError):
            continue
    return False


def monitor_earlier_servers() -> None:
    """Ping the servers before this one forever, taking over if none of them answers.
    Runs on its own thread.

    Taking over only needs every earlier server to be silent. If one of them is alive,
    it is the one that should be in charge, or will take over itself if the ones before
    it are down.
    """
    global missed_heartbeats

    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if any_earlier_server_answers():
            with node.assets_lock:
                missed_heartbeats = 0
            continue

        with node.assets_lock:
            missed_heartbeats += 1
            if missed_heartbeats >= MISSED_HEARTBEATS_LIMIT and not node.accepts_writes:
                node.accepts_writes = True
                logger.warning(
                    "No server before this one has answered for %s heartbeats. "
                    "TAKING OVER: now accepting changes.",
                    missed_heartbeats,
                )


# ---- Leading: keeping every later server up to date ----

def is_valid_peer_asset(record: object) -> bool:
    """Check a record from another server before it is stored.

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


def merge_state(peer_assets: list, peer_deleted: dict, peer_deleted_at: Optional[dict] = None) -> int:
    """Combine a later server's data into this server's, returning how many records
    changed. The caller must hold the lock.

    The two can only differ if the later server took over while this one was down or
    cut off. This server alone makes these decisions, then gives the other the result,
    so the two cannot disagree afterwards:
      - A deleted asset stays deleted, even if the other server changed it meanwhile.
      - A missing asset is copied across.
      - Of two different copies, the one with the higher version wins.
      - With equal versions, each server made one change. This server's copy wins.
      - If each server registered the same item, this server's record is kept.
    A deletion keeps the time the other server recorded for it, if it sent one.
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


def catch_up(index: int) -> bool:
    """Reconcile with the later server at this position and bring it up to date,
    returning whether it worked.

    The lock is held throughout, so no change can slip in part-way through. A client
    that arrives meanwhile simply waits a moment.

      1. HANDOVER: the other server stops accepting changes and sends all its data.
      2. Its data is merged into this server's, and saved.
      3. INSTALL_STATE: the other server replaces its data with the merged result.

    If this server fails between the steps, the other notices the missed heartbeats and
    takes over again, with its data intact.
    """
    address, name = common.SERVERS[index], name_of(index)
    with node.assets_lock:
        previous = (list(node.assets), dict(node.deleted_ids), dict(node.deleted_at), node.last_seq)
        try:
            reply = request_from(address, {"action": "HANDOVER"})
            if reply.get("status") != "OK":
                raise ValueError(f"It replied with an error: {reply.get('detail')}")

            records, deleted, peer_seq = reply.get("assets"), reply.get("deleted"), reply.get("seq")
            deleted_at = reply.get("deleted_at", {})
            if (not isinstance(records, list) or not isinstance(deleted, dict) or not isinstance(peer_seq, int)
                    or not isinstance(deleted_at, dict)):
                raise ValueError("Its reply has no valid data")

            valid_records = [record for record in records if is_valid_peer_asset(record)]
            if len(valid_records) != len(records):
                logger.warning("Skipped %s invalid record(s) from the %s", len(records) - len(valid_records), name)

            changed = merge_state(valid_records, deleted, deleted_at)
            seq_before = node.last_seq
            node.last_seq = max(node.last_seq, peer_seq)
            try:
                node.save_assets()
            except OSError:
                # Keep memory and disk in agreement. The next check will try again.
                node.assets[:], restored_deleted, restored_times, node.last_seq = previous
                restore_deleted(restored_deleted, restored_times)
                raise

            reply = request_from(address, {
                "action": "INSTALL_STATE",
                "assets": node.assets,
                "deleted": node.deleted_ids,
                "deleted_at": node.deleted_at,
                "seq": node.last_seq,
            })
            if reply.get("status") != "OK":
                raise ValueError(f"It replied with an error: {reply.get('detail')}")
        except (OSError, ValueError) as exc:
            logger.warning("Could not bring the %s up to date: %s", name, exc)
            followers_in_sync[index] = False
            return False

        followers_in_sync[index] = True

        # If that server had changes this one lacked, or a higher change number, then the
        # other servers that were in sync no longer are. They are marked to be brought up
        # to date too, which happens within a second. This can't go on forever: a server
        # that really was in sync has nothing to add, so bringing it up to date changes
        # nothing here.
        if changed or node.last_seq != seq_before:
            for other in followers_in_sync:
                if other != index:
                    followers_in_sync[other] = False

    logger.info("The %s is in sync, at change %s (%s record(s) reconciled)", name, node.last_seq, changed)
    return True


def replicate_change(change: dict) -> None:
    """Copy one change to every later server that is in sync, before the client is told
    it worked. Called by node.commit_change() while holding the lock, so changes arrive
    in order.

    A failure does not fail the client's request, because the change is safe on this
    server, and the system must keep working while another server is down.
    """
    for index, in_sync in followers_in_sync.items():
        if not in_sync:
            continue
        name = name_of(index)
        try:
            reply = request_from(common.SERVERS[index], {"action": "REPLICATE", **change})
            if reply.get("status") != "OK":
                raise ValueError(f"It replied with an error: {reply.get('detail')}")
            logger.info("Change %s copied to the %s", change["seq"], name)
        except (OSError, ValueError) as exc:
            logger.warning("Could not copy change %s to the %s: %s", change["seq"], name, exc)
            followers_in_sync[index] = False


def sync_at_start() -> bool:
    """Try to sync with every later server for up to START_SYNC_SECONDS, returning
    whether all of them are in sync. Any that aren't there by then are synced by
    watch_followers() when they appear."""
    deadline = time.monotonic() + START_SYNC_SECONDS
    while True:
        for index, in_sync in list(followers_in_sync.items()):
            if not in_sync:
                catch_up(index)
        if all(followers_in_sync.values()):
            return True
        if time.monotonic() >= deadline:
            logger.info("Not every server is reachable yet, so starting without them for now.")
            return False
        time.sleep(START_SYNC_RETRY_SECONDS)


def watch_followers() -> None:
    """Check on every later server forever, bringing each up to date whenever it needs
    it, but only while this server is the one accepting changes. Runs on its own thread.

    This also ends a split: if the network failed but the servers stayed running, a
    later one will have taken over, and this notices when it can be reached again.
    """
    while True:
        time.sleep(FOLLOWER_CHECK_SECONDS)
        for index in followers_in_sync:
            with node.assets_lock:
                if not node.accepts_writes:
                    # Another server is in charge and keeps them up to date. If this one
                    # takes over later, it must sync them again first.
                    followers_in_sync[index] = False
                    continue

            name = name_of(index)
            try:
                reply = request_from(common.SERVERS[index], {"action": "PING"}, timeout=HEARTBEAT_TIMEOUT_SECONDS)
            except (OSError, ValueError):
                with node.assets_lock:
                    if followers_in_sync[index]:
                        logger.warning("The %s cannot be reached. Carrying on without it.", name)
                    followers_in_sync[index] = False
                continue

            with node.assets_lock:
                if reply.get("accepts_writes"):
                    followers_in_sync[index] = False  # it took over, so has changes to merge
                needs_catch_up = not followers_in_sync[index]

            if needs_catch_up:
                catch_up(index)


# ---- Starting up ----

def main(argv: Optional[list] = None) -> None:
    """Start this server. `argv` is the command-line options, given by tests, or None to
    read them from the command line."""
    global my_index

    parser = argparse.ArgumentParser(description="Run an asset server.")
    parser.add_argument("--backup", type=int, default=0, metavar="N",
                        help="run the server at this position in the server list, counting the primary as 0")
    my_index = parser.parse_args(argv).backup
    if not 0 <= my_index < len(common.SERVERS):
        parser.error(f"--backup {my_index} isn't a position in the server list, "
                     f"which has {len(common.SERVERS)} server(s)")

    node.ROLE = name_of(my_index)
    node.ID_PREFIX = f"AST-{ID_LETTERS[my_index]}"
    node.ASSETS_FILE = ASSETS_FILE or data_file(my_index)
    node.accepts_writes = my_index == 0  # only the primary starts in charge
    node.on_change = replicate_change
    node.role_actions = {
        "REPLICATE": apply_change,
        "HANDOVER": hand_over,
        "INSTALL_STATE": install_state,
    }
    followers_in_sync.update({index: False for index in range(my_index + 1, len(common.SERVERS))})

    server_sock = node.start_up(common.SERVERS[my_index])
    with server_sock:
        if my_index == 0 and followers_in_sync:
            # A later server may have taken over while this one was down, so get its
            # changes before serving anyone. Any that aren't there are synced later.
            sync_at_start()

        # daemon=True means these threads do not stop the program from exiting.
        if followers_in_sync:
            threading.Thread(target=watch_followers, name="watch-followers", daemon=True).start()
        if my_index > 0:
            threading.Thread(target=monitor_earlier_servers, name="heartbeat", daemon=True).start()

        try:
            node.run_server(server_sock)
        except KeyboardInterrupt:
            logger.info("Server stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

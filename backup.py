"""
The backup server of the Asset Management System.

    python backup.py

This is a separate program from the primary (server.py), run as its own process with
its own data file, and it can run on a different machine. It has two states:

  Standby:   it receives each change from the primary and applies it in order. It
             answers LIST, but refuses changes from clients with NOT_PRIMARY.
  Taken over: it has decided the primary has failed, and accepts changes itself.

It detects failure with heartbeats: it pings the primary every second, and takes over
after several pings in a row get no answer. When the primary returns, the primary
reconciles the two servers' data, and this server goes back to standby.

The code the two servers share, including the rules for changing assets, is in node.py.
"""

import os         # os.path finds the data file
import threading  # runs the heartbeat alongside the clients
import time       # waits between heartbeats

import common
import node
from common import HEARTBEAT_TIMEOUT_SECONDS, NOT_STANDBY, OUT_OF_SYNC, request_from

# The backup keeps its own copy of the data, separate from the primary's.
ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_backup.json")

# How often to ping the primary, and how many pings in a row must go unanswered before
# taking over. Needing several means that one lost message does not cause a takeover.
HEARTBEAT_INTERVAL_SECONDS = 1
MISSED_HEARTBEATS_LIMIT = 3

logger = node.logger

missed_heartbeats = 0


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

        previous = (list(node.assets), dict(node.deleted_ids))

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

        node.last_seq = seq
        try:
            node.save_assets()
        except OSError:
            # Keep memory and disk in agreement. The primary will send everything later.
            node.assets[:], restored_deleted = previous
            node.deleted_ids.clear()
            node.deleted_ids.update(restored_deleted)
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
            "seq": node.last_seq,
        }


def install_state(request: dict) -> dict:
    """Replace this server's data with the primary's reconciled copy."""
    with node.assets_lock:
        previous = (list(node.assets), dict(node.deleted_ids), node.last_seq)

        node.assets[:] = request["assets"]
        node.deleted_ids.clear()
        node.deleted_ids.update(request["deleted"])
        node.last_seq = request["seq"]
        try:
            node.save_assets()
        except OSError:
            node.assets[:], restored_deleted, node.last_seq = previous
            node.deleted_ids.clear()
            node.deleted_ids.update(restored_deleted)
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


def main() -> None:
    node.ROLE = "backup"
    node.ID_PREFIX = "AST-B"
    node.ASSETS_FILE = ASSETS_FILE
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


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

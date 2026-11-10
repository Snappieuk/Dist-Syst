"""
Shows a failover, step by step, on a running system.

    python tools/demo.py

Start the system first, either with python start.py or with each server in its own
terminal. The demo adds an asset and lends it out, stops the primary as if it had
crashed, adds another asset while the backup is in charge (which is flagged for review),
waits for the primary to come back, shows that nothing was lost, approves the flagged
asset, then deletes the assets it added. Its assets are marked as sample data, like
those from tools/fill.py, so they carry a "sample data" tag. It pauses between steps so you can
explain what is happening, and the server logs show the same events from their side.

start.py restarts the stopped primary by itself. If you started the servers in separate
terminals, the demo tells you when to start server.py again.
"""

import datetime  # the demo loan runs from today
import time      # times the failover, and waits for the primary to return
import os    # finds the project folder
import sys   # sys.path is where Python looks for modules

# This file is in tools/ and common.py is one folder up, in the project folder, so
# Python is told to look there too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from common import IN_USE, new_register_request, request_from, send_request

# How long to wait for the primary to come back before giving up.
PRIMARY_RETURN_SECONDS = 120


def pause() -> None:
    try:
        input("\nPress Enter for the next step...")
    except EOFError:
        print()  # no keyboard attached, so carry straight on


def heading(number: int, text: str) -> None:
    print(f"\n=== Step {number}: {text} ===")


def server_state(address: tuple) -> str:
    """Ask a server for its heartbeat, and describe what it is doing."""
    try:
        reply = request_from(address, {"action": "PING"}, timeout=common.HEARTBEAT_TIMEOUT_SECONDS)
    except (OSError, ValueError):
        return "down"
    return "accepting changes" if reply.get("accepts_writes") else "on standby"


def show_servers() -> None:
    for index, address in enumerate(common.SERVERS):
        name = "Primary" if index == 0 else f"Backup {index}"
        print(f"  {name} at {address[0]}:{address[1]}: {server_state(address)}")


def add_asset(description: str) -> tuple:
    """Register a demo asset through the normal client failover, report who stored it,
    and return the stored asset and the name of that server."""
    started = time.monotonic()
    reply = send_request(new_register_request(
        {"description": description, "category": "Demo", "location": "Demo room", "status": IN_USE,
         "sample": True}
    ))
    if reply.get("status") != "OK":
        raise RuntimeError(f"The asset was not added: {reply.get('detail')}")
    stored = reply["stored"]
    print(f"  Added {stored['asset_id']} ({description}) in {time.monotonic() - started:.1f} seconds,"
          f" served by the {reply['served_by']}.")
    return stored, reply["served_by"]


def lend(asset: dict) -> None:
    """Check the demo asset out for a week, so it shows on the web page's calendar."""
    today = datetime.date.today()
    reply = send_request({"action": "CHECK_OUT", "asset_id": asset["asset_id"], "borrower": "Demo borrower",
                          "start": today.isoformat(), "due": (today + datetime.timedelta(days=7)).isoformat()})
    if reply.get("status") == "OK":
        print(f"  Checked {asset['asset_id']} out to Demo borrower for a week. It shows as a bar on the"
              " web page's loans calendar.")


def is_flagged(asset_id: str) -> bool:
    """Whether the asset is waiting for review, according to whichever server answers."""
    assets = send_request({"action": "LIST"}).get("assets", [])
    return any(asset["asset_id"] == asset_id and asset.get("flagged") for asset in assets)


def held_by(address: tuple) -> set:
    """The IDs of the assets one server holds."""
    reply = request_from(address, {"action": "LIST"})
    return {asset["asset_id"] for asset in reply.get("assets", [])}


def primary_is_back() -> bool:
    """The primary is accepting changes again, and the backup has gone back to standby."""
    primary, backup = common.SERVERS[:2]  # the demo uses the first two servers
    return server_state(primary) == "accepting changes" and server_state(backup) != "accepting changes"


def remove(asset_ids: list) -> None:
    """Delete the demo's assets, so it leaves the data as it found it."""
    current = {asset["asset_id"]: asset for asset in send_request({"action": "LIST"}).get("assets", [])}
    for asset_id in asset_ids:
        if asset_id in current:
            send_request({"action": "DELETE", "asset_id": asset_id, "expected_version": current[asset_id]["version"]})
            print(f"  Deleted {asset_id}.")


def run_demo() -> bool:
    """Run every step, returning whether the demo could be completed."""
    if len(common.SERVERS) < 2:
        print("Only one server is configured, so there is nothing to fail over to.")
        return False
    primary, backup = common.SERVERS[:2]  # the demo uses the first two servers

    heading(1, "The system is running normally")
    show_servers()
    if server_state(primary) != "accepting changes" or server_state(backup) != "on standby":
        print("\nStart the primary and the backup first, for example with python start.py,"
              " and wait for them to connect.")
        return False
    print("  The primary handles every change. The backup keeps a copy and checks on the"
          " primary every second.")
    pause()

    stamp = time.strftime("%H:%M:%S")
    heading(2, "Adding an asset while the primary is in charge")
    first, _ = add_asset(f"Demo laptop {stamp}")
    if first["asset_id"] in held_by(backup):
        print("  The backup already holds a copy, because the primary copies each change"
              " before replying.")
    lend(first)
    pause()

    heading(3, "Stopping the primary, as if it had crashed")
    request_from(primary, {"action": "STOP"})
    while server_state(primary) != "down":
        time.sleep(0.2)
    print("  The primary has stopped without warning anyone. Watch the backup's log: it"
          " misses 3 heartbeats, then takes over.")

    heading(4, "Adding an asset while the primary is down")
    print("  The client tries the primary, then the backup, and waits for it to take over...")
    second, served_by = add_asset(f"Demo monitor {stamp}")
    if served_by == "backup":
        print(f"  Its ID starts with {second['asset_id'][:5]}, because the backup issues IDs"
              " from its own range.")
        if second.get("flagged"):
            print("  It is FLAGGED FOR REVIEW, because it was added while the primary was offline.")
    else:
        print("  The primary was restarted before the backup took over, so it handled this.")
    show_servers()
    pause()

    heading(5, "Waiting for the primary to come back")
    print("  start.py restarts it by itself. If you started the servers separately, run"
          " python server.py again now.")
    deadline = time.monotonic() + PRIMARY_RETURN_SECONDS
    while not primary_is_back():
        if time.monotonic() > deadline:
            print("  The primary didn't come back, so the demo stops here.")
            return False
        time.sleep(1)
    print("  The primary is back. It took the backup's changes, and the backup returned"
          " to standby.")
    show_servers()
    ids = held_by(primary)
    for asset in (first, second):
        found = "has" if asset["asset_id"] in ids else "is MISSING"
        print(f"  The primary {found} {asset['asset_id']} ({asset['description']}).")
    print("  Nothing was lost, including the change made while the primary was down.")
    pause()

    heading(6, "Reviewing the asset added while the primary was offline")
    if is_flagged(second["asset_id"]):
        print(f"  {second['asset_id']} is listed under \"Flagged for review\" on the web page and in"
              " the GUI, where an admin can approve or deny it.")
        current = {asset["asset_id"]: asset for asset in send_request({"action": "LIST"})["assets"]}
        reply = send_request({"action": "APPROVE", "asset_id": second["asset_id"],
                              "expected_version": current[second["asset_id"]]["version"]})
        if reply.get("status") == "OK":
            print(f"  Approved {second['asset_id']}, so it is no longer flagged. Its history records"
                  " the approval.")
    else:
        print("  Nothing needs reviewing, because the primary handled every change.")
    pause()

    heading(7, "Tidying up")
    remove([first["asset_id"], second["asset_id"]])
    print("\nDemo complete.")
    return True


def main() -> None:
    try:
        run_demo()
    except KeyboardInterrupt:
        print("\nDemo stopped.")
    except (ConnectionError, RuntimeError, ValueError) as exc:
        print(f"\nThe demo could not carry on: {exc}")


if __name__ == "__main__":
    main()

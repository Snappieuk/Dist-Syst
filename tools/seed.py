"""
Sample-data tool.

Fills a running server with realistic sample assets so that every field of a stored
asset holds a value:

- every asset gets an asset_id, description, category, location and status, and the
  statuses alternate between "In Use" and "Not In Use";
- every third asset is then edited, so its version is above 1 and its updated_at
  differs from its created_at;
- every tenth asset is deleted (administrators only), so deleted and deleted_at are
  filled too. Deleted assets are kept by the server but left out of lists.

It talks to the server with the normal requests (LOGIN, REGISTER, EDIT, DELETE), so
the changes are checked, saved, audited and copied to the backup like any other.
The server must already be running (start it with python server.py first).

    python tools/seed.py          (30 assets)
    python tools/seed.py 100      (100 assets)

Asset IDs are SEED-0001, SEED-0002 and so on. IDs that already exist (from an
earlier run) are skipped, so running it again adds new assets instead of failing.
"""

import getpass
import sys

import os

# common.py is in the project folder, one level up from this one.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import DUPLICATE_ASSET, STATUSES, load_config, login, new_request_id, send_request

DEFAULT_COUNT = 30
EDIT_EVERY = 3      # every third asset is edited
DELETE_EVERY = 10   # every tenth asset is deleted (administrators only)
MAX_ID_NUMBER = 9999

# Each category with a few realistic descriptions.
CATALOGUE = {
    "IT Equipment": ["Dell Latitude 5440 laptop", "Lenovo ThinkPad T14 laptop", "HP EliteDesk 800 desktop",
                     "Dell UltraSharp 27in monitor", "Cisco Catalyst 9200 switch"],
    "AV Equipment": ["Epson EB-L200F projector", "Logitech Rally conference camera", "Samsung 65in display",
                     "Shure MXA910 ceiling microphone"],
    "Furniture": ["Height-adjustable standing desk", "Herman Miller Aeron chair", "Four-drawer filing cabinet",
                  "Meeting room table, 8 seats"],
    "Vehicles": ["Ford Transit Custom van", "Toyota Corolla pool car", "Electric forklift truck"],
    "Tools": ["Makita cordless drill set", "Fluke 117 multimeter", "Aluminium step ladder, 3 m"],
    "Mobile Devices": ["Apple iPhone 15", "Samsung Galaxy Tab S9", "Zebra TC52 barcode scanner"],
}
LOCATIONS = ["Site A - IT Store", "Site A - Room 101", "Site B - Reception", "Site B - Workshop",
             "Head Office - Floor 2", "Head Office - Server Room", "Warehouse - Bay 4", "Remote - Home Worker"]


def sample_asset(index: int, asset_id: str) -> dict:
    """The index-th sample asset: every field filled, with values varying by index."""
    categories = list(CATALOGUE)
    category = categories[index % len(categories)]
    descriptions = CATALOGUE[category]
    return {
        "asset_id": asset_id,
        "description": descriptions[(index // len(categories)) % len(descriptions)],
        "category": category,
        "location": LOCATIONS[index % len(LOCATIONS)],
        "status": STATUSES[index % len(STATUSES)],
    }


def sample_changes(index: int, asset: dict) -> dict:
    """Changes for an edit: move the asset and flip its status."""
    return {
        "location": LOCATIONS[(index + 3) % len(LOCATIONS)],
        "status": STATUSES[(STATUSES.index(asset["status"]) + 1) % len(STATUSES)],
    }


def seed(token: str, role: str, count: int, config: dict, out=print) -> dict:
    """Register `count` sample assets, edit and delete some, and return counts of what
    was done. Stops at the first error other than an ID that is already taken."""
    done = {"registered": 0, "edited": 0, "deleted": 0, "skipped": 0}
    number = 0
    while done["registered"] < count:
        number += 1
        if number > MAX_ID_NUMBER:
            out("Ran out of free SEED- asset IDs.")
            break
        asset = sample_asset(done["registered"], f"SEED-{number:04d}")
        reply = send_request({"action": "REGISTER", "request_id": new_request_id(),
                              "asset": asset, "token": token}, config)
        if reply.get("code") == DUPLICATE_ASSET:
            done["skipped"] += 1
            continue
        if reply.get("status") != "OK":
            out(f"Could not register {asset['asset_id']} ({reply.get('code')}): {reply.get('message')}")
            break
        stored = reply["stored"]
        done["registered"] += 1
        position = done["registered"]

        if position % EDIT_EVERY == 0:
            reply = send_request({"action": "EDIT", "request_id": new_request_id(), "asset_id": stored["asset_id"],
                                  "expected_version": stored["version"],
                                  "changes": sample_changes(position, stored), "token": token}, config)
            if reply.get("status") != "OK":
                out(f"Could not edit {stored['asset_id']} ({reply.get('code')}): {reply.get('message')}")
                break
            stored = reply["stored"]
            done["edited"] += 1

        if position % DELETE_EVERY == 0 and role == "admin":
            reply = send_request({"action": "DELETE", "request_id": new_request_id(), "asset_id": stored["asset_id"],
                                  "expected_version": stored["version"], "token": token}, config)
            if reply.get("status") != "OK":
                out(f"Could not delete {stored['asset_id']} ({reply.get('code')}): {reply.get('message')}")
                break
            done["deleted"] += 1
    return done


def count_from_arguments(arguments: list) -> int:
    if not arguments:
        return DEFAULT_COUNT
    if len(arguments) == 1 and arguments[0].isdigit() and int(arguments[0]) > 0:
        return int(arguments[0])
    raise ValueError("Usage: python tools/seed.py [number of assets]")


def main(config: dict = None, arguments: list = None) -> None:
    try:
        count = count_from_arguments(sys.argv[1:] if arguments is None else arguments)
    except ValueError as exc:
        print(exc)
        return
    config = config or load_config()
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    try:
        reply = login(username, password, config)
        if reply.get("status") != "OK":
            print(f"Login failed: {reply.get('message')}")
            return
        if reply["role"] != "admin":
            print("You are not an administrator, so no sample assets will be deleted "
                  "and the deleted and deleted_at fields stay empty.")
        done = seed(reply["token"], reply["role"], count, config)
    except (OSError, ValueError) as exc:
        print(f"Could not reach the server: {exc}")
        return
    print(f"Registered {done['registered']} sample assets, edited {done['edited']} "
          f"and deleted {done['deleted']}.")
    if done["skipped"]:
        print(f"Skipped {done['skipped']} asset IDs that were already taken.")


if __name__ == "__main__":
    main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

"""
The command-line client: adds an asset, lists them, or changes one.

    python cli.py add      asks for a new asset's details, then registers it
    python cli.py list     lists every asset with its ID, status and tags
    python cli.py change   changes one asset's status, edits its details, or deletes it

Without a command, it asks which of the three you want. At least one server must
already be running (run start.py first).

If you run this by double-clicking the file (rather than from an already-open terminal),
Windows closes the console the instant the script finishes, too fast to read the server's
response. The pause at the end keeps the window open until you press Enter. Running it
from a terminal you already have open does not need it.
"""

import argparse  # reads which command was asked for

# The shared networking code used by every client.
from common import IN_USE, NOT_IN_USE, STATUSES, generate_asset_tags, new_register_request, send_request

# The fields that can be edited, with what the user is asked for each.
EDITABLE_FIELDS = (("description", "Description"), ("category", "Category"), ("location", "Location"))


# ---------- add ----------

def choose_status() -> str:
    """Ask for a status by number, offering only the ones the server accepts."""
    print("  Status:")
    for number, status in enumerate(STATUSES, start=1):
        print(f"    {number}. {status}")
    while True:
        answer = input(f"  Choose 1-{len(STATUSES)}: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(STATUSES):
            return STATUSES[int(answer) - 1]
        print(f"  Please type a number from 1 to {len(STATUSES)}.")


def prompt_for_asset() -> dict:
    """Ask the user for each field of a new asset record."""
    print("Enter the details of the new asset:")

    # input() prints the given prompt, waits for the user to type a line
    # and press Enter, then returns what they typed as a string.
    # .strip() removes any accidental leading/trailing spaces.
    description = input("  Description (e.g. Dell Latitude 5440 laptop): ").strip()
    category = input("  Category (e.g. IT Equipment): ").strip()
    location = input("  Location (e.g. Site A - IT Store): ").strip()
    status = choose_status()

    # The server validates these fields and rejects anything invalid. It also creates
    # the asset_id, date_registered and tags itself, so those are not sent from here.
    return {
        "description": description,
        "category": category,
        "location": location,
        "status": status,
    }


def add_asset() -> None:
    """Ask for a new asset's details, then register it."""
    new_asset = prompt_for_asset()

    # If this fails, show a clear message rather than a traceback. After an error,
    # running this again is safe: if the first attempt did work, the server replies
    # DUPLICATE_ASSET with the existing asset's ID, rather than storing the item twice.
    try:
        response = send_request(new_register_request(new_asset))
    except (OSError, ValueError) as exc:
        print(f"Could not register the asset: {exc}")
        return

    # Show the raw reply so the user can see what the server did.
    print("Server response:", response)


# ---------- list ----------

def list_assets() -> None:
    """Print every asset's ID, status and tags."""
    # If this fails, show a clear message rather than a traceback.
    try:
        response = send_request({"action": "LIST"})
    except (OSError, ValueError) as exc:
        print(f"Could not retrieve the asset list: {exc}")
        return

    if response.get("status") != "OK":
        print("Server returned an error:", response.get("detail"))
        return

    assets = response.get("assets", [])
    if not assets:
        print("No assets registered yet.")
        return

    print(f"{len(assets)} asset(s) registered, according to the {response.get('served_by', 'unknown')} server:\n")
    for asset in assets:
        # Assets saved before tagging was added have no tags, so those are worked
        # out here for display, as the GUI does.
        tags = asset.get("tags") or generate_asset_tags(asset)
        print(f"{asset.get('asset_id', 'Unknown')} ({asset.get('status', 'Unknown')}): {tags}")


# ---------- change ----------

def prompt_for_changes(asset: dict) -> dict:
    """Ask for a new value for each field, where pressing Enter keeps the current one.
    Returns only the fields the user changed."""
    print("Type a new value, or press Enter to keep the one shown.")
    changes = {}
    for field, label in EDITABLE_FIELDS:
        value = input(f"  {label} [{asset.get(field, '')}]: ").strip()
        if value and value != asset.get(field):
            changes[field] = value

    choices = ", ".join(f"{number} {status}" for number, status in enumerate(STATUSES, start=1))
    while True:
        answer = input(f"  Status [{asset.get('status', '')}] ({choices}): ").strip()
        if not answer:
            break
        if answer.isdigit() and 1 <= int(answer) <= len(STATUSES):
            if STATUSES[int(answer) - 1] != asset.get("status"):
                changes["status"] = STATUSES[int(answer) - 1]
            break
        print(f"  Please type a number from 1 to {len(STATUSES)}, or press Enter.")
    return changes


def prompt_for_request(asset: dict) -> object:
    """Ask the user what to do, returning the request, or None if they cancel.

    The request says which version of the asset the user was shown. If someone else
    changes it while they decide, the server replies CONFLICT rather than overwriting
    a change the user has not seen.
    """
    asset_id = asset["asset_id"]
    print(f"{asset_id}: {asset.get('description')} ({asset.get('status')})")
    print(f"  1. Mark as {NOT_IN_USE}")
    print(f"  2. Mark as {IN_USE}")
    print("  3. Delete it for good")
    print("  4. Edit its details")
    choice = input("Choose 1, 2, 3 or 4: ").strip()

    request = {"asset_id": asset_id, "expected_version": asset.get("version", 0)}

    if choice == "1":
        return {"action": "SET_STATUS", "status": NOT_IN_USE, **request}
    if choice == "2":
        return {"action": "SET_STATUS", "status": IN_USE, **request}
    if choice == "3":
        # A delete cannot be undone, so ask first.
        if input(f"Type DELETE to delete {asset_id}: ").strip() == "DELETE":
            return {"action": "DELETE", **request}
        print("Nothing was deleted.")
        return None
    if choice == "4":
        changes = prompt_for_changes(asset)
        if not changes:
            print("Nothing was changed.")
            return None
        return {"action": "EDIT", "changes": changes, **request}

    print("That is not one of the choices.")
    return None


def change_asset() -> None:
    """Ask for an asset's ID, then change its status, edit it or delete it."""
    asset_id = input("Asset ID (python cli.py list shows them): ").strip()

    # If this fails, show a clear message rather than a traceback.
    try:
        listing = send_request({"action": "LIST"})
        if listing.get("status") != "OK":
            print("Server returned an error:", listing.get("detail"))
            return

        asset = next((a for a in listing.get("assets", []) if a.get("asset_id") == asset_id), None)
        if asset is None:
            print(f"No asset has the ID {asset_id!r}.")
            return

        request = prompt_for_request(asset)
        if request is None:
            return

        response = send_request(request)
    except (OSError, ValueError) as exc:
        print(f"Could not complete the request: {exc}")
        return

    # Show the raw reply so the user can see what the server did.
    print("Server response:", response)


# ---------- choosing a command ----------

# Each command's name, what it does, and the function that does it.
COMMANDS = {
    "add": ("Add an asset", add_asset),
    "list": ("List the assets", list_assets),
    "change": ("Change, edit or delete an asset", change_asset),
}


def ask_for_command() -> str:
    """Ask which command to run, for when none was given, such as after a double-click."""
    names = list(COMMANDS)
    for number, name in enumerate(names, start=1):
        print(f"  {number}. {COMMANDS[name][0]}")
    while True:
        answer = input(f"Choose 1-{len(names)}: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        print(f"Please type a number from 1 to {len(names)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add, list or change assets.")
    parser.add_argument("command", nargs="?", choices=COMMANDS, help="what to do (asked for if left out)")
    command = parser.parse_args().command or ask_for_command()
    COMMANDS[command][1]()


# Only run this if the file is executed directly (not imported).
if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print()  # the user closed the input or pressed Ctrl+C, so just stop
    try:
        # Keep the console window open until the user presses Enter, so the output
        # above doesn't disappear immediately (see the top of this file).
        input("\nPress Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass  # no keyboard attached (e.g. run non-interactively), so just exit

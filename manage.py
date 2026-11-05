"""
Management client.

Changes the status of one asset, edits its details, or deletes it. At least one server
must already be running (run start.py or server.py first), and receive.py shows each
asset's ID.

    python manage.py

If you run this by double-clicking the file (rather than from an already-open
terminal), the pause at the end keeps the window open, as in send.py.
"""

# The shared networking code used by every client.
from common import IN_USE, NOT_IN_USE, STATUSES, send_request

# The fields that can be edited, with what the user is asked for each.
EDITABLE_FIELDS = (("description", "Description"), ("category", "Category"), ("location", "Location"))


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


def main() -> None:
    asset_id = input("Asset ID: ").strip()

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


# Only run this if the file is executed directly (not imported).
if __name__ == "__main__":
    main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        # No keyboard attached (e.g. run non-interactively) - just exit.
        pass

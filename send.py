"""
Registration client.

Prompts you for the details of one asset, then registers it with the server.
The server must already be running (start server.py first).

    python send.py

If you run this by double-clicking the file (rather than from an already-open
terminal), Windows closes the console the instant the script finishes. The pause at
the end keeps the window open until you press Enter.
"""

from common import STATUSES, new_request_id, send_request


def prompt_for_status() -> str:
    """Offer the allowed statuses as a numbered choice, so only a valid one can be picked."""
    options = "  ".join(f"{number}) {status}" for number, status in enumerate(STATUSES, start=1))
    while True:
        choice = input(f"  Status - {options} (required): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(STATUSES):
            return STATUSES[int(choice) - 1]
        print(f"    Please type a number from 1 to {len(STATUSES)}.")


def prompt_for_asset() -> dict:
    """Ask for each field of a new asset. Each prompt names the field and gives an example."""
    print("Enter the details of the new asset. Every field is required.")
    return {
        "asset_id": input("  Asset ID, e.g. AST-0001 (letters, digits, - or _): ").strip(),
        "description": input("  Description, e.g. Dell Latitude 5440 laptop: ").strip(),
        "category": input("  Category, e.g. IT Equipment: ").strip(),
        "location": input("  Location, e.g. Site A - IT Store: ").strip(),
        "status": prompt_for_status(),
    }


def main(config: dict = None) -> None:
    new_asset = prompt_for_asset()
    try:
        request = {"action": "REGISTER", "request_id": new_request_id(), "asset": new_asset}
        response = send_request(request, config)
    except (OSError, ValueError) as exc:
        print(f"Could not register the asset: {exc}")
        return

    if response.get("status") == "OK":
        stored = response["stored"]
        print(f"Registered {stored['asset_id']} at {stored['created_at']}.")
    else:
        print(f"Not registered ({response.get('code')}): {response.get('message')}")


if __name__ == "__main__":
    main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

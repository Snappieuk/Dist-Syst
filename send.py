"""
"Send" client (STARTER CODE).

Prompts you for the details of one asset, then registers it with the
server. The server must already be running (start server.py first).

    python send.py

Deliberately basic: there is almost no validation of what you type in
before it is sent. Improving that is part of the assignment.

If you run this by double-clicking the file (rather than from an
already-open terminal), Windows closes the console the instant the
script finishes — too fast to read the server's response. The pause at
the end of main() below keeps the window open until you press Enter;
running the script from a terminal you already have open does not need
it.
"""

import datetime  # used to stamp each new asset with today's date

# Import the shared constants/helpers used by every part of the prototype.
from common import send_request


def prompt_for_asset() -> dict:
    """Ask the user for each field of a new asset record."""
    print("Enter the details of the new asset:")

    # input() prints the given prompt, waits for the user to type a line
    # and press Enter, then returns what they typed as a string.
    # .strip() removes any accidental leading/trailing spaces.
    asset_id = input("  Asset ID (e.g. AST-0001): ").strip()
    description = input("  Description (e.g. Dell Latitude 5440 laptop): ").strip()
    category = input("  Category (e.g. IT Equipment): ").strip()
    location = input("  Location (e.g. Site A - IT Store): ").strip()
    status = input("  Status (e.g. In Use): ").strip()

    # TODO (student): this does not check that the fields above were
    # actually filled in, or that asset_id is not already in use.

    # Build and return the asset as a dict, matching what the server and
    # receive.py expect. date_registered is filled in automatically
    # rather than asked for, using today's date in YYYY-MM-DD format.
    return {
        "asset_id": asset_id,
        "description": description,
        "category": category,
        "location": location,
        "status": status,
        "date_registered": datetime.date.today().isoformat(),
    }


def main() -> None:
    # Ask the user for the asset details before we even open a connection.
    new_asset = prompt_for_asset()

    # Send a REGISTER request containing the new asset. send_request() adds the
    # protocol version and reads the server's address from config.json.
    try:
        response = send_request({"action": "REGISTER", "asset": new_asset})
    except (OSError, ValueError) as exc:
        print(f"Could not register the asset: {exc}")
        return

    # Show the raw reply so the user can see what the server did.
    print("Server response:", response)


# Only run this if the file is executed directly (not imported).
if __name__ == "__main__":
    main()
    try:
        # Keep the console window open until the user presses Enter, so
        # the output above doesn't disappear immediately (see the module
        # docstring for why this matters on Windows).
        input("\nPress Enter to close...")
    except EOFError:
        # No keyboard attached (e.g. run non-interactively) - just exit.
        pass

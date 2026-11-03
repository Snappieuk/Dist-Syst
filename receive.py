"""
Query client.

Asks the server for the current list of registered assets and prints
them. The server must already be running (start server.py first).

    python receive.py

If you run this by double-clicking the file (rather than from an
already-open terminal), Windows closes the console the instant the
script finishes — too fast to read the output. The pause at the end of
main() below keeps the window open until you press Enter; running the
script from a terminal you already have open does not need it.
"""

# The shared networking code used by every client.
from common import send_request


def main() -> None:
    # Ask the server for every asset it currently has stored. If that fails,
    # show a clear message rather than a traceback.
    try:
        response = send_request({"action": "LIST"})
    except (OSError, ValueError) as exc:
        print(f"Could not retrieve the asset list: {exc}")
        return

    # If the server reported an error, show it and stop here.
    if response.get("status") != "OK":
        print("Server returned an error:", response.get("detail"))
        return

    # Otherwise pull the list of assets out of the reply (defaulting
    # to an empty list if, for some reason, the field is missing).
    assets = response.get("assets", [])

    if not assets:
        # The list came back empty - nothing has been registered yet.
        print("No assets registered yet.")
        return

    # Print a short summary, then one line per asset record.
    print(f"{len(assets)} asset(s) registered:\n")
    for asset in assets:
        print(asset)


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

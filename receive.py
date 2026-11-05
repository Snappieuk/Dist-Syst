"""
Query client.

Asks the server for the registered assets and prints them. The server must already
be running (start server.py first).

    python receive.py

If you run this by double-clicking the file (rather than from an already-open
terminal), Windows closes the console the instant the script finishes. The pause at
the end keeps the window open until you press Enter.
"""

from common import send_request


def format_asset(asset: dict) -> str:
    """One line per asset, with every field."""
    return (
        f"{asset['asset_id']} | {asset['description']} | {asset['category']} | "
        f"{asset['location']} | {asset['status']} | registered {asset['created_at']}"
    )


def main(config: dict = None) -> None:
    try:
        response = send_request({"action": "LIST"}, config)
    except (OSError, ValueError) as exc:
        print(f"Could not retrieve the asset list: {exc}")
        return

    if response.get("status") != "OK":
        print(f"Server returned an error ({response.get('code')}): {response.get('message')}")
        return

    assets = response["assets"]
    if not assets:
        print("No assets registered yet.")
        return

    print(f"{len(assets)} asset(s) registered:\n")
    print("ID | Description | Category | Location | Status | Registered")
    for asset in assets:
        print(format_asset(asset))


if __name__ == "__main__":
    main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

"""
Query client.

Asks the server for the registered assets and prints them. The server must already
be running (start it with python server.py first).

    python clients/receive.py                  (every asset)
    python clients/receive.py laptop           (assets with "laptop" in any field)
    python clients/receive.py location=site a  (assets whose location contains "site a")

If you run this by double-clicking the file (rather than from an already-open
terminal), Windows closes the console the instant the script finishes. The pause at
the end keeps the window open until you press Enter.
"""

import sys

import os

# common.py is in the project folder, one level up from this one.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import log_in_from_prompt, send_request


def search_from_arguments(arguments: list) -> dict:
    """Turn the command-line words into LIST search parameters.

    "field=text" searches one field; anything else searches every field.
    """
    request = {}
    text = " ".join(arguments).strip()
    if not text:
        return request
    field, equals, value = text.partition("=")
    if equals:
        request["filters"] = {field.strip(): value.strip()}
    else:
        request["query"] = text
    return request


def format_asset(asset: dict) -> str:
    """One line per asset, with every field."""
    return (
        f"{asset['asset_id']} | {asset['description']} | {asset['category']} | "
        f"{asset['location']} | {asset['status']} | registered {asset['created_at']}"
    )


def main(config: dict = None, arguments: list = None) -> None:
    search = search_from_arguments(sys.argv[1:] if arguments is None else arguments)
    token = log_in_from_prompt(config)
    if token is None:
        return
    try:
        response = send_request({"action": "LIST", "token": token, **search}, config)
    except (OSError, ValueError) as exc:
        print(f"Could not retrieve the asset list: {exc}")
        return

    if response.get("status") != "OK":
        print(f"Server returned an error ({response.get('code')}): {response.get('message')}")
        return

    assets = response["assets"]
    if not assets:
        print("No assets match that search." if search else "No assets registered yet.")
        return

    print(f"{len(assets)} asset(s) {'found' if search else 'registered'}:\n")
    print("ID | Description | Category | Location | Status | Registered")
    for asset in assets:
        print(format_asset(asset))


if __name__ == "__main__":
    main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

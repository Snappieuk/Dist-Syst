"""
"Receive" client (STARTER CODE).

Asks the server for the current list of registered assets and prints
them. The server must already be running (start server.py first).

    python receive.py

If you run this by double-clicking the file (rather than from an
already-open terminal), Windows closes the console the instant the
script finishes — too fast to read the output. The pause at the end of
main() below keeps the window open until you press Enter; running the
script from a terminal you already have open does not need it.
"""

import socket  # provides the networking building blocks (sockets)

# Import the shared constants/helpers used by every part of the prototype.
from common import HOST, PORT, receive_message, send_message


def main() -> None:
    # Create a TCP/IPv4 socket. `with` closes it automatically on exit.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Connect to the server at the shared HOST/PORT.
        sock.connect((HOST, PORT))

        # Ask the server for every asset it currently has stored.
        send_message(sock, {"action": "LIST"})

        # Wait for, and decode, the server's reply.
        response = receive_message(sock)

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

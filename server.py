"""
Asset Management System server (STARTER CODE).

Listens for one connection at a time and understands two request actions:

    {"action": "REGISTER", "asset": {...}}   -> stores the asset, replies with a status
    {"action": "LIST"}                       -> replies with every stored asset

Deliberately basic — this is a starting point, not a finished system:
  - only one client is served at a time (no threading/concurrency)
  - assets are stored in memory only and are lost when the server stops
  - there is almost no validation of incoming data
  - there is no authentication or encryption

Reviewing, extending, testing, and documenting these limitations is part
of the assignment.
"""

import socket  # provides the networking building blocks (sockets)

# Import the shared constants/helpers used by every part of the prototype.
from common import HOST, PORT, receive_message, send_message

assets = []  # in-memory "database" of registered assets (a plain list of dicts)


def handle_client(conn: socket.socket) -> None:
    # `with conn:` guarantees the connection is closed automatically once
    # this block finishes, even if an error happens inside it.
    with conn:
        try:
            # Wait for, and decode, one complete message from this client.
            request = receive_message(conn)
        except (ConnectionError, ValueError) as exc:
            # ValueError covers JSON that doesn't parse; ConnectionError
            # covers a client that disconnects mid-message.
            send_message(conn, {"status": "ERROR", "detail": str(exc)})
            return  # nothing more we can do with this connection

        # Every request must say what it wants done via an "action" field.
        action = request.get("action")

        if action == "REGISTER":
            # Pull the asset details out of the request.
            asset = request.get("asset")

            # TODO (student): validate `asset` properly before storing it
            # (e.g. required fields present, sensible types, no duplicate
            # asset_id) and decide what should happen if it is invalid.
            assets.append(asset)  # add it to the in-memory list

            # Tell the client the registration succeeded, and echo back
            # what was stored so it can be confirmed on screen.
            send_message(conn, {"status": "OK", "stored": asset})

        elif action == "LIST":
            # Send back the full current list of assets as-is.
            send_message(conn, {"status": "OK", "assets": assets})

        else:
            # Any action we don't recognise is treated as an error.
            send_message(conn, {"status": "ERROR", "detail": f"Unknown action: {action!r}"})


def main() -> None:
    # Create a TCP/IPv4 socket. `with` closes it automatically on exit.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        # Allow the port to be reused immediately after the server stops,
        # instead of Windows/Linux holding on to it for a short while.
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind the socket to our chosen host/port, then start listening
        # for incoming connections (this does not accept any yet).
        server_sock.bind((HOST, PORT))
        server_sock.listen()

        print(f"Asset Management server listening on {HOST}:{PORT}")
        print("Press Ctrl+C to stop.")

        # Serve clients forever, one at a time, until the server is stopped.
        while True:
            # Block here until a client connects; `conn` is a new socket
            # dedicated to that one client, `addr` is their address.
            conn, addr = server_sock.accept()
            print(f"Connection from {addr}")

            # Handle this client's single request/response fully before
            # looping back round to accept() the next one.
            handle_client(conn)


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

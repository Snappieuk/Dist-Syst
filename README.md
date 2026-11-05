# Asset Management System — Basic Starter Code

This is the starter code referred to in the Assignment 2 of 2 brief. It is
**deliberately basic** — you are expected to critically review, extend,
test, and document it as part of the assignment, not submit it unchanged.

## Files

- `common.py` — shared helpers: sends and receives JSON messages over a
  TCP socket, using a 4-byte length prefix so each message is framed
  correctly.
- `server.py` — accepts `REGISTER` and `LIST` requests and stores assets
  in memory.
- `send.py` — prompts you for the details of a new asset, then registers
  it with the server.
- `receive.py` — retrieves and prints the current list of assets.

## Running it

Requires Python 3.8+ and no third-party packages.

1. Open a terminal and start the server:

   ```
   python server.py
   ```

2. In a second terminal, register an asset (you'll be asked for its
   details):

   ```
   python send.py
   ```

3. In a third terminal, retrieve the current list:

   ```
   python receive.py
   ```

`send.py` and `receive.py` both end with a "Press Enter to close..."
prompt, so if you run them by double-clicking the file instead of from
an open terminal, the window stays open long enough to read the result.

## Known limitations (your starting point, not the end point)

- Only one client is handled at a time — there is no threading or
  concurrency, so a second client has to wait for the first to finish.
- Assets are stored in memory only and are lost when the server stops.
- There is almost no validation of incoming data.
- There is no authentication or encryption of data in transit.
- Error handling is minimal.

Part of the assignment is to identify, discuss, and where appropriate
address limitations like these.

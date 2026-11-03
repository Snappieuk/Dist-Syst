# Asset Management System

A simple distributed prototype: a central server, a registration client and a
query client, which register and retrieve asset records. They communicate over
TCP sockets using JSON messages.

Each asset has an asset ID, description, category, location, status and
registration date.

## Files

- `common.py` — shared helpers: sends and receives JSON messages over a TCP
  socket, using a 4-byte length prefix so each message is framed correctly.
- `server.py` — accepts `REGISTER` and `LIST` requests, and saves assets to
  `assets.json`.
- `send.py` — the registration client: prompts for a new asset and registers it.
- `receive.py` — the query client: retrieves and prints the current assets.
- `gui.py` — a Tkinter window that can both register and list assets.
- `test_system.py` — the automated tests.

## Running it

Requires Python 3.8+ and no third-party packages.

1. In one terminal, start the server:

   ```
   python server.py
   ```

2. In a second terminal, register an asset (you'll be asked for its details):

   ```
   python send.py
   ```

3. In a third terminal, retrieve the current list:

   ```
   python receive.py
   ```

You can also use the graphical interface once the server is running:

```
python gui.py
```

`send.py` and `receive.py` both end with a "Press Enter to close..." prompt, so
if you run them by double-clicking the file, the window stays open long enough
to read the result.

### Running across two machines

Set `HOST` in `common.py` to the server machine's IP address, on both machines.
The server's firewall must allow the port.

## Running the tests

```
python -m unittest -v test_system
```

The tests run a real server on a spare port with a temporary data file, so they
do not touch `assets.json`. One test deliberately causes a failed save, so a
logged error and traceback in the output are expected.

## Messages

Each message is a 4-byte length followed by JSON. Each connection carries one
request and one reply.

| Request | Successful reply |
|---|---|
| `{"action": "REGISTER", "asset": {...}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "LIST"}` | `{"status": "OK", "assets": [...]}` |

Failures look like `{"status": "ERROR", "code": "DUPLICATE_ID", "detail": "..."}`.

| Code | Meaning |
|---|---|
| `BAD_REQUEST` | The message is malformed or the asset is invalid |
| `DUPLICATE_ID` | The asset ID is already registered |
| `TOO_LARGE` | The message is over the 10 MiB limit |
| `INTERNAL` | The server failed, for example because the disk is full |

## Improvements made to the starter code

- **Persistence:** assets are saved to `assets.json` after every change and
  loaded at startup.
- **Concurrency:** each client has its own thread, so a slow client does not
  delay the others. A lock protects the shared data.
- **Validation:** the server checks every field and rejects duplicate IDs. It also
  sets the registration date, because client clocks may differ.
- **Reliability:** a client that disconnects abruptly or sends invalid data used
  to crash the server. Each failure now only affects that client.
- **Timeouts and limits:** idle clients are disconnected, clients do not wait
  forever, and message size is capped.
- **Error handling:** errors have clear codes, and clients show a message instead
  of a traceback when the server is down.

### ACID properties

Each registration has these properties:

| Property | How it is provided |
|---|---|
| Atomicity | The save writes a temporary file and swaps it into place in one step. If the save fails, the asset is removed from memory again. |
| Consistency | Only valid assets with unique IDs are stored. |
| Isolation | A lock makes checking, storing and saving one indivisible step. |
| Durability | The file is forced to disk before the client gets its reply. |

## Known limitations

These are outside the project's scope, or were left out to keep the prototype simple:

- **No authentication or encryption:** anyone who can reach the server can read
  and add assets, and data is sent as plain text. Use it only on a trusted network.
- **Single point of failure:** there is one server, with no backup or replication.
  If it stops, clients cannot work until it restarts, although saved data is kept.
- **Register and list only:** assets cannot be searched, updated or removed.
- **No automatic retry:** if the reply to a registration is lost, the user does not
  know whether it worked. Running it again is safe, and shows `DUPLICATE_ID` if
  the first attempt succeeded.
- **Limited scale:** every registration rewrites the whole file and happens one at
  a time, every asset is held in memory, and each `LIST` returns everything.
- **Unlimited threads:** a flood of connections could exhaust the server. The idle
  timeout applies to each read, so a client sending one byte at a time could also
  hold a thread for a long time.

## Opportunities for further development

Update and delete actions, search and paging, a database in place of the JSON file,
login and encryption, and a backup server.

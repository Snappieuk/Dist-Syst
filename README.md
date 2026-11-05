# Asset Management System

A simple distributed prototype: a central server with a backup, a registration
client and a query client, which register and retrieve asset records. They
communicate over TCP sockets using JSON messages.

Each asset has an asset ID, description, category, location, status, registration
date and tags. The server sets the date and generates the tags.

## Files

- `common.py` — shared helpers: sends and receives JSON messages over a TCP
  socket, using a 4-byte length prefix so each message is framed correctly. It
  also holds the server addresses, error codes, tag generation and client failover.
- `server.py` — accepts `REGISTER` and `LIST` requests, and saves assets to
  `assets.json`. Run with `--backup`, it acts as the backup server and uses
  `assets_backup.json`.
- `send.py` — the registration client: prompts for a new asset and registers it.
- `receive.py` — the query client: retrieves the current assets and prints each
  one's ID and tags.
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

### Running the backup server

The clients work with the primary alone, but for redundancy, start the backup in
another terminal:

```
python server.py --backup
```

Clients always try the primary first, and automatically use the backup if the
primary can't be reached. Each server copies new assets from the other every two
seconds, so a server that was down catches up by itself when it restarts.

### Running across several machines

Edit `SERVERS` in `common.py` to list each server's IP address, and use the same
list on every machine. Each server's firewall must allow its port.

## Running the tests

```
python -m unittest -v test_system
```

The tests run real servers on spare ports with temporary data files, so they do
not touch your data. One of them runs the primary and backup as separate programs
and stops the primary part-way through. Another deliberately causes a failed save,
so a logged error and traceback in the output are expected.

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
  sets the registration date, because client clocks may differ, and generates
  each asset's tags.
- **Reliability:** a client that disconnects abruptly or sends invalid data used
  to crash the server. Each failure now only affects that client.
- **Timeouts and limits:** idle clients are disconnected, clients do not wait
  forever, and message size is capped.
- **Error handling:** errors have clear codes, and clients show a message instead
  of a traceback when no server can be reached.
- **Redundancy:** a backup server takes over automatically if the primary is down,
  and the two servers copy new assets from each other.

### ACID properties

Each registration has these properties on the server that accepts it. Between the
two servers, data is only eventually consistent (see the limitations below).

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
- **A simple backup, not high availability:** full high availability is outside
  the project's scope, so this backup has limits:
  - The servers can differ for a couple of seconds. If the primary's disk were
    destroyed in that time, its newest assets would be lost.
  - If each server accepts a different asset with the same ID while clients can
    only reach one or the other, the primary's copy wins. The backup's copy is
    discarded, even though its client was told the registration succeeded.
  - Using the backup takes a client a few seconds longer, because it tries the
    primary first. If both servers are down, clients show an error.
  - Only one backup is supported, and restarting a failed server is a manual step.
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
login and encryption, and stronger replication, where a registration is confirmed
only after both servers have saved it.

# Asset Management System

A distributed system for registering, retrieving, updating and deleting asset
records. It is made of separate programs that run as independent processes, possibly
on different machines, and cooperate by sending JSON messages over TCP sockets:

```
  send.py   receive.py   manage.py   gui.py        clients
      \          |           |        /
       \         |           |       /             tries the primary first,
        v        v           v      v              then the backup
    +-----------------+              +-----------------+
    |    server.py    |  heartbeats  |    backup.py    |
    |    (primary)    |<-------------|    (backup)     |
    |                 |  each change |                 |
    |  accepts all    |------------->|  on standby:    |
    |  changes, and   |  reconcile   |  applies copies,|
    |  orders them    |<------------>|  refuses clients|
    +-----------------+              +-----------------+
       assets.json                     assets_backup.json
```

Each asset has an asset ID, description, category, location, status, registration
date and tags. The server creates the ID, sets the date and generates the tags. The
status says whether the item is currently being used, such as "In Use" or "Not In Use".

## Files

- `server.py` — the **primary** server. It accepts every change, puts changes in one
  order, and copies each to the backup. Its data is in `assets.json`.
- `backup.py` — the **backup** server, a separate program. It keeps a copy of the data
  in `assets_backup.json`, watches the primary with heartbeats, and takes over if the
  primary fails.
- `node.py` — what both servers share: the data, the rules for changing it, and the
  network loop.
- `common.py` — what servers and clients share: JSON messages over TCP with a 4-byte
  length prefix, the server addresses, error codes, tag generation and client failover.
- `send.py` — registers a new asset.
- `receive.py` — lists the assets, with each one's ID, status and tags.
- `manage.py` — marks an asset as "Not In Use" or "In Use", or deletes it.
- `gui.py` — a Tkinter window that can do all of the above. Select an asset in the
  table to change or delete it. The status line shows which server answered.
- `test_system.py` — the automated tests.

## Running it

Requires Python 3.8+ and no third-party packages. Run each program in its own terminal:

```
python server.py
python backup.py
python gui.py
```

Or, instead of the GUI, use `python send.py`, `python receive.py` and `python manage.py`.
Those three end with a "Press Enter to close..." prompt, so if you run them by
double-clicking, the window stays open long enough to read the result.

The system works without the backup, but cannot then survive the primary failing.

### Demonstrating it

With both servers and the GUI running, and watching the server logs:

1. **Replication:** register an asset. The primary logs `Change N copied to the backup`,
   and the backup logs `Applied change N from the primary`.
2. **Failure detection and failover:** stop `server.py` with Ctrl+C, then register
   another asset. The backup logs `TAKING OVER`, and the GUI's status line says `served
   by the backup server`. The new asset's ID starts `AST-B`.
3. **Recovery:** start `server.py` again. It reconciles with the backup, which logs
   `Returning to standby`. Nothing is lost, and the primary is in charge again.
4. **Simultaneous changes:** open two GUI windows showing the same asset, with the status
   "In Use". Click "Mark Not In Use" in one, then "Delete Asset" in the other without
   refreshing. The delete is refused, because that window has not seen the change.

### Running across several machines

Edit `SERVERS` in `common.py` to list each server's IP address, and use the same
list on every machine. Each server's firewall must allow its port. This has been run
as separate processes on one machine, and not yet on separate machines.

## Running the tests

```
python -m unittest -v test_system
```

The tests run real servers on spare ports with temporary data files, so they do not
touch your data. `DistributedTest` runs `server.py` and `backup.py` as separate
programs and kills them part-way through. Some tests deliberately cause a failed save,
so logged errors and tracebacks in the output are expected.

## How this works as a distributed system

| Concept | How it is done here | Where |
|---|---|---|
| Independent processes that communicate only by messages | The servers and clients share no memory, and can run on different machines | `common.py`: `send_message`, `receive_message` |
| External data representation | Length-prefixed UTF-8 JSON, independent of language and platform | `common.py`: `send_message` |
| Replication | The backup holds a full copy of the data, in its own file | `backup.py`: `apply_change` |
| Synchronous replication | A client is told a change worked only after the backup has saved it | `server.py`: `replicate_change` |
| Single leader, for one order of events | Only the primary accepts changes, so all servers see them in one order | `node.py`: `accepts_writes` |
| Total ordering | Every change has a sequence number, and a gap is detected | `backup.py`: `apply_change` |
| Failure detection | The backup pings the primary every second, and 3 missed heartbeats count as a failure | `backup.py`: `monitor_primary` |
| Failover | The backup promotes itself, and clients move to it without the user doing anything | `backup.py`: `monitor_primary`, `common.py`: `send_request` |
| Recovery and reconciliation | A returning primary takes the backup's changes, merges them and takes back control | `server.py`: `catch_up_backup`, `merge_state` |
| Conflict resolution | Deletes win, then the higher version, then the primary | `server.py`: `merge_state` |
| Idempotent requests | A retry carries the same `request_id`, so it cannot create a second asset | `node.py`: `register_asset` |
| Unique IDs without coordination | Each server issues IDs from its own range, and checks each before issuing it | `node.py`: `generate_asset_id` |
| Concurrency control | A lock makes each change one indivisible step | `node.py`: `assets_lock` |
| Optimistic concurrency control | A change names the version it was based on, and is refused if that is out of date | `node.py`: `version_conflict` |
| Tombstones | Deleted IDs are kept, so a delete cannot be undone by another server's old copy | `node.py`: `deleted_ids` |
| Independent failure | A client or server failing does not bring down the others | `node.py`: `handle_client` |
| The CAP trade-off | If the network splits, each server stays available, and consistency is restored afterwards | see the limitations |

## Messages

Each message is a 4-byte length followed by JSON. Each connection carries one
request and one reply. Every reply also has `served_by`, naming the server that answered.

From clients:

| Request | Successful reply |
|---|---|
| `{"action": "REGISTER", "request_id": "...", "asset": {...}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "LIST"}` | `{"status": "OK", "assets": [...]}` |
| `{"action": "SET_STATUS", "asset_id": "...", "status": "Not In Use", "expected_version": 1}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "DELETE", "asset_id": "...", "expected_version": 1}` | `{"status": "OK", "deleted": "..."}` |

`request_id` is a random value chosen by the client for each new registration, and
sending the same one again gives the same asset. `expected_version` is the version of
the asset the client last saw. Both are optional, but leaving them out loses the
protection they give.

Between the servers:

| Request | Purpose |
|---|---|
| `{"action": "PING"}` | The heartbeat, in both directions |
| `{"action": "REPLICATE", "seq": 5, "asset": {...}}` | Copies one change to the backup |
| `{"action": "HANDOVER"}` | Tells the backup to go on standby and send all its data |
| `{"action": "INSTALL_STATE", "assets": [...], "deleted": {...}, "seq": 5}` | Replaces the backup's data with the reconciled copy |

Failures look like `{"status": "ERROR", "code": "NOT_FOUND", "detail": "..."}`.

| Code | Meaning |
|---|---|
| `BAD_REQUEST` | The message is malformed or the asset is invalid, including any field over 1000 characters |
| `DUPLICATE_ASSET` | The same item is already registered. `existing_id` gives its ID |
| `CONFLICT` | Someone else changed the asset since the client read it |
| `NOT_FOUND` | No asset has that ID on any server that replied |
| `NOT_PRIMARY` | This server is on standby. Clients wait and retry while the backup takes over |
| `TOO_LARGE` | The request or the reply is over the 10 MiB limit |
| `INTERNAL` | The server failed, for example because the disk is full |
| `NOT_STANDBY`, `OUT_OF_SYNC` | Between the servers only: the backup cannot take this copy, so the primary sends all its data instead |

## How duplicates and simultaneous changes are prevented

- **Asset IDs:** the server generates each ID and checks it against every stored and
  deleted ID, issuing it only if unused. IDs count up, and the lock means nothing can
  take an ID between the check and storing it. The primary issues `AST-P000001` and so
  on, and the backup `AST-B000001`, so they cannot issue the same ID even if they cannot
  reach each other. A deleted ID is never issued again.
- **Duplicate items:** two assets with the same description, category and location are
  the same item, ignoring capitals, spacing and punctuation. The second is rejected with
  `DUPLICATE_ASSET`. To register two identical items, make their descriptions differ,
  for example with a serial number. A deleted item can be registered again, with a new ID.
- **Retries:** if a reply is lost, the client's retry carries the same `request_id`, and
  gets the existing asset rather than an error.
- **Simultaneous requests:** the lock makes "check, change, save, copy to the backup" one
  indivisible step, so requests take effect strictly one after another.
- **Simultaneous edits:** if two people act on the same version of an asset, the first
  wins and the second gets `CONFLICT`, then sees the latest data.
- **Between servers:** only one server normally accepts changes, so there is one order of
  events. If both did while cut off from each other, see the limitations.

## Improvements made to the starter code

- **Persistence:** data is saved after every change and loaded at startup.
- **Concurrency:** each client has its own thread, so a slow client does not delay the
  others. A lock protects the shared data.
- **Validation:** the server checks every field and limits each to 1000 characters. It
  also sets the registration date, because client clocks may differ, and generates each
  asset's tags, in any language.
- **Reliability:** a client that disconnects abruptly or sends invalid data used to crash
  the server. Each failure now only affects that client.
- **Timeouts and limits:** idle clients are disconnected, clients do not wait forever, and
  message size is capped.
- **Error handling:** errors have clear codes, and clients show a message instead of a
  traceback when no server can be reached.
- **Redundancy:** see the sections above.

### ACID properties

Each change has these properties. While both servers are running, it is on both disks
before the client gets its reply.

| Property | How it is provided |
|---|---|
| Atomicity | The save writes a temporary file and swaps it into place in one step. If the save fails, the change is undone in memory too. |
| Consistency | Only valid assets with unique IDs and identities are stored. |
| Isolation | A lock makes checking, changing and saving one indivisible step. |
| Durability | The file, and on Linux and macOS the folder holding it, is forced to disk before the client gets its reply. |

## Known limitations

- **A network split lets both servers accept changes:** two servers cannot tell a failed
  partner from an unreachable one, and deciding that fairly needs a majority, so at least
  three servers. If the two are cut off from each other while both are running, the backup
  takes over while the primary carries on, which keeps the system available. When they
  reconnect, the primary reconciles the data, and these changes are lost even though their
  clients were told they worked:
  - If the same item was registered on both, the primary's record is kept and the backup's
    ID is retired, so anyone who noted that ID will find it gone.
  - If both changed an asset's status, the copy with more changes wins, or the primary's
    if they made the same number.
  - A delete on either server beats any status change.
- **A failover takes a few seconds:** the first change after the primary fails took about 8
  seconds when timed on one Windows machine, most of it waiting for 3 missed heartbeats.
  Each client also tries the primary first on every request, which took about 2 more seconds
  each time. If both servers are down, clients show an error.
- **Only one backup is supported**, and restarting a failed server is a manual step.
- **An out-of-date backup can take over:** if the backup was down when the primary took
  changes, and the primary then fails, those changes stay unavailable until the primary
  returns. If the primary's disk were also destroyed, they would be lost.
- **Replication slows changes:** the lock is held while copying to the backup, so one slow
  or stalled backup delays changes, by up to the 5-second timeout, until it is marked as behind.
- **Reads can be out of date during a failure:** the backup answers `LIST` while on standby,
  and may be behind if it was recently down.
- **Duplicates are judged by description, category and location:** nothing identifies the
  physical item, so "Dell laptop" and "Dell Latitude" at one site count as different items.
  Status and category are also free text.
- **No authentication or encryption:** anyone who can reach a server can read and change
  assets, including by sending the messages meant for the other server. Use it only on a
  trusted network.
- **Limited updates:** assets cannot be searched, and only the status can be changed.
- **Deleted IDs are kept forever:** the list of them only grows.
- **Limited scale:** every change rewrites the whole file and happens one at a time, every
  asset is held in memory, and each `LIST` returns everything. Once all the data is over the
  10 MiB message limit, `LIST` replies `TOO_LARGE` and the primary cannot reconcile with a
  returning backup, although changes still work. There is no paging to get past this yet.
- **Unlimited threads:** a flood of connections could exhaust a server. The idle timeout
  applies to each read, so a client sending one byte at a time could also hold a thread for
  a long time.

## Opportunities for further development

A third server, so that a majority can decide which server leads and a network split cannot
let two lead at once. Also editing other fields, a serial number to identify items, search
and paging, a database in place of the JSON file, and login and encryption.

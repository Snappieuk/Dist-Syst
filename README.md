# Asset Management System

A distributed system for registering, listing, updating and deleting asset records.
The servers and clients are separate programs that run as their own processes and talk
to each other by sending JSON messages over TCP sockets. They can run on one machine or
on several.

```
                                      browser
                                         | HTTP
  send.py   receive.py   manage.py   gui.py   web.py   clients
      \          |           |        /        /
       \         |           |       /        /    try the primary first,
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

Each asset has an ID, description, category, location, status, registration date and
tags. The server creates the ID, sets the date and generates the tags. The status says
whether the item is being used at the moment, for example "In Use" or "Not In Use".

## Files

| File | Purpose |
|---|---|
| `server.py` | The primary server. Accepts changes, puts them in order and copies each one to the backup. Stores its data in `assets.json`. |
| `backup.py` | The backup server. Keeps its own copy of the data in `assets_backup.json`, sends heartbeats to the primary and takes over if the primary fails. |
| `node.py` | Code used by both servers: the data, the rules for changing it and the network loop. |
| `common.py` | Code used by the servers and clients: the message format, server addresses, error codes, tag generation and client failover. |
| `send.py` | Registers a new asset. |
| `receive.py` | Lists the assets with their IDs, statuses and tags. |
| `manage.py` | Marks an asset as "Not In Use" or "In Use", or deletes it. |
| `gui.py` | A Tkinter window that does all of the above. Select a row to change or delete that asset. The status line shows which server answered. |
| `web.py` | A web interface for browsing, adding and deleting assets, and creating accounts, behind a login. It serves the page in `static/` and talks to the servers like any other client. |
| `static/` | The web page: `index.html`, `app.js` and `style.css`. |
| `start.py` | Starts the primary, backup and web interface with one command, running setup first if needed. |
| `setup.py` | First-time setup, which creates the first web account, and a full reset with `--reset`. |
| `test_system.py`, `test_web.py`, `test_start.py`, `test_setup.py` | The automated tests. |

## Running it

You need Python 3.8 or later. There are no third-party packages to install.

The quickest way to start everything is:

```
python start.py
```

The first time, this asks you to create your account for the web page. It then starts the
primary, the backup and the web interface, opens the web page, and stops them all when you
press Ctrl+C. To create an account from the command line, or replace a lost password, run
`python web.py --set-password`.

If you had a web password from before accounts were added, it keeps working as an account
called `admin`.

To put everything back to a fresh install, stop the system and run:

```
python setup.py --reset
```

**This permanently deletes every asset, every web account and the shared secret on this
machine.** It shows what will be lost and asks you to type `RESET` first. It doesn't touch
the code or the git history. If you use several machines, reset each of them, because a
server that still has the data would copy it straight back. The next `python start.py` runs
setup again.

To stop one server on its own, which you need for the demo below, or to use the GUI, run
each program in its own terminal instead:

```
python server.py
python backup.py
python gui.py
```

You can use `python send.py`, `python receive.py` and `python manage.py` instead of the
GUI. These wait for you to press Enter before closing, so the window stays open if you
start them by double-clicking.

To use the web page, run this as well and open http://127.0.0.1:8080:

```
python web.py
```

After logging in, you can search, filter by status, sort by clicking a column heading, add
and delete assets, and create accounts for other people. The page refreshes every 5 seconds.
Its summary row shows the totals and which server answered. Only this machine can open it
unless you change `WEB_ADDRESS` in `web.py`.

The system runs without the backup, but it won't survive the primary failing.

To run it across several machines, put each server's IP address in `SERVERS` in
`common.py` and use the same list on every machine. Copy `secret.key` to every machine too,
after it has been created by the first run. Each server's firewall must allow its port. So
far this has only been run as separate processes on one machine.

## Demo

Start both servers and the GUI, and keep the server logs visible.

1. Register an asset. The primary logs `Change N copied to the backup` and the backup
   logs `Applied change N from the primary`.
2. Stop `server.py` with Ctrl+C and register another asset. The backup logs `TAKING OVER`
   and the GUI's status line says `served by the backup server`. The new ID starts with
   `AST-B`.
3. Start `server.py` again. It merges the backup's changes and the backup logs `Returning
   to standby`. Nothing is lost and the primary is back in charge.
4. Open two GUI windows showing the same asset with the status "In Use". Click "Mark Not
   In Use" in one, then "Delete Asset" in the other without refreshing. The delete is
   refused because that window hasn't seen the change.
5. Run `web.py`, log in and repeat steps 2 and 3. Without reloading, the summary row
   changes to `Backup` and back, and you can still add and delete assets during the outage.
   If you stop both servers, it keeps the last list on screen with a warning.

## Authentication

There are two layers, because a login on the web page alone could be bypassed by connecting
directly to a server's port.

Between programs, every request to a server carries a shared secret from `secret.key`. The
first program to run creates this file. Servers refuse any request without the right value
with `UNAUTHORIZED`, including the messages between the servers themselves.

On the web page, everything except the login page itself needs an account. You create the
first one during setup. If `web.py` is started with nobody at the keyboard to do that, it
creates an `admin` account with a random password and prints it once.

There is no public sign-up, because that would let anyone who can reach the page give
themselves an account. Instead, anyone logged in can create more accounts. Creating an
account can't change an existing one, so only `python web.py --set-password`, run on the
machine itself, can replace a password.

Usernames ignore capitals and can only contain letters, numbers, dots, dashes and
underscores. Passwords must be at least 8 characters. Only a salted PBKDF2 hash is stored,
in `web_users.json`. Logging in gives the browser a random session token that lasts 8 hours,
in a cookie that scripts can't read and other websites can't send. A wrong username gets the
same reply as a wrong password, so neither reveals which accounts exist. After 5 failed
logins in a row, on any usernames, logins are refused for a minute.

`secret.key` and `web_users.json` are ignored by git and should never be shared publicly.

## Tests

```
python -m unittest -v test_system test_web test_start test_setup
```

The tests start real servers on spare ports and use temporary files for data and accounts,
so yours aren't touched. They do use `secret.key`, and create it if it doesn't exist yet.
`test_web.py` makes real HTTP requests but doesn't run the page's script in a browser, so that
part was checked separately in Microsoft Edge. `DistributedTest` runs `server.py` and
`backup.py` as separate programs and kills them partway through. Some tests make a save fail
on purpose, so you will see logged errors and tracebacks in the output.

## Distributed systems concepts used

| Concept | How it's used | Where |
|---|---|---|
| Message passing | Servers and clients share no memory and only communicate over sockets | `common.py`: `send_message`, `receive_message` |
| External data representation | Length-prefixed UTF-8 JSON, so programs don't depend on a language or platform | `common.py`: `send_message` |
| Replication | The backup holds a full copy of the data in its own file | `backup.py`: `apply_change` |
| Synchronous replication | A client only hears that a change worked after the backup has saved it | `server.py`: `replicate_change` |
| Single leader | Only the primary accepts changes, so there is one order of events | `node.py`: `accepts_writes` |
| Total ordering | Every change has a sequence number, so a gap can be detected | `backup.py`: `apply_change` |
| Failure detection | The backup pings the primary every second and treats 3 missed heartbeats as a failure | `backup.py`: `monitor_primary` |
| Failover | The backup promotes itself and clients move to it automatically | `backup.py`: `monitor_primary`, `common.py`: `send_request` |
| Recovery and reconciliation | A returning primary merges the backup's changes and takes back control | `server.py`: `catch_up_backup`, `merge_state` |
| Conflict resolution | A delete wins, then the higher version, then the primary's copy | `server.py`: `merge_state` |
| Idempotent requests | A retry has the same `request_id`, so it can't create a second asset | `node.py`: `register_asset` |
| Unique IDs without coordination | Each server issues IDs from its own range and checks each one first | `node.py`: `generate_asset_id` |
| Concurrency control | A lock makes each change one indivisible step | `node.py`: `assets_lock` |
| Optimistic concurrency control | A change names the version it was based on and is refused if that is out of date | `node.py`: `version_conflict` |
| Tombstones | Deleted IDs are kept so another server's old copy can't bring an asset back | `node.py`: `deleted_ids` |
| Independent failure | One client or server failing doesn't bring down the others | `node.py`: `handle_client` |
| Authentication | Programs prove themselves with a shared secret, and people log in to the web page with a username and password | `node.py`: `is_authorized`, `web.py`: `try_login` |
| Multi-tier architecture | The browser talks to `web.py`, which talks to the servers, so the servers don't need to know about HTTP | `web.py` |
| Failure transparency | A browser keeps working through a failover without knowing which server is in charge | `web.py`: `assets_reply` |
| CAP trade-off | In a network split both servers stay available, and the data is made consistent afterwards | See known limitations |

## Messages

Each message is a 4-byte length followed by JSON. A connection carries one request and
one reply. Every request also has an `auth` field holding the shared secret, and every reply
has a `served_by` field naming the server that answered.

Requests from clients:

| Request | Successful reply |
|---|---|
| `{"action": "REGISTER", "request_id": "...", "asset": {...}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "LIST"}` | `{"status": "OK", "assets": [...]}` |
| `{"action": "SET_STATUS", "asset_id": "...", "status": "Not In Use", "expected_version": 1}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "DELETE", "asset_id": "...", "expected_version": 1}` | `{"status": "OK", "deleted": "..."}` |

`request_id` is a random value the client picks for each new registration. Sending the
same one again returns the same asset. `expected_version` is the version of the asset
the client last saw. Both are optional, but you lose their protection without them.

Requests between the servers:

| Request | Purpose |
|---|---|
| `{"action": "PING"}` | Heartbeat, sent in both directions |
| `{"action": "REPLICATE", "seq": 5, "asset": {...}}` | Copies one change to the backup |
| `{"action": "HANDOVER"}` | Tells the backup to go on standby and send all its data |
| `{"action": "INSTALL_STATE", "assets": [...], "deleted": {...}, "seq": 5}` | Replaces the backup's data with the merged copy |

Requests from the browser to `web.py`, over HTTP:

| Request | Purpose | Needs a login? |
|---|---|---|
| `GET /`, `/app.js`, `/style.css` | The page itself, which holds no data | No |
| `POST /api/login` with `{"username": "...", "password": "..."}` | Logs in, and sets the session cookie | No |
| `POST /api/logout` | Logs out | No |
| `GET /api/assets` | Lists the assets, who is logged in and which server answered | Yes |
| `POST /api/assets` with `{"request_id": "...", "asset": {...}}` | Adds an asset | Yes |
| `DELETE /api/assets/<id>?version=<n>` | Deletes an asset, if it is still at that version | Yes |
| `POST /api/users` with `{"username": "...", "password": "..."}` | Creates an account | Yes |

Errors from the servers look like `{"status": "ERROR", "code": "NOT_FOUND", "detail": "..."}`.

| Code | Meaning |
|---|---|
| `BAD_REQUEST` | The message is malformed or the asset is invalid, including any field over 1000 characters |
| `DUPLICATE_ASSET` | The item is already registered. `existing_id` gives its ID |
| `CONFLICT` | Someone else changed the asset after the client read it |
| `NOT_FOUND` | No server that replied has an asset with that ID |
| `NOT_PRIMARY` | This server is on standby. Clients wait and retry while the backup takes over |
| `UNAUTHORIZED` | The request didn't include the shared secret from `secret.key` |
| `TOO_LARGE` | The request or reply is over the 10 MiB limit |
| `INTERNAL` | The server failed, for example because the disk is full |
| `NOT_STANDBY`, `OUT_OF_SYNC` | Only used between servers. The backup can't take this copy, so the primary sends all its data instead |

## Duplicates and simultaneous changes

The server generates each asset ID and checks it against every stored and deleted ID
before issuing it. IDs count up, and the lock stops anything taking an ID between the
check and the save. The primary issues `AST-P000001` and so on and the backup issues
`AST-B000001`, so the two can't produce the same ID even when they can't reach each
other. A deleted ID is never reused.

Two assets with the same description, category and location count as the same item,
ignoring capitals, spacing and punctuation, and the second one is rejected with
`DUPLICATE_ASSET`. To register two identical items, make the descriptions different, for
example by adding a serial number. A deleted item can be registered again and gets a new
ID.

If a reply gets lost, the client retries with the same `request_id` and gets the existing
asset back instead of an error.

The lock makes "check, change, save, copy to the backup" one step, so simultaneous
requests take effect one after another. If two people act on the same version of an asset,
the first one wins and the second gets `CONFLICT` and sees the latest data.

Normally only one server accepts changes, so there is one order of events. The known
limitations cover what happens if both do.

## Changes from the starter code

- Data is saved after every change and loaded at startup.
- Each client has its own thread, so a slow client doesn't hold up the others. A lock
  protects the shared data.
- The server checks every field and limits each one to 1000 characters. It sets the
  registration date itself because client clocks can differ, and it generates tags in
  any language.
- A client that disconnected abruptly or sent invalid data used to crash the server. Now
  that only affects that client.
- Idle clients are disconnected, clients don't wait forever and message size is capped.
- Errors have clear codes, and clients show a message instead of a traceback when no
  server can be reached.
- A separate backup server takes over if the primary fails.
- Programs need a shared secret, and the web page needs a login, with accounts that can be
  created from the page.
- A web page, a single start command, first-time setup and a full reset were added.

### ACID properties

While both servers are running, a change is on both disks before the client gets its reply.

| Property | How it's provided |
|---|---|
| Atomicity | The save writes a temporary file and swaps it into place in one step. If the save fails, the change is undone in memory too. |
| Consistency | Only valid assets with unique IDs and identities are stored. |
| Isolation | A lock makes checking, changing and saving one indivisible step. |
| Durability | The file is forced to disk before the client gets its reply, and on Linux and macOS so is the folder holding it. |

## Known limitations

The biggest one is a network split. Two servers can't tell a failed partner from an
unreachable one, and settling that properly needs a majority, so at least three servers.
If the two lose contact while both are running, the backup takes over while the primary
carries on, which keeps the system available. When they reconnect, the primary merges the
data, and some changes are lost even though their clients were told they worked:

- If both servers registered the same item, the primary's record is kept and the backup's
  ID is retired, so anyone who noted that ID will find it gone.
- If both changed an asset's status, the copy with more changes wins, or the primary's if
  they made the same number.
- A delete on either server beats any status change.

Other limitations:

- The first change after the primary fails took about 8 seconds when timed on one Windows
  machine, mostly waiting for the 3 missed heartbeats. Clients also try the primary first
  on every request, which took about 2 more seconds each time. If both servers are down,
  clients show an error.
- Only one backup is supported, and failed servers have to be restarted by hand.
- If the backup was down when the primary took changes and the primary then fails, those
  changes are unavailable until the primary returns. If the primary's disk were also
  destroyed, they would be lost.
- The lock is held while copying to the backup, so a stalled backup can delay changes by
  up to the 5-second timeout before it is marked as behind.
- The backup answers `LIST` while on standby, and may be out of date if it was recently down.
- Duplicates are judged only by description, category and location, because nothing
  identifies the physical item. "Dell laptop" and "Dell Latitude" at one site count as
  different items. Status and category are free text.
- Nothing is encrypted. The shared secret, web passwords and all data cross the network
  as plain text, so someone watching a network could read them. Only run this across a
  trusted network, or add TLS first.
- Every web account can do everything, including creating more accounts, because there are
  no roles. Accounts can't be deleted from the page, and nothing records who changed an asset.
- Accounts belong to `web.py` and are stored beside it. They aren't copied to the backup, so
  they don't protect against losing that machine. All programs share one secret.
- A password can't be changed from the web page, only with `python web.py --set-password`,
  and `web.py` must then be restarted.
- The web page can add and delete assets, but can't change an asset's status. Use the GUI
  or `manage.py` for that.
- Don't change `WEB_ADDRESS` to expose the web page on a network you don't trust, because
  passwords would travel unencrypted.
- The web page fetches the whole list on every refresh and searches it in the browser, so
  this will get slow with a very large number of assets.
- The servers can't search assets, and only the status can be changed after registration.
- The list of deleted IDs only ever grows.
- Every change rewrites the whole file, changes happen one at a time, every asset is held
  in memory and each `LIST` returns everything. Once the data is over the 10 MiB message
  limit, `LIST` replies `TOO_LARGE` and the primary can't reconcile with a returning
  backup, although changes still work. There is no paging yet.
- There is no limit on threads, so a flood of connections could exhaust a server. The idle
  timeout applies to each read, so a client sending one byte at a time could also hold a
  thread for a long time.

## Further development

A third server would let a majority decide which server leads, so a network split couldn't
leave two leaders. Other ideas are editing assets from the web page, editing more fields, a
serial number to identify items, search and paging on the servers, a database instead of the
JSON file, encryption with TLS, and account roles, including deleting accounts and recording
who changed what.

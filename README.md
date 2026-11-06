# Asset Management System

A distributed system for registering, listing, updating and deleting asset records.
The servers and clients are separate programs that run as their own processes and talk
to each other by sending JSON messages over TCP sockets. By default everything runs on one
machine. Setting one environment variable spreads it across several, with the servers
found by name rather than by fixed addresses in the code.

```
                                       browser
                                          | HTTP
          cli.py         gui.py         web.py            clients, on any machine
              \            |            /
               \           |           /           find the servers by name through
                v          v          v            ASSET_SERVERS, and try the
    +-----------------+              +-----------------+   primary first, then the backup
    |    server.py    |  heartbeats  |    server.py    |
    |    (primary)    |<-------------|    (backup)     |
    |                 |  each change |                 |
    |  accepts all    |------------->|  on standby:    |
    |  changes, and   |  reconcile   |  applies copies,|
    |  orders them    |<------------>|  refuses clients|
    +-----------------+              +-----------------+
       assets.json                     assets_backup.json
       e.g. assets-1                   e.g. assets-2
```

This shows the default of two servers. More can be added to the end of the list, and each
one is a further backup. See [More than two servers](#more-than-two-servers).

Each asset has an ID, description, category, location, status, registration date and
tags. The server creates the ID, sets the date and generates the tags. It also records
`created_at` and `updated_at`, and `deleted_at` for each deleted ID, in UTC in ISO 8601 form,
such as `2026-11-05T21:14:03Z`. These come from the server's clock, and any a client sends
are ignored. UTC means times from servers in different time zones can be compared. Assets
and deletions from before these were added have none. The status says
whether the item is being used at the moment, and can only be "In Use" or "Not In Use".
Every client offers just these two: a read-only list in the GUI and on the web page, and a
numbered choice in `cli.py add`. The server also rejects anything else with `BAD_REQUEST`,
because any program could send other text. Assets saved with other statuses before this
rule still load, and can be changed to one of the two.

## Files

| File | Purpose |
|---|---|
| `server.py` | An asset server. `python server.py` runs the primary, which accepts changes, puts them in order and copies each one to the backup, and stores its data in `data/assets.json`. `python server.py --backup 1` runs the backup, which keeps its own copy in `data/assets_backup.json`, sends heartbeats to the primary and takes over if the primary fails. `--backup 2` and so on run any further servers in the list. |
| `node.py` | Code used by every server: the data, the rules for changing it and the network loop. |
| `common.py` | Code used by the servers and clients: the message format, where the servers are, error codes, tag generation and client failover. |
| `cli.py` | The command-line client. `python cli.py add` registers a new asset, `list` lists the assets with their IDs, statuses and tags, and `change` marks one as "Not In Use" or "In Use", edits its details, or deletes it. |
| `clients/AssetClient.cs` | A small client written in C#, to show the servers can be used from another language. It logs in, then lists, adds, checks out and checks in assets by sending the same length-prefixed JSON over TLS as the other clients. On Windows, `clients\asset.cmd` builds it with the compiler that comes with Windows (no SDK needed) and then runs it, so there is no separate build step. Run, for example, `clients\asset.cmd list --user admin`. Pass `--server host:port` for another machine and `--fingerprint` to check the server's certificate (it prints the fingerprint it sees). The top of the file describes the message format, which is all another language needs to match. |
| `clients/app/` | The desktop app in C# (Windows Forms), built with `clients\build_app.cmd` into `clients\AssetApp.exe`. See "The GUI on other computers". |
| `gui.py` | A Tkinter window that does all of the above. It is one standalone file with a login screen, and finds the servers from one address. Select a row to change its status, edit its details or delete it. A search box and status filter above the table ask the server for only the matching assets. The status line shows which server answered. A "Flagged for review" area lists assets added while the primary was offline, and History (or a double-click) shows what has happened to an asset. |
| `web.py` | A web interface for browsing, adding and deleting assets, reviewing flagged ones, checking assets out and in, seeing their history and a loans calendar, and creating accounts, behind a login. It serves the page in `static/` and talks to the servers like any other client. |
| `static/` | The web page: `index.html`, `app.js` and `style.css`. |
| `start.py` | Starts the primary, backup and web interface on this machine with one command, running setup first if needed, and restarts any that stops. |
| `tools/` | Two helpers for trying the system out. `demo.py` shows a failover step by step on a running system. `fill.py` fills a running system with made-up assets, 500 unless `--count` says otherwise, so there is a full table to try things on. |
| `config.example.json` | An example of the optional settings file, `config.json`. |
| `setup.py` | First-time setup, which creates the shared secret, the TLS certificate and the first web account, and a full reset with `--reset`. |
| `requirements.txt` | The one package needed, `cryptography`, to create the certificate. |
| `data/` | Created as the programs run, and ignored by git. Everything they create: the data files, the web accounts (`web_users.json`), the shared secret (`secret.key`), the certificate (`tls.pem`) and each program's log in `data/logs/`. |
| `tests/` | The automated tests, one file for each part of the system. |

## Running it on one machine

You need Python 3.8 or later, and one package, `cryptography`, which setup uses to create
the certificate that encrypts every connection:

```
python -m pip install -r requirements.txt
```

A machine that is given a copy of `tls.pem`, rather than creating its own, doesn't need it.

The quickest way to start everything is:

```
python start.py
```

The first time, this asks you to create your account for the web page. It then starts every
server in the list (the primary and the backup, by default) and the web interface, opens the web page, and stops them all when you
press Ctrl+C. To create an account from the command line, or replace a lost password, run
`python web.py --set-password`.

If one of them stops, the others carry on and it is restarted 20 seconds later, the way a
cluster manager such as Kubernetes restarts a failed server. One that fails as it starts,
usually because the system is already running and its port is in use, stops everything
instead.

If you had a web password from before accounts were added, it keeps working as an account
called `admin`.

To see each program's log on its own, or to use the GUI, run each program in its own
terminal instead:

```
python server.py
python server.py --backup 1
python gui.py
```

`gui.py` is a single file with no other requirements. To use it on another computer, copy
just that file and run it with Python 3.8 or later. Enter the server's address (or press
Find servers to look for one on the local network) and log in with a web account. See
[the GUI](#the-gui-on-other-computers).

You can use `python cli.py add`, `python cli.py list` and `python cli.py change` instead of
the GUI. Without a command, it asks which one you want. It waits for you to press Enter
before closing, so the window stays open if you start it by double-clicking.

To use the web page, run this as well and open http://127.0.0.1:8080 (on another device,
use this computer's address instead, such as `http://192.168.1.20:8080`):

```
python web.py
```

After logging in, you can search, filter by status, sort by clicking a column heading, and
add, edit and delete assets. Adding and editing open in a window over the page, and the
result of each change appears briefly in the bottom corner. The page refreshes every 5
seconds. The page has five tabs: Assets (the table, with totals showing which server
answered, in green for the primary and amber for the backup), Loans, People, Calendar and Review.
The Assets and Loans tables scroll inside their own area, so the page around them stays put.

The button at the top right shows who is logged in and their role. It opens a small menu
with Log out and, for admins, Manage accounts. That opens a window listing every account,
where an admin can change roles, delete accounts and create new ones. The page listens on
every network by default (`0.0.0.0`), so other devices can open it. It is plain HTTP, so on
a network you don't trust, set `web_address` to `127.0.0.1:8080` to allow only this machine.
The computer's firewall may also need to allow port 8080, and, for the desktop apps on
other devices, the servers' ports 65432 and 65433 (the servers listen on every network by
default). A blocked port normally makes a connection time out; "actively refused" usually
means nothing is listening there.

## Settings

Everything works without any settings. To change them, copy `config.example.json` to
`config.json` beside the code and edit it. Leave out any setting you don't want to change.

| Setting | Default | Meaning |
|---|---|---|
| `servers` | `["127.0.0.1:65432", "127.0.0.1:65433"]` | Where the servers are: the primary first, then each backup in the order they take over |
| `web_address` | `"0.0.0.0:8080"` | Where `web.py` listens. `0.0.0.0` is every network; `127.0.0.1` is only this machine |
| `behind_https_proxy` | `false` | Set to `true` when `web.py` sits behind an HTTPS reverse proxy such as Nginx Proxy Manager: the session cookie is then marked `Secure` and the real visitor address is logged |
| `trusted_proxy` | `"127.0.0.1"` | The one address the proxy connects from. The `X-Forwarded-For` header is believed only on requests from here, and only when `behind_https_proxy` is `true` |
| `timeout_seconds` | `5` | How long a program waits on each network step before giving up |
| `heartbeat_timeout_seconds` | `1` | How long the servers wait for each other's heartbeats |
| `failover_wait_seconds` | `15` | How long a client keeps trying while the backup takes over |

A misspelt setting or a wrong value stops the program with a message naming `config.json`,
rather than being ignored. Environment variables override the file: `ASSET_SERVERS` for the
servers and `ASSET_WEB_ADDRESS` for the web address, each in the same `host:port` form;
`ASSET_BEHIND_HTTPS_PROXY` (`true` or `false`) and `ASSET_TRUSTED_PROXY` for the two proxy settings.
`ASSET_CONFIG` can name a different settings file. `config.json` is ignored by git,
because each machine has its own, and a reset leaves it alone.

The system runs without the backup, but it won't survive the primary failing. When the
primary starts, it keeps trying to reach the backup for up to 3 seconds before serving
anyone, so the two can be started at the same moment and no change misses the backup. If
the backup still isn't there, the primary starts alone and brings the backup up to date
when it appears.

## Running it across several machines

Nothing in the code needs changing. Every machine, including those running only a client
or `web.py`, needs the same server list, either as `servers` in its `config.json` or in the
`ASSET_SERVERS` environment variable:

| Variable | Set on | Meaning |
|---|---|---|
| `ASSET_SERVERS` | Every machine, unless `config.json` has `servers` | Where the servers are: the primary first, then each backup, as `host:port,host:port`. Without it or the setting, both are on this machine, on ports 65432 and 65433. |
| `ASSET_LISTEN_HOST` | Each server machine, only to restrict it | The address the server listens on. Without it, every server listens on every network connection the machine has (`0.0.0.0`), so other devices can connect. Set it to `127.0.0.1` to accept only this machine. |

For example:

```
export ASSET_SERVERS=assets-1:65432,assets-2:65433      # Linux and macOS
$env:ASSET_SERVERS = "assets-1:65432,assets-2:65433"   # Windows PowerShell
```

Use hostnames rather than IP addresses where you can, so a machine can change its address
without anyone updating the list. Any name the machines can look up works, from an entry
in each machine's hosts file to DNS, a container's name or a Kubernetes service. Names are
looked up as IPv4 addresses, because that is what the servers listen on.

The servers listen on every network connection by default, on the port in their own
`ASSET_SERVERS` entry, whatever name that entry uses. The name is only how other programs
reach the server, and it may not belong to the server's own machine, for example when it is
behind a router or in a container. `ASSET_LISTEN_HOST` restricts that if you want it to.

Then:

1. Run `python setup.py` on the machine that will run `web.py`. Copy the `secret.key` and
   `tls.pem` it creates to the `data/` folder beside the code on every other machine. Every machine must have the
   same two files, or its connections are refused.
2. Run `python server.py` on the first machine and `python server.py --backup 1` on the
   second. With more servers, run `--backup 2` on the third, and so on.
3. Run clients or `web.py` anywhere that can reach every server.

Each server's firewall must allow its port. Don't use `start.py` here, because it starts
every server in the list on the same machine. Every connection to a server is encrypted, including the
servers' own, so the network can't read them. The web page isn't, so keep `web.py` on the
machine its users sit at, as it is by default.

So far this has been tested as separate processes on one machine, with the servers found
by name (`localhost`) instead of by `127.0.0.1`. It hasn't been run on two physical machines
yet, and listening on `0.0.0.0` hasn't been tried.

## More than two servers

To add a server, add its address to the end of the `servers` list on every machine, then
start it with the next number:

```json
{"servers": ["assets-1:65432", "assets-2:65433", "assets-3:65434"]}
```

```
python server.py               # the primary, assets-1
python server.py --backup 1    # assets-2
python server.py --backup 2    # assets-3
```

On one machine, `start.py` starts one server for each address in the list by itself.

The order of the list is the order of succession, and one rule decides everything:

- **A server accepts changes only when no server before it in the list answers.** Normally
  that is the primary. If it fails, backup 1 takes over. If backup 1 has failed as well,
  backup 2 takes over, and so on. A server only takes over once every server before it has
  missed 3 heartbeats in a row.
- **Whichever server is accepting changes copies each one to every server after it** before
  the client is told it worked. A server that is down is skipped, and brought up to date
  when it returns.
- **When a server earlier in the list comes back, it takes control again.** It collects the
  data of each server after it, merges it with its own using the usual rules, and sends the
  result back, which puts them on standby.

Each server has its own name, data file, log and range of asset IDs: the primary uses
`assets.json` and `AST-P...` IDs, backup 1 uses `assets_backup.json` and `AST-B...`, backup 2
uses `assets_backup2.json` and `AST-C...`, and so on. Clients need no changes, because they
already try each server in the list in order.

This is deliberately simple. Extra servers hold extra copies and can take over in turn, but
they don't vote, so they don't protect against a network split. See
[Known limitations](#known-limitations).

## Resetting

To put everything back to a fresh install, run:

```
python setup.py --reset
```

**This permanently deletes every asset, every web account, the shared secret, the
certificate and the logs on this machine.** It shows what will be lost, and what is running,
and asks you to type `RESET` first. After that it stops everything of the system running on
this machine (`start.py` first, so it can't restart anything), then deletes. If something
can't be stopped, nothing is deleted. It doesn't touch
the code or the git history. If you use several machines, reset each of them, because a
server that still has the data would copy it straight back, and a server on another machine
has to be stopped there. The next `python start.py` runs setup again.

## Filling the table with test data

To see how the web page and GUI cope with a full table, start the system, then run this in
a second terminal:

```
python tools/fill.py               # adds 500 made-up assets
python tools/fill.py --count 100   # or any other number (-n 100 for short)
```

The assets are registered through the servers like any other, so the backup gets a copy
and the web page shows them on its next refresh. Each description ends with a random
serial number, such as `Dell Latitude 5440 laptop, SN 7KQ2M9XP`, because the server refuses
a second asset with the same description, category and location. Every asset is marked
as sample data, so it has a `sample data` tag (search for it to see only these). About two
in five are lent to a made-up person, with loans from 30 days ago to 20 days ahead: most
that have ended were returned, some late, and some are overdue, so the loans calendar has
something to show. Adding 500 takes about 10 seconds. To clear them away again, run `python setup.py --reset`,
which deletes every asset, not just these.

## Demo

The quickest way to show a failover is to start the system, then run this in a second
terminal:

```
python tools/demo.py
```

Keep the `start.py` window visible beside it, because the server logs show the same events
from the servers' side. The demo pauses before each step, so you can explain it:

1. It shows both servers' states, then adds an asset through the primary and checks the
   backup already has a copy. It lends that asset to a demo borrower for a week, so it
   shows on the loans calendar. The demo's assets are sample data, like `fill.py`'s.
2. It sends the primary a `STOP` request, which ends it abruptly, as if it had crashed.
3. It adds another asset. The client waits while the backup misses 3 heartbeats and takes
   over, which took about 8 seconds when tried, and the backup stores it with an `AST-B` ID.
   It is flagged for review, because the primary was offline.
4. It waits for the primary to come back. `start.py` restarts it after 20 seconds. If you
   started the servers in separate terminals, the demo asks you to run `server.py` again.
   The primary merges the backup's changes, and the demo checks both assets are there.
5. It shows the asset is still flagged for review once the primary is back, and approves
   it, as an admin would on the web page or in the GUI.
6. It deletes the two assets it added.

It also works across machines, as long as `ASSET_SERVERS` is set where it runs.

To show it by hand instead, start both servers and the GUI in separate terminals, and keep
the server logs visible.

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
6. To show the servers being found by name, set
   `ASSET_SERVERS=localhost:65432,localhost:65433` in every terminal and repeat steps 1–3.
   Nothing changes for the user, because no program needs to know the servers' addresses.

## Reviewing, loans, history and the calendar

**Flagged for review.** Only the primary normally accepts changes, so an asset registered
while it was offline (by a backup that had taken over) is flagged for someone to check. The
web page shows these in a "Flagged for review" panel above the totals, and the GUI in an
area above the table, each saying the assets were added while the primary server was
offline. Each has a tick box, with one to tick them all, and buttons approve or deny the
ticked ones or all of them. Approving clears the flag. Denying deletes the asset, so it
asks first. On the web page only admins can approve or deny, as only they can delete.
Flagged assets are also marked "Needs review" in the main table.

**Check-out form.** On the web page, each asset has a Check out button, which asks for the
borrower's name, a start date (today) and a due date (a week later). An asset on loan has a
Check in button instead, and its status says who has it and until when, in red once it is
overdue. Checking out marks it In Use and checking in marks it Not In Use. An asset can't
be lent twice at once, and the due date can't be before the start. Searching also finds
assets by who has them. In the GUI, select an asset and click Check Out / In: it opens the
same form for an asset that is free, or asks before checking in one that is on loan. The
Loans button lists every loan of the assets the table shows, oldest start first, with
the return date, "On loan" or "Overdue".

**History.** Every registration, status change, edit, approval, check-out and check-in is
recorded with the asset: when, what, which web user asked and which server did it. The
history travels with the asset, so every server has it. Click an asset on the web page,
or double-click it (or use History) in the GUI, to see it.

**People.** The People tab lists everyone who has something on loan right now. It is worked
out from the loans themselves, so it fills by itself (including with demo data), the same
person lent two items is one row listing both, and they drop off once everything is back,
whoever checked it in. The Borrower box offers every name ever lent to as you type. Remove
on someone who still has items out first lists those items, then asks again before
checking them all back in. (A loan can't be deleted, only closed by checking the item in.)

**Loans and calendar.** The Loans tab lists every loan, newest first, with a filter for
on loan, overdue or returned, and a Check in button on loans still out. The Calendar tab shows a month at a time as a real calendar,
weeks starting on Monday. Each loan is a bar across its days: green while out, red when
overdue (running up to today) and grey once returned. A day with more loans than fit shows
"+N more", which you can click to list that day's loans. Both cover all assets, and clicking a loan or bar shows that asset's history.
Every loan an asset has had is kept, so past months show too. The Review tab is always
there, and says so when nothing is waiting.

**Totals.** The totals at the top of the web page count what the search and status filter
match, so searching "switch" shows how many switches there are, how many are in use and
how many are on loan.

## Encryption

Every connection to a server is encrypted with TLS: from the GUI, the command-line
clients, `web.py` and `demo.py`, and between the servers. Someone watching the network
sees neither the shared secret nor any asset data. A test relays a real registration
through a copy of everything sent, and finds none of it readable.

Setup creates a self-signed certificate and its private key in `tls.pem`, using the
`cryptography` package, if `start.py`, `setup.py` or the first program to run hasn't already.
It lasts about 10 years. Each server presents it, and `common.request_from()` trusts that
one certificate and nothing else, so a server with any other certificate is refused, even
one a public authority signed, and to a client it is the same as a server that is down.
Clients don't check the server's name, because trusting one exact certificate is a stronger
check, and the servers may be reached by any name. A program that doesn't use TLS gets
nothing from a server.

The web page itself is plain HTTP, not HTTPS. That is safe while `web.py` listens only on
127.0.0.1, its default, because then the browser and `web.py` are on the same machine and
nothing crosses a network. Changing its address to reach it from other machines would send
passwords unencrypted.

## Authentication

There are two layers, because a login on the web page alone could be bypassed by connecting
directly to a server's port.

Between programs, every request to a server carries a shared secret from `secret.key`. The
first program to run creates this file, and every machine needs the same copy. Servers
refuse any request without the right value with `UNAUTHORIZED`, including the messages
between the servers themselves.

On the web page, everything except the login page itself needs an account. You create the
first one during setup. If `web.py` is started with nobody at the keyboard to do that, it
creates an `admin` account with a random password and prints it once.

Each web account has a role:

| Role | Can |
|---|---|
| `admin` | View, add, edit and delete assets, and create accounts |
| `user` | View, add and edit assets |

The first account is an admin, and so is any account made with `python web.py
--set-password`. Accounts from before roles were added count as admins, so nobody lost what
they could already do. `web.py` checks the role on every request, and refuses a user's
delete or account creation with `403`. Hiding those buttons on the page is only tidiness,
not what stops a user.

There is no public sign-up, because that would let anyone who can reach the page give
themselves an account. Instead, an admin creates accounts in the Manage accounts window, as
users unless they choose admin. An admin can also change an account's role or delete it
there, which logs it out everywhere at once. Nobody can delete their own account, and the
last admin can't be removed or made a user, so the system can't be left with no admin. Creating an account can't change an existing one, so only `python web.py
--set-password`, run on the machine itself, can replace a password. An existing account
keeps its role when its password is replaced.

**The command-line clients (`cli.py`, `demo.py`) are trusted admin tools.** They have no
login, because they prove themselves with `secret.key` instead, and anything with that file
can do everything, including deleting. Only give `secret.key` to people who should have
admin rights. `gui.py` doesn't need that file: it logs in, as described next.

Usernames ignore capitals and can only contain letters, numbers, dots, dashes and
underscores. Passwords must be at least 8 characters. Only a salted PBKDF2 hash is stored,
in `web_users.json`. Logging in gives the browser a random session token that lasts 8 hours,
in a cookie that scripts can't read and other websites can't send. A wrong username gets the
same reply as a wrong password, so neither reveals which accounts exist. After 5 failed
logins in a row for one username, logins for that username are refused for a minute. The
count is per username, so someone guessing one account's password can't lock the others out.

`secret.key`, `tls.pem` and `web_users.json` are kept in `data/`, which git ignores. They
should never be shared publicly.

## The GUI on other computers

`gui.py` is one file that imports nothing from the project, so it can be copied alone to
any computer with Python 3.8 or later. It has no `secret.key`, `tls.pem` or settings
file, and saves nothing.

**Easiest: download it from the web page.** Log in to the web page, open the account
menu and choose **Download Python app (gui.py)**. The file you get is `gui.py` with two lines
filled in: the servers' addresses (the address you used for the web page replaces
`127.0.0.1`) and the fingerprint of the real certificate. Run it, type your username and
password, and that's all: the address is already there and nothing is asked about
certificates. It only ever talks to a server presenting that exact certificate, which
closes the gap described below. Neither value is secret, and the secret key and passwords
are never put in the file. If the servers' certificate is ever replaced (`data/tls.pem`),
old downloads stop working and need downloading again. Anyone who can change the web page
could hand out a different file, so serve the page only over a network you trust.

**Windows app (.exe), no Python needed.** `clients\app\` holds the same desktop app written
in C# (Windows Forms): login, Assets, Loans, People, Calendar and Review tabs, add, edit,
status, delete, history and check out / in, with the same search, failover and certificate
pinning as `gui.py`. It refreshes itself every few seconds. The Loans and People tabs have
check-in buttons, a click on a person lists what they have out, and a click on a calendar
day lists that day's loans. Items are shown by description rather than asset tag. If no
server answers, a red notice appears in the status bar until one does. The web page's account menu has **Download Windows app (.exe)**. The server
builds `AssetApp.exe` the first time with the C# compiler that comes with Windows (no
SDK needed; only a Windows server can do this, otherwise the link says so), caches it in
`data\`, and appends the server addresses and certificate fingerprint to the end of the
file, which the program reads when it starts. To build it yourself, run
`clients\build_app.cmd`; a copy built this way has no settings, so it asks for the server
address and trusts the first certificate it sees, like a plain `gui.py`. Windows may show
a "SmartScreen" warning for an unsigned program: choose More info, then Run anyway.

The Windows app also has **HTTPS mode**, like `gui.py`: type `https://assets.example.com`
as the server and it uses the web page's own API over ordinary HTTPS (port 443 only,
through a reverse proxy). The certificate is checked the normal way by Windows (TLS 1.2 is
switched on, since .NET Framework 4.x doesn't always do that), plain `http://` and redirects
are refused, and pinning and Find servers are not used. The code is in `clients\app\WebNet.cs`.

**Without the download**, a plain copy of `gui.py` works on its own:

1. Run `python gui.py`. On the login screen, type the server's IP address or host name
   (add `:port` if it isn't 65432), or press **Find servers**. Find servers broadcasts on
   the local network to the usual ports, 65432 and 65433, and lists the servers that
   answer, so a server on another port has to be typed in. **Check** tests an address
   without logging in. **From outside the network**, type the site's web address instead,
   starting with `https://` (for example `https://assets.example.com`). The GUI then uses
   the web page's own API over ordinary HTTPS (only port 443 is needed): the site's
   certificate is checked the normal way, plain `http://` is refused, and Find servers
   and pinning are not used. web.py does the failover and checks every permission.
2. In the local-network mode there is no certificate question: the first certificate the GUI sees is trusted
   automatically, and from then on only that exact certificate is used, so a different
   server at the same address is refused. This is remembered only until the window
   closes. The weak spot is that an attacker already intercepting the very first
   connection would not be noticed.
3. Log in with a web account. The server checks every request against it, so a user can
   view, add and edit assets, and only an admin can delete assets or approve flagged ones
   (other requests get `FORBIDDEN`).

Only one address is needed. `PING` needs no login and lists every server's address, so the
GUI learns where the backup is, and moves to it by itself if the primary fails. A server
lists its own address as `config.json` gives it, so if that is `127.0.0.1` the GUI uses
the address you typed instead.

How it works: `LOGIN` checks the username and password against `web_users.json` and
returns a token, `username:expiry:signature`, that lasts 8 hours. The signature is made
with `secret.key`, so only a server can make one and it can't be edited, and the backup
accepts the primary's tokens without a second login. Each request looks the account up
again, so deleting an account or changing its role takes effect at once. A token may only
be used for the client actions (`LIST`, `REGISTER`, `SET_STATUS`, `EDIT`,
`APPROVE`, `DELETE`, `CHECK_OUT`, `CHECK_IN`), never to copy data between servers or
`STOP` one. The audit log names the account, whatever `user` the GUI claims. Failed
logins share the web page's limit: 5 in a row refuses logins for a minute.

## Logs

Every program logs to the console and to its own file in `data/logs/`:

| File | Written by | Records |
|---|---|---|
| `primary.log` | `server.py` | Requests it couldn't complete, each change copied to the backup, the backup falling behind or catching up, and taking back control |
| `backup.log` | `server.py --backup 1` | Each change applied, missed heartbeats, taking over and returning to standby. Any further servers write `backup2.log` and so on. |
| `web.log` | `web.py` | Logins and failed logins with where they came from, logouts, and which account added, deleted or created what |
| `start.log` | `start.py` | When the system started and stopped, and each program that stopped and was restarted |

Each program has its own file because they are separate processes, possibly on separate
machines. Processes sharing one file can mix up each other's lines, and can't safely take
turns starting a new file when it fills up. Every line starts with the time, the level, the
program and the thread, for example:

```
2026-11-05 23:10:25,238 WARNING [backup] [heartbeat] The primary missed 3 heartbeats. TAKING OVER: now accepting changes.
```

so lines from different files can be put in order to follow one event, such as a failover,
across the system. A file that reaches 1 MB is renamed to `primary.log.1` and a new one is
started, and only the 5 newest old files are kept, so the logs can't fill the disk. A crash
is recorded with its full error before the program stops.

### Audit logs

Beside its normal log, each program keeps an audit log of who did what:
`primary-audit.log`, `backup-audit.log` and `web-audit.log`. Each line is one JSON record,
so a program can read them back, for example:

```
{"time": "2026-11-05T21:14:03Z", "action": "EDIT", "asset_id": "AST-P000004", "request_id": null, "user": "alex", "server": "primary", "seq": 12}
```

| Written by | Actions | Fields |
|---|---|---|
| A server, for each change it makes | `REGISTER`, `SET_STATUS`, `EDIT`, `DELETE` | `time`, `action`, `asset_id`, `request_id`, `user`, `server`, `seq` |
| `web.py` | `LOGIN`, `LOGIN_FAILED`, `LOGOUT`, `CREATE_ACCOUNT` | `time`, `action`, `user`, and `address`, `reason`, `account` or `role` where they apply |

A change is only recorded once it has been saved, and a request that changes nothing, such
as a retry or a refused duplicate, isn't recorded. The server that made the change records
it, which is the backup while it is in charge. The backup doesn't record the copies it
receives from the primary, so each change is in exactly one audit log. `user` is the web
user who made the change, which `web.py` sends with each change request. It is `null` for
the GUI and command-line clients, which have no login. `request_id` is the one the client
sent, so for a registration it ties the record to the client's retries. Times are UTC.

Passwords and the shared secret are never logged, including the password `web.py` creates
when nobody is at the keyboard, which is only printed. Anything a user typed, such as a
description, is logged in quotes with line breaks shown as `\n`, so it can't fake an extra
log line. The `data/` folder is ignored by git, and a full reset deletes the logs.

## Tests

Run every test from the project folder with:

```
python -m unittest
```

Or run one file, such as the web tests, with `python -m unittest -v tests.test_web`.

The tests start real servers on spare ports and use temporary files for data and accounts,
so yours aren't touched. They do use `secret.key` and `tls.pem`, and create them if they
don't exist yet. `test_tls.py` checks that a server with a different certificate is refused,
that a client that doesn't encrypt gets nothing, and that nothing readable crosses the
network.
`test_web.py` makes real HTTP requests but doesn't run the page's script in a browser, so that
part was checked separately in Microsoft Edge. `DistributedTest` runs the primary and
the backup as separate programs and kills them partway through. `ThreeServerTest` does the
same with three servers, failing the first two in turn and bringing both back. `test_config.py` starts
programs with `ASSET_SERVERS` and `ASSET_LISTEN_HOST` set, including a server known by a name
it can't listen on. `test_demo.py` runs the whole demo against real servers.
`test_logging.py` checks that the servers write separate files, that full files are replaced
and that crashes are recorded. Tests write logs to temporary folders, never to `data/logs/`. Some
tests make a save fail on purpose, so you will see logged errors and tracebacks in the output.

## Distributed systems concepts used

| Concept | How it's used | Where |
|---|---|---|
| Message passing | Servers and clients share no memory and only communicate over sockets | `common.py`: `send_message`, `receive_message` |
| External data representation | Length-prefixed UTF-8 JSON, so programs don't depend on a language or platform | `common.py`: `send_message` |
| Naming and location transparency | Programs find the servers by name from configuration, so a server can move machine without any code changing | `common.py`: `SERVERS`, `parse_servers`, `request_from` |
| Replication | The backup holds a full copy of the data in its own file | `server.py`: `apply_change` |
| Synchronous replication | A client only hears that a change worked after the backup has saved it | `server.py`: `replicate_change` |
| Single leader | Only the primary accepts changes, so there is one order of events | `node.py`: `accepts_writes` |
| Total ordering | Every change has a sequence number, so a gap can be detected | `server.py`: `apply_change` |
| Failure detection | Each backup pings the servers before it every second and treats 3 missed heartbeats as a failure | `server.py`: `monitor_earlier_servers` |
| Failover | The first backup whose earlier servers are all silent promotes itself, and clients move to it automatically | `server.py`: `monitor_earlier_servers`, `common.py`: `send_request` |
| Automatic restart | A server that stops is restarted while the others carry on, as a cluster manager would | `start.py`: `check_programs` |
| Recovery and reconciliation | A returning server merges the changes of the servers after it and takes back control | `server.py`: `catch_up`, `merge_state` |
| Conflict resolution | A delete wins, then the higher version, then the primary's copy | `server.py`: `merge_state` |
| Idempotent requests | A retry has the same `request_id`, so it can't create a second asset | `node.py`: `register_asset` |
| Unique IDs without coordination | Each server issues IDs from its own range and checks each one first | `node.py`: `generate_asset_id` |
| Concurrency control | A lock makes each change one indivisible step | `node.py`: `assets_lock` |
| Optimistic concurrency control | A change names the version it was based on and is refused if that is out of date | `node.py`: `version_conflict` |
| Tombstones | Deleted IDs are kept so another server's old copy can't bring an asset back | `node.py`: `deleted_ids` |
| Independent failure | One client or server failing doesn't bring down the others | `node.py`: `handle_client` |
| Authentication | Programs prove themselves with a shared secret, and people log in to the web page with a username and password | `node.py`: `is_authorized`, `web.py`: `try_login` |
| Authorisation | Each web account's role decides what it may do, checked on every request | `web.py`: `is_admin` |
| Secure channels | Every connection to a server is encrypted with TLS, and clients trust only the system's own certificate | `common.py`: `connect`, `tls_context` |
| Multi-tier architecture | The browser talks to `web.py`, which talks to the servers, so the servers don't need to know about HTTP | `web.py` |
| Failure transparency | A browser keeps working through a failover without knowing which server is in charge | `web.py`: `assets_reply` |
| CAP trade-off | In a network split both servers stay available, and the data is made consistent afterwards | See known limitations |

## Messages

Each message is a 4-byte length followed by JSON. A connection carries one request and
one reply. Every request also has an `auth` field holding the shared secret and a
`protocol_version` field, currently `1`, and every reply has a `served_by` field naming the
server that answered. `common.request_from()` adds both fields, so programs using it never
set them. A server refuses a request whose version is missing or one it doesn't support,
so a program written for a different message format gets a clear error instead of being
misunderstood.

Requests from clients:

| Request | Successful reply |
|---|---|
| `{"action": "REGISTER", "request_id": "...", "asset": {...}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "LIST"}` | `{"status": "OK", "assets": [...], "total": 12}` |
| `{"action": "LIST", "query": "dell", "status": "In Use", "offset": 0, "limit": 20}` | One page of the matching assets, and `total`, how many match altogether |
| `{"action": "SET_STATUS", "asset_id": "...", "status": "Not In Use", "expected_version": 1}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "EDIT", "asset_id": "...", "changes": {"location": "Site B"}, "expected_version": 1}` | `{"status": "OK", "stored": {...}}`, the updated record |
| `{"action": "DELETE", "asset_id": "...", "expected_version": 1}` | `{"status": "OK", "deleted": "..."}` |
| `{"action": "APPROVE", "asset_id": "...", "expected_version": 1}` | `{"status": "OK", "stored": {...}}`, with `flagged` cleared |
| `{"action": "CHECK_OUT", "asset_id": "...", "borrower": "Sam Lee", "start": "2026-11-06", "due": "2026-11-13"}` | `{"status": "OK", "stored": {...}}`, with the loan added to `loans` |
| `{"action": "CHECK_IN", "asset_id": "...", "returned": "2026-11-10"}` | `{"status": "OK", "stored": {...}}`. `returned` is optional and defaults to today |
| `{"action": "STOP"}` | `{"status": "OK"}`, then the server ends abruptly, as if it had crashed. Used by `demo.py` |

Each of `LIST`'s options is optional, and without any it returns every asset, as it always
has. `query` is found anywhere in the ID, description, category, location or status,
ignoring capitals. `status` must match exactly. `offset` skips that many matches and `limit`
returns at most that many, so a client can page through a long list without hitting the
10 MiB message limit. Both servers answer `LIST`, so the backup can search too.

Any change request may also carry `"user": "..."`, the web user it is made for, which is
only used in the audit log. `web.py` sends it. The other clients leave it out.

`EDIT` changes any of `description`, `category`, `location` and `status`, and leaves the
others alone. Each is checked as on registration, the edit is refused with
`DUPLICATE_ASSET` if it would make the asset the same item as another, and it gets a new
version and `updated_at`. Like every change, it is copied to the backup as the whole
updated record.

`request_id` is a random value the client picks for each new registration. Sending the
same one again returns the same asset. `expected_version` is the version of the asset
the client last saw. Both are optional, but you lose their protection without them.

Requests between the servers:

| Request | Purpose |
|---|---|
| `{"action": "PING"}` | Heartbeat, sent in both directions |
| `{"action": "REPLICATE", "seq": 5, "asset": {...}}` | Copies one new or changed asset to the backup |
| `{"action": "REPLICATE", "seq": 5, "deleted": "...", "request_id": "...", "deleted_at": "..."}` | Copies one deletion to the backup, with the primary's deletion time |
| `{"action": "HANDOVER"}` | Tells the backup to go on standby and send all its data, including `deleted_at` |
| `{"action": "INSTALL_STATE", "assets": [...], "deleted": {...}, "deleted_at": {...}, "seq": 5}` | Replaces the backup's data with the merged copy |

Requests from the browser to `web.py`, over HTTP:

| Request | Purpose | Needs a login? |
|---|---|---|
| `GET /`, `/app.js`, `/style.css` | The page itself, which holds no data | No |
| `POST /api/login` with `{"username": "...", "password": "..."}` | Logs in, and sets the session cookie | No |
| `POST /api/logout` | Logs out | No |
| `GET /api/assets` | Lists the assets, who is logged in and which server answered | Yes |
| `POST /api/assets` with `{"request_id": "...", "asset": {...}}` | Adds an asset | Yes |
| `PATCH /api/assets/<id>` with `{"version": n, "changes": {...}}` | Edits an asset, if it is still at that version | Yes |
| `DELETE /api/assets/<id>?version=<n>` | Deletes an asset, if it is still at that version | Yes, as an admin |
| `GET /api/users` | Lists every account and its role, never a password hash | Yes, as an admin |
| `POST /api/users` with `{"username": "...", "password": "...", "role": "user"}` | Creates an account. `role` is `user` or `admin`, and `user` if left out | Yes, as an admin |
| `PATCH /api/users/<username>` with `{"role": "admin"}` | Changes an account's role, unless that would leave no admin | Yes, as an admin |
| `DELETE /api/users/<username>` | Deletes an account and logs it out, unless it is your own or the last admin | Yes, as an admin |

Errors from the servers look like `{"status": "ERROR", "code": "NOT_FOUND", "detail": "..."}`.

| Code | Meaning |
|---|---|
| `BAD_REQUEST` | The message is malformed or the asset is invalid, including any field over 1000 characters |
| `DUPLICATE_ASSET` | The item is already registered. `existing_id` gives its ID |
| `CONFLICT` | Someone else changed the asset after the client read it |
| `NOT_FOUND` | No server that replied has an asset with that ID |
| `NOT_PRIMARY` | This server is on standby. Clients wait and retry while the backup takes over |
| `UNAUTHORIZED` | The request had neither the shared secret from `secret.key` nor a valid login token, or a login was refused |
| `FORBIDDEN` | A logged-in user may not do that, for example a `user` account deleting |
| `UNSUPPORTED_VERSION` | The request had no `protocol_version`, or one the server doesn't support. `supported_versions` lists the ones it does |
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
- The servers can run on separate machines, found by name through configuration instead
  of addresses fixed in the code.
- Programs need a shared secret, and the web page needs a login, with accounts that can be
  created from the page.
- A web page, a single start command, first-time setup and a full reset were added.
- Each program logs to its own file, and the web log records who changed what.
- `demo.py` shows a failover, and `start.py` restarts a server that stops.

### ACID properties

While both servers are running, a change is on both disks before the client gets its reply.

| Property | How it's provided |
|---|---|
| Atomicity | The save writes a temporary file and swaps it into place in one step. If the save fails, the change is undone in memory too. |
| Consistency | Only valid assets with unique IDs and identities are stored. |
| Isolation | A lock makes checking, changing and saving one indivisible step. |
| Durability | The file is forced to disk before the client gets its reply, and on Linux and macOS so is the folder holding it. |

## How far it scales

Running the servers on separate machines makes the system **fault tolerant**: it keeps
working when one machine fails. It doesn't add **capacity**, because every change still
goes through the one primary.

| | Now |
|---|---|
| Server failures it survives | All but one, as long as they don't fail at the same moment as a network split |
| Servers that accept changes | 1, the first one in the list that is running |
| Servers that answer reads | Every server, though a backup may be slightly behind |
| Servers in total | Any number up to 25, 2 by default |
| Copies of `web.py` | 1, because logins are held in its memory |

It could run on Kubernetes with two small changes. Each server would be a StatefulSet with
one pod and a persistent volume, with the service names in `ASSET_SERVERS`,
`ASSET_LISTEN_HOST=0.0.0.0`, and `secret.key` and `tls.pem` mounted from a Kubernetes Secret. Kubernetes
would restart a failed pod, and the backup would cover the gap until it returned. First,
though, the `data/` folder would need to be configurable, because it is always beside the
code, and `web.py` would need to listen on more than `127.0.0.1`. This hasn't been tried.

Growing beyond this would take, roughly in order:

1. **Reading from the backup** for `LIST`, trading slightly old data for more read capacity.
2. **A majority vote**, as in Raft. A change would count once most servers had saved it,
   and a leader would be elected by majority. This would end the network split problem
   below, because the smaller side can't form a majority. It was left out on purpose to
   keep the system simple.
3. **A database instead of the JSON file**, so a change only writes what changed, plus
   paging for `LIST`.
4. **Sessions and accounts in a shared store**, so `web.py` can run as many copies as needed
   behind a load balancer.
5. **Sharding**, splitting the assets across several groups of servers, each with its own
   leader. This is the only step that adds write capacity.

## Known limitations

The biggest one is a network split. A server can't tell a failed server from an
unreachable one, and settling that properly needs the servers to vote, which this system
deliberately doesn't do, however many servers there are. If a backup loses contact with
every server before it while they are still running, it takes over while they carry on,
which keeps the system available. When they reconnect, the primary merges the
data, and some changes are lost even though their clients were told they worked:

- If both servers registered the same item, the primary's record is kept and the backup's
  ID is retired, so anyone who noted that ID will find it gone.
- If both changed an asset's status, the copy with more changes wins, or the primary's if
  they made the same number.
- A delete on either server beats any status change.

This is more likely across separate machines, where the network between them can fail on
its own, than on one machine.

Other limitations:

- The first change after the primary fails took about 8 seconds when timed on one Windows
  machine, mostly waiting for the 3 missed heartbeats. Clients also try the primary first
  on every request, which took about 2 more seconds each time. If both servers are down,
  clients show an error.
- Extra servers are only extra copies, taking over in turn. They don't add capacity, they
  don't vote, and they don't make a network split any safer.
- Every change is copied to every other server one after another, so each server added
  makes every change a little slower.
- Every machine must have the same server list in the same order. Adding a server means
  changing the list on every machine and restarting everything.
- A backup only takes over when every server before it is silent, and it pings them one at
  a time, so the more earlier servers are down, the longer a takeover takes.
- There can be at most 25 servers, because each needs its own letter for asset IDs.
- `start.py` restarts a failed server, but only on one machine. Across machines, failed
  servers have to be restarted by hand or by something like Kubernetes.
- Any program with `secret.key` can stop a server with `STOP`. That is no more access than it
  already has, since it could also delete every asset.
- If the backup was down when the primary took changes and the primary then fails, those
  changes are unavailable until the primary returns. If the primary's disk were also
  destroyed, they would be lost.
- The lock is held while copying to the other servers, so a stalled one can delay changes by
  up to the 5-second timeout before it is marked as behind. Across a slow network, every
  change also waits for the round trip to the backup.
- The backup answers `LIST` while on standby, and may be out of date if it was recently down.
- The servers only use IPv4. A name is looked up as an IPv4 address, and only its first
  address is tried.
- `secret.key` and `tls.pem` have to be copied to each machine by hand, and every program
  shares them. `tls.pem` holds the private key as well as the certificate, so a client
  machine has the key too. That is no more than `secret.key` already gives, but it means
  anyone with the file could pretend to be a server.
- A new certificate means copying the new `tls.pem` to every machine and restarting
  everything, because programs load it once.
- Duplicates are judged only by description, category and location, because nothing
  identifies the physical item. "Dell laptop" and "Dell Latitude" at one site count as
  different items. Category and location are free text.
- The web page uses plain HTTP, so it is only safe while `web.py` listens on 127.0.0.1.
- There are only two roles.
- The roles apply to the web page and the GUI. Anything with `secret.key` has full access.
- A plain copy of the GUI trusts a server's certificate on first use, so someone who can
  intercept that first connection could pose as the server. A copy downloaded from the web
  page has the real certificate built in, so it is not affected. Nothing is remembered, so
  a plain copy decides again every time. Discovery only finds servers on the usual
  ports, on the local network. `web_users.json` isn't copied between servers, so every
  server machine needs its own copy, and a token is only accepted where the account exists.
- Only `web.py` and the GUI know who made a change, so changes from the command-line
  clients are audited with `user` set to `null`. The servers trust the `user` a program sends, since only programs with
  `secret.key` can send one.
- The audit logs are ordinary files, so someone with access to the machine could edit them.
  They are a record, not tamper-proof evidence.
- Each machine keeps its own logs. Following an event across machines means collecting
  their files, and their clocks must agree for the times to line up.
- Accounts belong to `web.py` and are stored beside it. They aren't copied to the backup, so
  they don't protect against losing that machine.
- A password can't be changed from the web page, only with `python web.py --set-password`,
  and `web.py` must then be restarted.
- Don't change the web address to expose the web page on a network you don't trust, because
  passwords would travel unencrypted.
- The web page fetches the whole list on every refresh and searches it in the browser, so
  this will get slow with a very large number of assets.
- An asset's ID can't be changed, so an item can't be moved to a different ID.
- A search looks through every asset in memory, which is fine for thousands of assets but
  not millions. A page is only stable while nothing changes, so an asset added meanwhile can
  shift the next page by one.
- The list of deleted IDs only ever grows, and so does each asset's history and list of
  loans.
- Assets are only flagged for review when a backup registers them. A status change or
  edit made while the primary was offline isn't flagged, although its history shows which
  server made it.
- The GUI's review area only lists flagged assets that the table is showing, so a search
  can hide some. The GUI has a check-out form and a loans list, like the web page, but no calendar; that is only on the web page.
- Loans are whole days, in each machine's local date, with no times or time zones. The
  calendar shows the browser's month.
- Every change rewrites the whole file, changes happen one at a time and every asset is held
  in memory. `LIST` can page, but the handover between the servers can't. Once the data is
  over the 10 MiB message limit, a `LIST` without a `limit` replies `TOO_LARGE`, and the
  primary can't reconcile with a returning backup, although changes still work.
- There is no limit on threads, so a flood of connections could exhaust a server. The idle
  timeout applies to each read, so a client sending one byte at a time could also hold a
  thread for a long time.

## Further development

The scaling steps above come first, especially reading from the backups and a majority vote. Other
ideas are a serial number to identify items, HTTPS for the web page, the shared secret supplied through an
environment variable, changing passwords from the page, and sending every machine's logs
to one place.

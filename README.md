# Asset Management System

Keep a list of your organisation's assets (laptops, furniture, vans...) on a server, and
register, view, search, edit and delete them from several kinds of client program.

There are two servers: a **primary** and a **backup** that keeps a copy of everything.
If the primary stops, the clients carry on using the backup by themselves.

## Quick start

Follow these steps in order. You only need to do steps 1 to 3 once.

### Step 1: Install Python

You need Python 3.8 or newer. To check, open a terminal (see step 2) and type:

```
python --version
```

If it says `Python 3.8` or higher, go to step 2. Otherwise, install Python from
[python.org](https://www.python.org/downloads/). On Windows, tick **"Add python.exe to
PATH"** on the first screen of the installer.

### Step 2: Open a terminal in this folder

All the commands below are typed into a terminal that is open **in this folder** (the
folder that has `server.py` in it).

On Windows: open this folder in File Explorer, click in the address bar at the top,
type `cmd` and press Enter. A black window opens, already in the right folder.

### Step 3: Set up (once only)

Type these commands one at a time, pressing Enter after each:

```
pip install -r requirements.txt
python certificate.py
python users.py add admin admin
python users.py add alice user
```

What they do:

1. Installs the one package the system needs.
2. Creates the security certificate that encrypts every connection
   (`server.crt` and `server.key`).
3. Creates an account called `admin` that can do everything. It asks for a password
   twice. The password must be **at least 8 characters**, and nothing appears on
   screen as you type it; that's normal.
4. Creates an account called `alice` that can do everything except delete. You can use
   any names you like instead of `admin` and `alice`.

### Step 4: Start the servers

Open **two** terminals in this folder (repeat step 2 twice). Type one command in each:

| Terminal | Type | You should see a line ending in |
| --- | --- | --- |
| First | `python server.py` | `Asset Management primary server listening on 0.0.0.0:65432` |
| Second | `python server.py backup` | `Asset Management backup server listening on 0.0.0.0:65433` |

**Leave both windows open.** Closing a window stops that server. The backup is
optional, but without it nothing keeps a copy of your data.

### Step 5: Open the program and log in

Open one more terminal in this folder and type:

```
python gui.py
```

A window opens. Then:

1. Click **Find servers**. The Server box fills in with the primary it found. To use
   a different server, click the arrow on the Server box and pick from the list, or
   type an address yourself (like `192.168.1.20` or `192.168.1.20:65433`).
2. Type your username and password from step 3.
3. Click **Log in**.
4. The first time, a box asks **"Trust this server?"** and shows a long code (the
   certificate fingerprint). Click **Yes**. (If someone else runs the server, check
   the code matches the one they gave you. `python certificate.py` prints it.)

You now see the list of assets. It is empty until you register some.

### Step 6 (optional): Add sample data

To fill the server with example assets to try things out, open another terminal in
this folder and type:

```
python seed.py
```

Log in as `admin` when it asks. It adds 30 sample assets. Click **Show all** in the
window to see them. Type `python seed.py 100` for 100 instead.

## Using the window

| To... | Do this |
| --- | --- |
| Register an asset | Fill in all five boxes under "Asset details", then click **Register new asset**. |
| Edit an asset | Click it in the list. Its details appear in the boxes below. Change them, then click **Save changes**. (The Asset ID can't be changed.) |
| Delete an asset | Click it in the list, then click **Delete selected**. Only `admin` accounts can delete. |
| Search | Type in the box at the top and click **Search**. Choose a field from the drop-down to search only that field, or leave it on "Any field". Click **Show all** to clear the search. |
| See more | The list shows 20 assets at a time. Use **< Previous** and **Next >** at the bottom. |
| Empty the boxes | Click **Clear form**. |

The bar at the bottom of the window tells you what happened, or what went wrong.

## Other ways to connect

You can use any of these instead of `gui.py`, or as well as it. The servers must be
running (step 4) for all of them.

### C# program (Windows, nothing to install)

A Windows program written in C#. It needs no Python, so you can copy it to any
Windows 10 or 11 computer.

1. **Build it.** Double-click `build.bat`. It creates `client.exe` in this folder,
   using the C# compiler that comes with Windows.
2. **Run it.** Double-click `client.exe`. A window opens that works exactly like
   `gui.py` (see step 5 and "Using the window" above).

`client.exe` can also be used from a terminal in this folder. The first two words
after it are always your username and password:

```
client.exe alice yourpassword list
client.exe alice yourpassword register
client.exe alice yourpassword edit SEED-0001 1 location=Lab
client.exe admin yourpassword delete SEED-0001 2
client.exe x x ping
```

- `list` shows every asset.
- `register` on its own asks for each field, one at a time.
- `edit` and `delete` need the asset's current **version** number (the `version` shown
  by `list`). If someone changed the asset since, you get a `CONFLICT` error: run
  `list` again to get the new version.
- `ping` checks a server is there, without logging in (type anything for the
  username and password).

On the command line, `client.exe` reads `config.json` and `server.crt`, so run it in
this folder, or copy those two files next to it.

### Command-line Python programs

```
python send.py                        # register one asset (asks for each field)
python receive.py                     # list every asset
python receive.py laptop              # list assets with "laptop" in any field
python receive.py location=site a     # list assets whose location contains "site a"
```

Each asks for your username and password first.

### Node.js program

If you have [Node.js](https://nodejs.org/) installed, `client.js` takes the same
commands as `client.exe` on the command line:

```
node client.js alice yourpassword list
node client.js alice yourpassword register
node client.js x x ping
```

In PowerShell, use `register` on its own (it asks for each field) rather than typing
JSON after it, because PowerShell removes the quotes from JSON.

## If something goes wrong

| Problem | What to do |
| --- | --- |
| `python` is not recognised | Python isn't installed, or wasn't added to PATH. Reinstall it and tick "Add python.exe to PATH" (step 1). Then open a **new** terminal. |
| `No module named 'cryptography'` | Run `pip install -r requirements.txt` (step 3). |
| A server says the port is already in use | That server is already running in another window. Use that one, or close it first. |
| **Find servers** finds nothing | Check the servers are running (step 4). If you only just started them, wait 5 seconds and try again. If the servers are on another computer, its firewall may be blocking them: type the server's address into the Server box instead. |
| "Could not connect: nothing answered there" | The server isn't running at that address, or the address or port is wrong. |
| "Login failed" or `UNAUTHENTICATED` | Wrong username or password. To reset a password, run `python users.py add NAME admin` (or `user`) again. Run `python users.py list` to see every account. |
| "Certificate changed" | The server's certificate is different from last time (for example after `reset.py` or `certificate.py --replace`). If you expected that, delete `.asset_management_gui.json` (for `gui.py`) or `.asset_management_csharp.json` (for `client.exe`) from your home folder (`C:\Users\YourName`), then log in again. |
| `FORBIDDEN` when deleting | Only `admin` accounts can delete. |
| `CONFLICT` | Someone else changed that asset since you last loaded it. The list has been refreshed: make your change again. |
| `client.exe` won't build | `build.bat` explains what's missing. The C# compiler is part of the .NET Framework 4, which comes with Windows 10 and 11. |

## Starting again from nothing

Close both server windows, then type:

```
python reset.py
```

It lists what it will delete and asks you to type `RESET` to confirm. It deletes every
asset, every account, the logs and the certificate, but never the program files. Then it
offers to make a new certificate and admin account for you. (`python reset.py --yes`
skips the confirmation.)

## Running on several computers

By default everything runs on one computer. To put the servers on different computers:

1. In `config.json`, replace `127.0.0.1` under `servers` with each server computer's
   real IP address (for example `192.168.1.20`). Use the same `config.json` everywhere.
2. Run `python certificate.py --replace`, so the certificate names those addresses.
3. Copy these files from the first computer to the others:
   - `server.crt` and `config.json` go to **every** computer.
   - `server.key`, `users.json` and `secret.key` go **only** to the computer that runs
     the other server. Never give these to anyone else.
4. Allow ports 65432 and 65433 through each server computer's firewall.

`gui.py` and `client.exe` need none of these files. Just type a server's address into
the Server box, or click **Find servers** if it's on the same network.

## Accounts and what they can do

| Account type | Log in | Register, view, search, edit | Delete |
| --- | --- | --- | --- |
| `admin` | yes | yes | yes |
| `user` | yes | yes | no (`FORBIDDEN`) |

## How it works

```
   send.py    receive.py    gui.py    client.exe    client.js
        \          |          |          |          /
         v         v          v          v         v       clients try the primary, and
  +------------------+   copies every change   +------------------+  the backup if the
  |  server.py       | ----------------------> |  server.py backup|  primary is down
  |  (primary)       | <---------------------- |  (backup)        |
  |                  |   takes back changes    |                  |
  +------------------+   after an outage       +------------------+
   assets_primary.json                          assets_backup.json
   audit_primary.log                            audit_backup.log
```

Clients and servers send each other JSON messages over encrypted (TLS) connections.
Users log in and get a session token, and their account type decides what they may do.

This branch (`requirements-only`) builds on the starter code on `main` and adds only
what the requirements ask for. The table at the end shows where each requirement is
met and which tests prove it.

## Files

| File | Purpose |
| --- | --- |
| `server.py` | The server. `python server.py` runs the primary and `python server.py backup` runs the backup. It validates and stores assets, checks logins and roles, saves to disk, keeps the audit log and replicates to the backup. |
| `common.py` | Code shared by every Python program: the message format, protocol version, error codes, loading `config.json`, TLS settings and client failover. |
| `users.py` | Accounts, password hashing and session tokens. Run it to add accounts. |
| `certificate.py` | Creates the TLS certificate and private key. |
| `reset.py` | Deletes everything the system has created, back to a fresh install, then offers to set up again. |
| `gui.py` | Tkinter window: log in, register, view, search, page through, edit and delete. |
| `send.py` | Command-line registration client. |
| `receive.py` | Command-line query client, with optional search. |
| `seed.py` | Fills a running server with sample assets, then edits and deletes some, so every stored field has a value. |
| `client.cs`, `client_gui.cs` | C# client. With no arguments it opens a window like `gui.py`; with arguments it takes the same commands as `client.js`. Built with the C# compiler that comes with Windows. |
| `build.bat` | Builds `client.exe` from the C# files. |
| `client.js` | Minimal Node.js client with no dependencies. |
| `config.json` | Hosts, ports, timeouts, size limits and file names. |
| `requirements.txt` | The one third-party package: `cryptography`, used only by `certificate.py`. |
| `test_system.py`, `test_gui.py`, `test_reset.py` | Automated tests. |

## Messages

Each message is a 4-byte big-endian length followed by that many bytes of UTF-8 JSON,
which must be an object. One connection carries one request and one reply, inside TLS.

Every request includes `"protocol_version": 1`. Every request except `LOGIN` includes
the session `"token"`. Every change includes a unique `"request_id"`; sending the same
request again returns the original reply instead of repeating the change.

| Request | Successful reply |
| --- | --- |
| `{"action": "LOGIN", "username": "alice", "password": "..."}` | `{"status": "OK", "token": "...", "username", "role", "expires_at"}` |
| `{"action": "REGISTER", "request_id", "asset": {"asset_id", "description", "category", "location", "status"}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "LIST"}`, optionally with `"query"`, `"filters": {"field": "text"}`, `"page"`, `"page_size"` | `{"status": "OK", "assets": [...], "total", "page", "page_size", "pages"}` |
| `{"action": "EDIT", "request_id", "asset_id", "expected_version", "changes": {...}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "DELETE", "request_id", "asset_id", "expected_version"}` (admin only) | `{"status": "OK", "deleted": "AST-0001", "stored": {...}}` |

The servers also use `SYNC_STATUS`, `STATE` and `REPLICATE` between themselves. These
need a key derived from `secret.key`, not a user token.

### Stored asset

| Field | Set by | Rules |
| --- | --- | --- |
| `asset_id` | client | 1–32 letters, digits, `-` or `_`, starting with a letter or digit; unique, ignoring capitals; never reused |
| `description`, `category`, `location` | client | text, 1–200 characters |
| `status` | client | `In Use` or `Not In Use`, in any capitalisation |
| `version` | server | 1 when registered, +1 on every edit or delete |
| `created_at`, `updated_at`, `deleted_at` | server | UTC ISO 8601, e.g. `2026-11-05T21:14:03Z` |
| `deleted` | server | `true` once deleted; the record is kept but left out of `LIST` |

### Errors

Errors look like `{"status": "ERROR", "code": "...", "message": "...", "context": {...}}`.
The context names the field, asset or limit concerned.

| Code | Meaning |
| --- | --- |
| `BAD_REQUEST` | The message is malformed or a field is missing, the wrong type or an invalid value |
| `UNKNOWN_ACTION` | The action isn't one the server supports |
| `UNSUPPORTED_VERSION` | `protocol_version` is missing or unsupported; the context lists the supported versions |
| `TOO_LARGE` | The message is over `max_message_bytes` (1 MiB) |
| `DUPLICATE_ASSET` | An asset with that `asset_id` already exists |
| `NOT_FOUND` | No current asset has that `asset_id` |
| `CONFLICT` | The asset changed since the client read it; the context gives the current version |
| `UNAUTHENTICATED` | Wrong login, or a missing, invalid or expired token |
| `FORBIDDEN` | The user's role doesn't allow the action |
| `INTERNAL` | The server failed, for example a full disk; nothing was changed |

## Tests

```
python -m unittest -v test_system test_gui test_reset
```

The tests start real servers on spare ports, with their own certificate, accounts and
data files in a temporary folder, and talk to them over real TLS connections.

`test_gui.py` drives the real window. It skips itself if Tkinter or a display is not
available; on a Linux machine without a screen, run it with `xvfb-run`.

## Requirements traceability

### Functional requirements

| ID | Requirement | Where | Tests |
| --- | --- | --- | --- |
| FR01 | Register an asset | `send.py`, `gui.py`; `server.py` `register`, `validate_asset` | `test_a_registered_asset_is_stored_and_returned_with_server_timestamps`, `test_the_registration_client_prompts_for_each_field_and_registers`, `test_assets_can_be_registered_and_viewed` |
| FR02 | Retrieve assets | `receive.py`; `server.py` `list_assets` | `test_the_query_client_shows_every_asset` |
| FR03 | Use external data representation | `common.py` `send_message`, `receive_message` | `test_messages_are_utf8_json_with_a_length_prefix` |
| FR04 | Identify request type | `server.py` `process` | `test_register_and_list_are_accepted`, `test_unknown_actions_are_rejected` |
| FR05 | Provide registration response | `server.py` `register` | `test_a_registered_asset_is_stored_and_returned_with_server_timestamps` |
| FR06 | Handle empty asset list | `receive.py` | `test_the_query_client_says_when_there_are_no_assets` |
| FR07 | Persist data after changes | `server.py` `save`, `commit` | `test_an_asset_is_on_disk_before_the_client_is_told`, `test_a_failed_save_leaves_the_old_file_whole_and_nothing_in_memory`, `test_assets_are_loaded_again_after_a_restart` |
| FR08 | Provide a Tkinter GUI | `gui.py` | all of `test_gui.py` |
| FR09 | Authenticate users and enforce roles | `users.py`; `server.py` `login`, `authorise`, `ALLOWED_ROLES` | `AccountTest` |
| FR10 | Delete assets as an administrator | `server.py` `delete`, `ALLOWED_ROLES` (soft deletion) | `test_only_an_administrator_can_delete`, `test_a_deleted_asset_is_kept_marked_deleted_and_left_out_of_lists` |
| FR11 | Edit existing assets | `server.py` `edit`; `gui.py` `save_changes` | `test_an_asset_can_be_edited_and_the_updated_record_is_returned`, `test_a_selected_asset_can_be_edited` |
| FR12 | Search assets | `server.py` `list_assets` (`query`, `filters`); `receive.py`; `gui.py` | `SearchTest`, `test_assets_can_be_searched_by_any_field_or_one_field` |
| FR13 | Page or filter asset lists | `server.py` `list_assets` (`page`, `page_size`, metadata) | `test_pages_return_only_their_records_with_paging_metadata`, `test_paging_applies_after_filtering` |

### Non-functional requirements

| ID | Requirement | Where | Tests |
| --- | --- | --- | --- |
| NFR01 | Interoperability | `common.py`: JSON only | `test_messages_are_utf8_json_with_a_length_prefix` |
| NFR02 | Reliable message framing | `common.py` 4-byte length prefix, `_recv_exact` | `test_messages_are_utf8_json_with_a_length_prefix` |
| NFR03 | Usability | `send.py` prompts name each field with an example | `test_the_registration_client_prompts_for_each_field_and_registers` |
| NFR04 | Error handling | `server.py` `process`, `InvalidRequest` | `test_an_invalid_asset_gets_an_error_not_silent_acceptance`, `test_unknown_actions_are_rejected` |
| NFR05 | Support concurrent clients safely | `server.py` thread per connection, `self.lock` on every read and write | `test_many_clients_at_once_complete_without_errors_or_lost_writes`, `test_only_one_of_many_simultaneous_edits_of_a_version_wins` |
| NFR06 | Encrypt data in transit | `certificate.py`; `server.py` `self.tls`; `common.py` `client_tls_context` | `TlsTest` |
| NFR07 | Isolate client failures | `server.py` `handle_connection` | `test_a_client_vanishing_mid_message_does_not_affect_the_server`, `test_an_exception_while_processing_affects_only_that_request` |
| NFR08 | Apply network timeouts | `config.json` timeouts; `server.py`, `common.py` | `test_an_idle_client_does_not_block_others_and_is_disconnected`, `test_a_client_does_not_wait_forever_for_a_silent_server` |
| NFR09 | Provide server replication | `server.py` `sync_with_backup`, `keep_backup_in_sync`; `common.py` `send_request` | `ReplicationTest` |
| NFR10 | Detect conflicting updates | `server.py` `current_asset` | `test_an_edit_based_on_an_old_version_is_a_conflict`, `test_a_delete_based_on_an_old_version_is_a_conflict` |
| NFR11 | Make retried requests idempotent | `server.py` `change`; `common.py` `new_request_id` | `test_repeating_a_registration_stores_one_asset_and_returns_the_same_reply`, `test_repeating_an_edit_or_delete_does_not_repeat_the_change`, `test_a_retry_on_the_backup_of_a_change_the_primary_made_is_not_repeated` |
| NFR12 | Use authoritative server timestamps | `server.py` `utc_now` | `test_timestamps_and_unknown_fields_from_the_client_are_ignored`, `test_an_asset_can_be_edited_and_the_updated_record_is_returned` |
| NFR13 | Validate incoming asset data | `server.py` `validate_asset`, `check_status`, `register` | `test_missing_fields_are_rejected`, `test_wrong_types_empty_and_overlong_values_are_rejected`, `test_only_the_allowed_statuses_are_accepted`, `test_a_duplicate_asset_id_is_rejected_ignoring_capitals` |
| NFR14 | Limit message size | `common.py` `receive_message` checks the header first | `test_an_oversized_message_is_rejected_before_it_is_read` |
| NFR15 | Handle malformed JSON safely | `common.py` `receive_message`; `server.py` `read_and_process` | `test_malformed_json_gets_a_structured_error`, `test_json_that_is_not_an_object_is_rejected` |
| NFR16 | Return structured error codes | `common.py` `error_reply` and codes | context checked throughout the tests |
| NFR17 | Use configurable network settings | `config.json`, `common.py` `load_config` | `test_settings_come_from_the_configuration_file` |
| NFR18 | Declare protocol version | `common.py` `request_from`; `server.py` `process` | `test_a_missing_protocol_version_is_rejected`, `test_an_unsupported_protocol_version_is_rejected` |
| NFR19 | Maintain logging and an audit trail | `server.py` `audit` | `AuditTest` |
| NFR20 | Verify behaviour under concurrent load | `test_system.py` | `test_many_clients_at_once_complete_without_errors_or_lost_writes`, `test_only_one_of_many_simultaneous_edits_of_a_version_wins` |

## Known limitations

- **Network split.** With two servers, neither can tell a failed partner from an
  unreachable one. If the two lose contact while both are running and clients reach
  both, both accept changes. When they reconnect, the server that made more changes
  wins and the other's changes are lost. Fixing this properly needs at least three
  servers voting.
- **Asynchronous replication.** Changes are copied to the backup straight after they
  are saved, not before the client is told. A change made just before the primary
  crashes may be missing from the backup until the primary returns.
- **Manual recovery.** Only one backup is supported, and failed servers must be
  restarted by hand.
- **Account setup on the command line.** Accounts are created with `users.py`, and
  `users.json`, `secret.key` and the certificate files must be copied by hand to a
  second machine.
- **Scale.** Every change rewrites the whole data file and copies all the data to the
  backup, so this suits hundreds or thousands of assets, not millions.

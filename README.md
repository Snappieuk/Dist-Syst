# Asset Management System

A distributed client–server system for registering, listing, searching, editing and
deleting asset records.

A primary server and a backup server run as separate processes, on one machine or
several. Clients (two command-line programs and a Tkinter GUI) talk to them with
length-prefixed UTF-8 JSON messages over TLS-encrypted TCP connections. Users log in
and get a session token, and their role decides what they may do.

This branch (`requirements-only`) builds on the starter code on `main` and adds only
what the requirements ask for. The table at the end shows where each requirement is
met and which tests prove it.

```
   send.py    receive.py    gui.py          clients: log in, then try the primary,
       \          |          /              and the backup if the primary is down
        v         v         v
  +------------------+   copies every change   +------------------+
  |  server.py       | ----------------------> |  server.py backup|
  |  (primary)       | <---------------------- |  (backup)        |
  |                  |   takes back changes    |                  |
  +------------------+   after an outage       +------------------+
   assets_primary.json                          assets_backup.json
   audit_primary.log                            audit_backup.log
```

## Files

| File | Purpose |
| --- | --- |
| `server.py` | The server. `python server.py` runs the primary and `python server.py backup` runs the backup. It validates and stores assets, checks logins and roles, saves to disk, keeps the audit log and replicates to the backup. |
| `common.py` | Code shared by every program: the message format, protocol version, error codes, loading `config.json`, TLS settings and client failover. |
| `users.py` | Accounts, password hashing and session tokens. Run it to add accounts. |
| `certificate.py` | Creates the TLS certificate and private key. |
| `reset.py` | Deletes everything the system has created, back to a fresh install, then offers to set up again. |
| `send.py` | Command-line registration client. |
| `receive.py` | Command-line query client, with optional search. |
| `gui.py` | Tkinter GUI: log in, register, view, search, page through, edit and delete. |
| `client.js` | Minimal Node.js client with no dependencies. It talks to the server only in JSON (TLS, 4-byte length header, UTF-8 JSON object) and fails over to the backup. |
| `config.json` | Hosts, ports, timeouts, size limits and file names. |
| `requirements.txt` | The one third-party package: `cryptography`, used only by `certificate.py`. |
| `test_system.py`, `test_gui.py`, `test_reset.py` | Automated tests. |

## Setting up and running

Requires Python 3.8 or later.

```
pip install -r requirements.txt
python certificate.py                 # creates server.crt and server.key
python users.py add admin admin       # creates an administrator (asks for a password)
python users.py add alice user        # creates an ordinary user
```

Then start each program in its own terminal:

```
python server.py            # the primary
python server.py backup     # the backup
python gui.py               # the GUI, or:
python send.py              # register an asset
python receive.py           # list every asset
python receive.py laptop    # search every field
python receive.py location=site a     # search one field
```

### Node.js client

`client.js` needs only Node.js (no `npm install`). Run it from this folder, since it reads `config.json` and `server.crt` beside itself. The first two arguments are always the username and password; `ping` ignores them but they must still be given.

```
node client.js x x ping
node client.js alice secret list
node client.js alice secret register
node client.js alice secret register '{"asset_id":"A1","description":"Laptop","category":"IT","location":"HQ","status":"In Use"}'
node client.js alice secret edit A1 <expected_version> '{"location":"Lab"}'
node client.js admin secret delete A1 <expected_version>
```

Each command except `ping` logs in first, then sends the request with a fresh `request_id` and prints the server's JSON reply. `register` with no JSON starts a step-by-step guide that asks for each field, offers the two statuses as a menu, and asks you to confirm. Only administrators can delete.

### Resetting

To start again from nothing, stop both servers and run:

```
python reset.py
```

It lists what it will delete and asks you to type `RESET`. It deletes:
- both servers' assets
- the audit logs
- every account
- `secret.key`
- the certificate and its key

It doesn't touch the code or any other file, and it refuses while a server is still
running. It then offers to create a new certificate and admin account straight away.
`python reset.py --yes` skips the confirmation. If you run the servers on two
machines, reset each of them.

### Running on several machines

1. Put each server's real IP address or host name in `servers` in `config.json`, and use
   the same `config.json` on every machine.
2. Run `python certificate.py --replace` so the certificate names those hosts.
3. Copy these files from the first machine to every other machine:
   - `server.crt` and `config.json`
   - `server.key`, `users.json` and `secret.key` (only to the machine that runs the other server)
4. Allow each server's port through its firewall.

No code changes are needed.

### Roles

| Role | Log in | Register, list, search, edit | Delete |
| --- | --- | --- | --- |
| `admin` | yes | yes | yes |
| `user` | yes | yes | no (`FORBIDDEN`) |

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

# Asset Management System

Register, view, search, edit and delete asset records on a server. A backup server
keeps a copy of everything, and clients switch to it by themselves if the primary stops.

| Folder | Contents |
| --- | --- |
| *(this one)* | `server.py`, `common.py` (shared code), `config.json` (ports, hosts, limits) |
| `clients/` | `gui.py` (window), `send.py` / `receive.py` (command line), `client.exe` (C#, from `build.bat`), `client.js` (Node.js) |
| `tools/` | `users.py` (accounts), `certificate.py`, `seed.py` (sample data), `reset.py` |
| `tests/` | Automated tests |

## Setup (once)

Needs Python 3.8+. Open a terminal in this folder and run:

```
pip install -r requirements.txt
python tools/certificate.py
python tools/users.py add admin admin
python tools/users.py add alice user
```

`users.py` asks for a password (8+ characters; nothing shows as you type). `admin`
accounts can do everything; `user` accounts can do everything except delete.

## Run

Start each server in its own terminal and leave them open:

```
python server.py
python server.py backup
```

Then open a client:

```
python clients/gui.py
```

Click **Find servers** (or pick from the Server list, or type an address), enter your
username and password, and click **Log in**. The first time, click **Yes** to trust the
server's certificate.

## Other clients

| Client | How |
| --- | --- |
| C# window (Windows, no Python needed) | Double-click `clients/build.bat`, then `clients/client.exe`. Works like `gui.py`. |
| C# command line | `clients\client.exe alice PASSWORD list` (also `register`, `edit ID VERSION field=value`, `delete ID VERSION`, `ping`) |
| Node.js | `node clients/client.js alice PASSWORD list` (same commands) |
| Python command line | `python clients/send.py` registers one asset; `python clients/receive.py [search]` lists them |

## Tools

| Task | Command |
| --- | --- |
| Add sample data | `python tools/seed.py` (30 assets, or `python tools/seed.py 100`) |
| Add an account or reset a password | `python tools/users.py add NAME admin` (or `user`) |
| Delete everything and start again | Stop the servers, then `python tools/reset.py` |

## Running on several computers

1. In `config.json`, replace `127.0.0.1` under `servers` with each server's real IP
   address, and use the same `config.json` everywhere.
2. Run `python tools/certificate.py --replace`.
3. Copy `server.crt` and `config.json` to every computer, and `server.key`, `users.json`
   and `secret.key` only to the other server's computer.
4. Allow ports 65432 and 65433 through each server's firewall.

The windows (`gui.py`, `client.exe`) need none of these files: type the server's address.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| **Find servers** finds nothing | Check the servers are running. Wait 5 seconds after starting them. Otherwise type the address. |
| "Certificate changed" | Expected after `reset.py` or `certificate.py --replace`: delete `.asset_management_gui.json` (`gui.py`) or `.asset_management_csharp.json` (`client.exe`) from your home folder. |
| `CONFLICT` | Someone else changed that asset first. The list is refreshed; make your change again. |
| `FORBIDDEN` | Only `admin` accounts can delete. |

## Tests

```
python -m unittest discover -v tests
```

The tests start real servers on spare ports, with their own files in a temporary folder.

## Protocol

Each message is a 4-byte big-endian length, then that many bytes of UTF-8 JSON (an
object), over TLS. One request and one reply per connection. Every request has
`"protocol_version": 1`, every request except `LOGIN` and `PING` has the session
`"token"`, and every change has a unique `"request_id"` (a repeated request gets the
original reply, and is not applied twice).

| Request | Reply |
| --- | --- |
| `{"action": "LOGIN", "username", "password"}` | `{"status": "OK", "token", "username", "role", "expires_at"}` |
| `{"action": "REGISTER", "request_id", "asset": {"asset_id", "description", "category", "location", "status"}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "LIST"}`, optionally `"query"`, `"filters": {"field": "text"}`, `"page"`, `"page_size"` | `{"status": "OK", "assets": [...], "total", "page", "page_size", "pages"}` |
| `{"action": "EDIT", "request_id", "asset_id", "expected_version", "changes": {...}}` | `{"status": "OK", "stored": {...}}` |
| `{"action": "DELETE", "request_id", "asset_id", "expected_version"}` (admin only) | `{"status": "OK", "deleted", "stored": {...}}` |

`asset_id` is 1–32 letters, digits, `-` or `_`, unique ignoring capitals. The text
fields are 1–200 characters. `status` is `In Use` or `Not In Use`. The server adds
`version`, `created_at`, `updated_at`, and on deletion `deleted` and `deleted_at`.

Errors are `{"status": "ERROR", "code", "message", "context"}`, with `code` one of
`BAD_REQUEST`, `UNKNOWN_ACTION`, `UNSUPPORTED_VERSION`, `TOO_LARGE`, `DUPLICATE_ASSET`,
`NOT_FOUND`, `CONFLICT`, `UNAUTHENTICATED`, `FORBIDDEN` or `INTERNAL`.

## Requirements traceability

Tests are in `tests/`.

| ID | Requirement | Where | Tests |
| --- | --- | --- | --- |
| FR01 | Register an asset | `clients/send.py`, `clients/gui.py`; `server.py` `register`, `validate_asset` | `test_a_registered_asset_is_stored_and_returned_with_server_timestamps`, `test_the_registration_client_prompts_for_each_field_and_registers`, `test_assets_can_be_registered_and_viewed` |
| FR02 | Retrieve assets | `clients/receive.py`; `server.py` `list_assets` | `test_the_query_client_shows_every_asset` |
| FR03 | Use external data representation | `common.py` `send_message`, `receive_message` | `test_messages_are_utf8_json_with_a_length_prefix` |
| FR04 | Identify request type | `server.py` `process` | `test_register_and_list_are_accepted`, `test_unknown_actions_are_rejected` |
| FR05 | Provide registration response | `server.py` `register` | `test_a_registered_asset_is_stored_and_returned_with_server_timestamps` |
| FR06 | Handle empty asset list | `clients/receive.py` | `test_the_query_client_says_when_there_are_no_assets` |
| FR07 | Persist data after changes | `server.py` `save`, `commit` | `test_an_asset_is_on_disk_before_the_client_is_told`, `test_a_failed_save_leaves_the_old_file_whole_and_nothing_in_memory`, `test_assets_are_loaded_again_after_a_restart` |
| FR08 | Provide a Tkinter GUI | `clients/gui.py` | all of `test_gui.py` |
| FR09 | Authenticate users and enforce roles | `tools/users.py`; `server.py` `login`, `authorise`, `ALLOWED_ROLES` | `AccountTest` |
| FR10 | Delete assets as an administrator | `server.py` `delete`, `ALLOWED_ROLES` (soft deletion) | `test_only_an_administrator_can_delete`, `test_a_deleted_asset_is_kept_marked_deleted_and_left_out_of_lists` |
| FR11 | Edit existing assets | `server.py` `edit`; `clients/gui.py` `save_changes` | `test_an_asset_can_be_edited_and_the_updated_record_is_returned`, `test_a_selected_asset_can_be_edited` |
| FR12 | Search assets | `server.py` `list_assets` (`query`, `filters`); `clients/receive.py`; `clients/gui.py` | `SearchTest`, `test_assets_can_be_searched_by_any_field_or_one_field` |
| FR13 | Page or filter asset lists | `server.py` `list_assets` (`page`, `page_size`, metadata) | `test_pages_return_only_their_records_with_paging_metadata`, `test_paging_applies_after_filtering` |
| NFR01 | Interoperability | `common.py`: JSON only; `clients/client.js`, `clients/client.cs` | `test_messages_are_utf8_json_with_a_length_prefix` |
| NFR02 | Reliable message framing | `common.py` 4-byte length prefix, `_recv_exact` | `test_messages_are_utf8_json_with_a_length_prefix` |
| NFR03 | Usability | `clients/send.py` prompts name each field with an example | `test_the_registration_client_prompts_for_each_field_and_registers` |
| NFR04 | Error handling | `server.py` `process`, `InvalidRequest` | `test_an_invalid_asset_gets_an_error_not_silent_acceptance`, `test_unknown_actions_are_rejected` |
| NFR05 | Support concurrent clients safely | `server.py` thread per connection, `self.lock` on every read and write | `test_many_clients_at_once_complete_without_errors_or_lost_writes`, `test_only_one_of_many_simultaneous_edits_of_a_version_wins` |
| NFR06 | Encrypt data in transit | `tools/certificate.py`; `server.py` `self.tls`; `common.py` `client_tls_context` | `TlsTest` |
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
| NFR20 | Verify behaviour under concurrent load | `tests/test_system.py` | `test_many_clients_at_once_complete_without_errors_or_lost_writes`, `test_only_one_of_many_simultaneous_edits_of_a_version_wins` |

## Known limitations

- **Network split:** if the two servers lose contact while clients reach both, both
  accept changes, and on reconnecting the one with fewer changes loses them.
- **Asynchronous replication:** a change made just before the primary crashes may be
  missing from the backup until the primary returns.
- **Manual recovery:** one backup only; failed servers are restarted by hand.
- **Scale:** every change rewrites the whole data file, so this suits thousands of
  assets, not millions.

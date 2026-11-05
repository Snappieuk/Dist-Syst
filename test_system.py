"""
Automated tests for the Asset Management System.

    python -m unittest -v test_system

Each test starts real servers on spare ports, with their own temporary data files,
and talks to them over real sockets.
"""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

import contextlib
import io
from unittest import mock

import ssl

import certificate
import common
import receive
import send
import server
import users

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "alice-password"


def spare_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(folder: str) -> dict:
    """A configuration like config.json, but with spare ports and files in `folder`."""
    with open(common.CONFIG_FILE, encoding="utf-8") as file:
        config = json.load(file)
    for role in config["servers"]:
        config["servers"][role] = {"host": "127.0.0.1", "port": spare_port()}
    # Short timeouts keep the tests quick.
    config["server_timeout_seconds"] = 1
    config["client_timeout_seconds"] = 2
    config["sync_interval_seconds"] = 0.2
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as file:
        json.dump(config, file)
    config = common.load_config(path)
    certificate.create_certificate(config)
    return config


def make_asset(asset_id: str = "AST-0001", **changes) -> dict:
    asset = {
        "asset_id": asset_id,
        "description": f"Laptop {asset_id}",
        "category": "IT Equipment",
        "location": "Site A",
        "status": "In Use",
    }
    asset.update(changes)
    return asset


class ServerTestCase(unittest.TestCase):
    """Starts a primary server in a background thread for each test."""

    def setUp(self) -> None:
        self.folder = tempfile.mkdtemp()
        # Server threads may still be finishing a write as a test ends, so errors
        # while deleting the folder are ignored.
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config = make_config(self.folder)
        # Few hash rounds keep the tests quick; real accounts use users.HASH_ITERATIONS.
        users.add_user(self.config, "admin", ADMIN_PASSWORD, "admin", iterations=1000)
        users.add_user(self.config, "alice", USER_PASSWORD, "user", iterations=1000)
        self.server = self.start_server("primary")
        self.token = self.log_in("admin", ADMIN_PASSWORD)

    def log_in(self, username: str, password: str) -> str:
        reply = common.login(username, password, self.config)
        self.assertEqual(reply["status"], "OK", reply)
        return reply["token"]

    def start_server(self, role: str) -> server.AssetServer:
        node = server.AssetServer(self.config, role)
        stop = threading.Event()
        thread = threading.Thread(target=node.serve, args=(stop,), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        self.running = getattr(self, "running", {})
        self.running[role] = (stop, thread)
        self.wait_for_port(node.address)
        return node

    def stop_server(self, role: str) -> None:
        """Stop a server as if it had failed: it stops answering and its port closes."""
        stop, thread = self.running.pop(role)
        stop.set()
        thread.join(5)

    def wait_for_port(self, address: tuple) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(address, timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail(f"Server at {address} never started")

    def request(self, request: dict) -> dict:
        """Send a request as the admin, unless it names its own token."""
        if "token" not in request and request.get("action") != "LOGIN":
            request = {**request, "token": self.token}
        return common.send_request(request, self.config)

    def register(self, asset: dict, request_id: str = None) -> dict:
        request_id = request_id or common.new_request_id()
        return self.request({"action": "REGISTER", "request_id": request_id, "asset": asset})

    def edit(self, asset_id: str, version: int, request_id: str = None, **changes) -> dict:
        return self.request({
            "action": "EDIT",
            "request_id": request_id or common.new_request_id(),
            "asset_id": asset_id,
            "expected_version": version,
            "changes": changes,
        })

    def delete(self, asset_id: str, version: int, request_id: str = None) -> dict:
        return self.request({
            "action": "DELETE",
            "request_id": request_id or common.new_request_id(),
            "asset_id": asset_id,
            "expected_version": version,
        })

    def listed(self) -> list:
        return self.request({"action": "LIST"})["assets"]

    def run_query_client(self, arguments: list) -> None:
        """Run receive.py, logging in as alice."""
        with mock.patch("builtins.input", return_value="alice"), mock.patch("getpass.getpass", return_value=USER_PASSWORD):
            receive.main(self.config, arguments)

    def raw_exchange(self, payload: bytes) -> dict:
        """Send raw bytes to the primary over TLS and return its decoded reply."""
        context = common.client_tls_context(self.config)
        with socket.create_connection(self.server.address, timeout=5) as raw_sock:
            with context.wrap_socket(raw_sock, server_hostname="127.0.0.1") as sock:
                sock.sendall(payload)
                return common.receive_message(sock, self.config["max_message_bytes"])

    def frame(self, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + body


class ProtocolTest(ServerTestCase):
    """FR03, FR04, NFR01, NFR02, NFR04, NFR14, NFR15, NFR16, NFR17, NFR18."""

    def test_settings_come_from_the_configuration_file(self):
        self.assertEqual(self.server.address[1], self.config["servers"]["primary"]["port"])

    def test_register_and_list_are_accepted(self):
        reply = self.register(make_asset())
        self.assertEqual(reply["status"], "OK")
        self.assertEqual(self.request({"action": "LIST"})["status"], "OK")

    def test_unknown_actions_are_rejected(self):
        reply = self.request({"action": "DANCE"})
        self.assertEqual(reply["code"], common.UNKNOWN_ACTION)
        self.assertEqual(reply["context"], {"action": "DANCE"})

    def test_a_missing_protocol_version_is_rejected(self):
        body = json.dumps({"action": "LIST"}).encode("utf-8")
        reply = self.raw_exchange(self.frame(body))
        self.assertEqual(reply["code"], common.UNSUPPORTED_VERSION)
        self.assertEqual(reply["context"]["supported_versions"], [common.PROTOCOL_VERSION])

    def test_an_unsupported_protocol_version_is_rejected(self):
        body = json.dumps({"action": "LIST", "protocol_version": 99}).encode("utf-8")
        self.assertEqual(self.raw_exchange(self.frame(body))["code"], common.UNSUPPORTED_VERSION)

    def test_messages_are_utf8_json_with_a_length_prefix(self):
        body = json.dumps({"action": "LIST", "protocol_version": 1, "token": self.token}).encode("utf-8")
        reply = self.raw_exchange(self.frame(body))
        self.assertEqual((reply["status"], reply["assets"]), ("OK", []))

    def test_malformed_json_gets_a_structured_error(self):
        reply = self.raw_exchange(self.frame(b"{not json"))
        self.assertEqual(reply["status"], "ERROR")
        self.assertEqual(reply["code"], common.BAD_REQUEST)
        self.assertIn("message", reply)
        self.assertEqual(self.request({"action": "LIST"})["assets"], [])

    def test_json_that_is_not_an_object_is_rejected(self):
        self.assertEqual(self.raw_exchange(self.frame(b"[1, 2]"))["code"], common.BAD_REQUEST)

    def test_an_oversized_message_is_rejected_before_it_is_read(self):
        too_big = self.config["max_message_bytes"] + 1
        reply = self.raw_exchange(too_big.to_bytes(4, "big"))  # header only: no payload sent
        self.assertEqual(reply["code"], common.TOO_LARGE)

    def test_the_client_refuses_to_send_an_oversized_message(self):
        huge = {"action": "REGISTER", "asset": {"description": "x" * self.config["max_message_bytes"]}}
        with self.assertRaises(common.MessageTooLargeError):
            self.request(huge)

    def test_an_invalid_asset_gets_an_error_not_silent_acceptance(self):
        reply = self.register("laptop")
        self.assertEqual(reply["code"], common.BAD_REQUEST)
        self.assertEqual(reply["context"], {"field": "asset"})


class AssetDataTest(ServerTestCase):
    """FR01, FR02, FR05, FR06, FR07, NFR03, NFR12, NFR13."""

    def test_a_registered_asset_is_stored_and_returned_with_server_timestamps(self):
        reply = self.register(make_asset())
        self.assertEqual(reply["status"], "OK")
        stored = reply["stored"]
        for field, value in make_asset().items():
            self.assertEqual(stored[field], value)
        self.assertEqual(stored["version"], 1)
        self.assertRegex(stored["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(stored["created_at"], stored["updated_at"])
        self.assertEqual(self.request({"action": "LIST"})["assets"], [stored])

    def test_timestamps_and_unknown_fields_from_the_client_are_ignored(self):
        asset = make_asset(created_at="1999-01-01T00:00:00Z", date_registered="1999-01-01", owner="me")
        stored = self.register(asset)["stored"]
        self.assertNotEqual(stored["created_at"], "1999-01-01T00:00:00Z")
        self.assertNotIn("date_registered", stored)
        self.assertNotIn("owner", stored)

    def test_missing_fields_are_rejected(self):
        for field in common.ASSET_FIELDS:
            asset = make_asset()
            del asset[field]
            reply = self.register(asset)
            self.assertEqual((reply["code"], reply["context"]["field"]), (common.BAD_REQUEST, field))

    def test_wrong_types_empty_and_overlong_values_are_rejected(self):
        for bad in (42, None, ["a"], "", "   ", "x" * (server.MAX_FIELD_LENGTH + 1)):
            reply = self.register(make_asset(description=bad))
            self.assertEqual(reply["code"], common.BAD_REQUEST, bad)
            self.assertEqual(reply["context"]["field"], "description")

    def test_invalid_asset_ids_are_rejected(self):
        for bad in ("-AST", "AST 1", "AST/1", "A" * 33):
            self.assertEqual(self.register(make_asset(bad))["context"]["field"], "asset_id", bad)

    def test_only_the_allowed_statuses_are_accepted(self):
        reply = self.register(make_asset(status="Broken"))
        self.assertEqual(reply["code"], common.BAD_REQUEST)
        self.assertEqual(reply["context"]["allowed"], list(common.STATUSES))
        self.assertEqual(self.register(make_asset(status="  not in USE "))["stored"]["status"], "Not In Use")

    def test_a_duplicate_asset_id_is_rejected_ignoring_capitals(self):
        self.register(make_asset("AST-0001"))
        reply = self.register(make_asset("ast-0001", description="Something else"))
        self.assertEqual(reply["code"], common.DUPLICATE_ASSET)
        self.assertEqual(reply["context"]["asset_id"], "ast-0001")
        self.assertEqual(len(self.request({"action": "LIST"})["assets"]), 1)

    def test_an_asset_is_on_disk_before_the_client_is_told(self):
        self.register(make_asset())
        with open(self.server.data_file, encoding="utf-8") as file:
            self.assertEqual([a["asset_id"] for a in json.load(file)["assets"]], ["AST-0001"])

    def test_assets_are_loaded_again_after_a_restart(self):
        stored = self.register(make_asset())["stored"]
        restarted = server.AssetServer(self.config)
        self.assertEqual(list(restarted.assets.values()), [stored])

    def test_a_failed_save_leaves_the_old_file_whole_and_nothing_in_memory(self):
        self.register(make_asset("AST-0001"))
        with mock.patch("server.json.dump", side_effect=OSError("disk full")):
            reply = self.register(make_asset("AST-0002"))
        self.assertEqual(reply["code"], common.INTERNAL)
        self.assertEqual([a["asset_id"] for a in self.request({"action": "LIST"})["assets"]], ["AST-0001"])
        with open(self.server.data_file, encoding="utf-8") as file:
            self.assertEqual(len(json.load(file)["assets"]), 1)

    def test_a_damaged_data_file_stops_the_server_instead_of_being_overwritten(self):
        with open(self.server.data_file, "w", encoding="utf-8") as file:
            file.write("{broken")
        with self.assertRaises(ValueError):
            server.AssetServer(self.config)

    def test_the_registration_client_prompts_for_each_field_and_registers(self):
        answers = iter(["alice", "AST-0009", "Projector", "AV", "Room 2", "3", "2"])
        prompts = []

        def fake_input(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        output = io.StringIO()
        with mock.patch("builtins.input", fake_input), mock.patch("getpass.getpass", return_value=USER_PASSWORD), \
                contextlib.redirect_stdout(output):
            send.main(self.config)
        for word in ("Username", "Asset ID", "Description", "Category", "Location", "Status"):
            self.assertTrue(any(word in prompt for prompt in prompts), word)
        self.assertIn("Registered AST-0009", output.getvalue())
        self.assertIn("Please type a number", output.getvalue())  # "3" was not a valid choice
        self.assertEqual(self.request({"action": "LIST"})["assets"][0]["status"], "Not In Use")

    def test_the_query_client_says_when_there_are_no_assets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_query_client([])
        self.assertIn("No assets registered yet.", output.getvalue())

    def test_the_query_client_shows_every_asset(self):
        self.register(make_asset("AST-0001"))
        self.register(make_asset("AST-0002"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_query_client([])
        self.assertIn("2 asset(s) registered", output.getvalue())
        self.assertIn("AST-0002 | Laptop AST-0002 | IT Equipment | Site A | In Use", output.getvalue())



class ConcurrencyTest(ServerTestCase):
    """NFR05, NFR07, NFR08, NFR20."""

    def test_many_clients_at_once_complete_without_errors_or_lost_writes(self):
        replies = []
        errors = []

        def register(n: int) -> None:
            try:
                replies.append(self.register(make_asset(f"AST-{n}")))
            except Exception as exc:  # any failure must fail the test, not vanish in a thread
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(n,)) for n in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual([reply["status"] for reply in replies], ["OK"] * 50)
        stored = sorted(asset["asset_id"] for asset in self.request({"action": "LIST"})["assets"])
        self.assertEqual(stored, sorted(f"AST-{n}" for n in range(50)))

    def test_an_idle_client_does_not_block_others_and_is_disconnected(self):
        idle = socket.create_connection(self.server.address)
        self.addCleanup(idle.close)
        started = time.monotonic()
        self.assertEqual(self.request({"action": "LIST"})["status"], "OK")
        self.assertLess(time.monotonic() - started, 0.5)  # not held up by the idle client

        idle.settimeout(5)
        self.assertEqual(idle.recv(1), b"")  # the server closed it after its timeout

    def test_a_client_vanishing_mid_message_does_not_affect_the_server(self):
        with socket.create_connection(self.server.address) as sock:
            sock.sendall(b"\x00\x00")  # half a header, then gone
        self.assertEqual(self.request({"action": "LIST"})["status"], "OK")

    def test_an_exception_while_processing_affects_only_that_request(self):
        original = self.server.process
        self.server.process = lambda request: 1 / 0
        try:
            self.assertEqual(self.request({"action": "LIST"})["code"], common.INTERNAL)
        finally:
            self.server.process = original
        self.assertEqual(self.request({"action": "LIST"})["status"], "OK")

    def test_a_client_does_not_wait_forever_for_a_silent_server(self):
        with socket.socket() as silent:
            silent.bind(("127.0.0.1", 0))
            silent.listen()
            config = dict(self.config, servers={"primary": {"host": "127.0.0.1", "port": silent.getsockname()[1]}})
            started = time.monotonic()
            with self.assertRaises(OSError):
                common.send_request({"action": "LIST"}, config)
            self.assertLess(time.monotonic() - started, config["client_timeout_seconds"] + 1)



class SearchTest(ServerTestCase):
    """FR12, FR13."""

    def setUp(self) -> None:
        super().setUp()
        self.register(make_asset("AST-0001", description="Dell laptop", location="Site A"))
        self.register(make_asset("AST-0002", description="HP printer", category="Printers", location="Site B"))
        self.register(make_asset("AST-0003", description="Dell monitor", location="Site B", status="Not In Use"))

    def ids(self, **parameters) -> list:
        return [asset["asset_id"] for asset in self.request({"action": "LIST", **parameters})["assets"]]

    def test_each_supported_field_can_be_searched_ignoring_capitals(self):
        self.assertEqual(self.ids(filters={"asset_id": "ast-0002"}), ["AST-0002"])
        self.assertEqual(self.ids(filters={"description": "DELL"}), ["AST-0001", "AST-0003"])
        self.assertEqual(self.ids(filters={"category": "printers"}), ["AST-0002"])
        self.assertEqual(self.ids(filters={"location": "site b"}), ["AST-0002", "AST-0003"])
        self.assertEqual(self.ids(filters={"status": "not in use"}), ["AST-0003"])

    def test_filters_combine_and_a_query_searches_every_field(self):
        self.assertEqual(self.ids(filters={"description": "dell", "location": "site b"}), ["AST-0003"])
        self.assertEqual(self.ids(query="printer"), ["AST-0002"])
        self.assertEqual(self.ids(query="site"), ["AST-0001", "AST-0002", "AST-0003"])

    def test_an_unknown_search_field_is_rejected(self):
        reply = self.request({"action": "LIST", "filters": {"colour": "red"}})
        self.assertEqual((reply["code"], reply["context"]["field"]), (common.BAD_REQUEST, "colour"))

    def test_deleted_assets_are_not_found(self):
        self.delete("AST-0001", 1)
        self.assertEqual(self.ids(query="dell"), ["AST-0003"])

    def test_pages_return_only_their_records_with_paging_metadata(self):
        first = self.request({"action": "LIST", "page": 1, "page_size": 2})
        self.assertEqual([a["asset_id"] for a in first["assets"]], ["AST-0001", "AST-0002"])
        self.assertEqual((first["total"], first["page"], first["page_size"], first["pages"]), (3, 1, 2, 2))
        second = self.request({"action": "LIST", "page": 2, "page_size": 2})
        self.assertEqual([a["asset_id"] for a in second["assets"]], ["AST-0003"])

    def test_paging_applies_after_filtering(self):
        reply = self.request({"action": "LIST", "filters": {"location": "b"}, "page": 2, "page_size": 1})
        self.assertEqual(([a["asset_id"] for a in reply["assets"]], reply["total"], reply["pages"]), (["AST-0003"], 2, 2))

    def test_without_paging_every_match_is_returned_with_metadata(self):
        reply = self.request({"action": "LIST"})
        self.assertEqual((len(reply["assets"]), reply["total"], reply["pages"]), (3, 3, 1))

    def test_invalid_paging_values_are_rejected(self):
        for parameters in ({"page": 0}, {"page": "2"}, {"page_size": 0}, {"page_size": server.MAX_PAGE_SIZE + 1}):
            reply = self.request({"action": "LIST", **parameters})
            self.assertEqual(reply["code"], common.BAD_REQUEST, parameters)

    def test_the_query_client_can_search(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_query_client(["location=site", "b"])
        self.assertIn("2 asset(s) found", output.getvalue())
        self.assertNotIn("AST-0001", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_query_client(["tablet"])
        self.assertIn("No assets match that search.", output.getvalue())


class AccountTest(ServerTestCase):
    """FR09, FR10."""

    def test_only_salted_password_hashes_are_stored(self):
        with open(os.path.join(self.folder, "users.json"), encoding="utf-8") as file:
            text = file.read()
        self.assertNotIn(ADMIN_PASSWORD, text)
        record = json.loads(text)["admin"]
        self.assertEqual(set(record), {"salt", "iterations", "hash", "role"})
        same_password_twice = users.hash_password("same-password"), users.hash_password("same-password")
        self.assertNotEqual(same_password_twice[0]["hash"], same_password_twice[1]["hash"])

    def test_a_login_issues_a_time_limited_token_and_the_role(self):
        reply = common.login("Alice", USER_PASSWORD, self.config)
        self.assertEqual((reply["status"], reply["username"], reply["role"]), ("OK", "alice", "user"))
        self.assertRegex(reply["expires_at"], r"Z$")
        self.assertEqual(users.read_token(reply["token"], self.server.secret), "alice")

    def test_a_wrong_password_and_an_unknown_user_get_the_same_error(self):
        wrong = common.login("alice", "not-her-password", self.config)
        unknown = common.login("nobody", "whatever-123", self.config)
        self.assertEqual(wrong["code"], common.UNAUTHENTICATED)
        self.assertEqual(wrong["message"], unknown["message"])
        self.assertNotIn("token", wrong)

    def test_every_action_except_login_needs_a_token(self):
        for action in ("LIST", "REGISTER", "EDIT", "DELETE"):
            reply = common.send_request({"action": action, "request_id": "x"}, self.config)
            self.assertEqual(reply["code"], common.UNAUTHENTICATED, action)

    def test_a_forged_or_altered_token_is_refused(self):
        other_key = b"not-the-servers-key"
        forged, _ = users.issue_token("admin", other_key, 3600)
        self.assertEqual(self.request({"action": "LIST", "token": forged})["code"], common.UNAUTHENTICATED)
        body, signature = self.token.split(".")
        altered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + signature
        self.assertEqual(self.request({"action": "LIST", "token": altered})["code"], common.UNAUTHENTICATED)

    def test_an_expired_token_is_refused(self):
        expired, _ = users.issue_token("admin", self.server.secret, -1)
        reply = self.request({"action": "LIST", "token": expired})
        self.assertEqual(reply["code"], common.UNAUTHENTICATED)
        self.assertIn("expired", reply["message"])

    def test_only_an_administrator_can_delete(self):
        self.register(make_asset())
        alice = self.log_in("alice", USER_PASSWORD)
        reply = self.request({
            "action": "DELETE", "request_id": "r", "asset_id": "AST-0001", "expected_version": 1, "token": alice,
        })
        self.assertEqual(reply["code"], common.FORBIDDEN)
        self.assertEqual(reply["context"]["allowed_roles"], ["admin"])
        self.assertEqual(len(self.listed()), 1)
        self.assertEqual(self.delete("AST-0001", 1)["status"], "OK")  # as admin

    def test_ordinary_users_can_register_list_search_and_edit(self):
        alice = self.log_in("alice", USER_PASSWORD)
        reply = self.request({"action": "REGISTER", "request_id": "a1", "asset": make_asset(), "token": alice})
        self.assertEqual(reply["status"], "OK")
        reply = self.request({
            "action": "EDIT", "request_id": "a2", "asset_id": "AST-0001", "expected_version": 1,
            "changes": {"location": "Site B"}, "token": alice,
        })
        self.assertEqual(reply["status"], "OK")
        reply = self.request({"action": "LIST", "query": "site b", "token": alice})
        self.assertEqual(reply["total"], 1)

    def test_a_role_change_or_removed_account_takes_effect_at_once(self):
        alice = self.log_in("alice", USER_PASSWORD)
        users.add_user(self.config, "alice", USER_PASSWORD, "admin", iterations=1000)
        self.register(make_asset())
        reply = self.request({
            "action": "DELETE", "request_id": "r", "asset_id": "AST-0001", "expected_version": 1, "token": alice,
        })
        self.assertEqual(reply["status"], "OK")

        accounts = users.load_users(self.config)
        del accounts["alice"]
        users.write_private_file(os.path.join(self.folder, "users.json"), json.dumps(accounts))
        self.assertEqual(self.request({"action": "LIST", "token": alice})["code"], common.UNAUTHENTICATED)

    def test_unacceptable_accounts_are_refused(self):
        for username, password, role in (("bad name", "long-enough", "user"), ("bob", "short", "user"), ("bob", "long-enough", "boss")):
            with self.assertRaises(ValueError):
                users.add_user(self.config, username, password, role)


class AuditTest(ServerTestCase):
    """NFR19."""

    def events(self, name: str = None) -> list:
        with open(self.server.audit_file, encoding="utf-8") as file:
            records = [json.loads(line) for line in file]
        return [record for record in records if name is None or record["event"] == name]

    def test_every_record_has_a_server_utc_timestamp_and_the_server_role(self):
        for record in self.events():
            self.assertRegex(record["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(record["server"], "primary")
        self.assertEqual(len(self.events("server_started")), 1)

    def test_asset_changes_are_recorded_with_the_user_and_request_id(self):
        self.register(make_asset(), request_id="reg-1")
        self.edit("AST-0001", 1, request_id="edit-1", location="Site B")
        self.delete("AST-0001", 2, request_id="del-1")
        changes = [(r["event"], r["user"], r["request_id"], r["asset_id"], r["version"])
                   for r in self.events() if r["event"] in ("register", "edit", "delete")]
        self.assertEqual(changes, [
            ("register", "admin", "reg-1", "AST-0001", 1),
            ("edit", "admin", "edit-1", "AST-0001", 2),
            ("delete", "admin", "del-1", "AST-0001", 3),
        ])
        self.assertEqual(self.events("edit")[0]["fields"], ["location"])

    def test_a_replayed_change_is_recorded_once(self):
        self.register(make_asset(), request_id="once")
        self.register(make_asset(), request_id="once")
        self.assertEqual(len(self.events("register")), 1)

    def test_security_events_are_recorded(self):
        common.login("alice", "wrong-password", self.config)
        self.request({"action": "LIST", "token": "nonsense"})
        alice = self.log_in("alice", USER_PASSWORD)
        self.register(make_asset())
        self.request({"action": "DELETE", "request_id": "d", "asset_id": "AST-0001", "expected_version": 1, "token": alice})

        self.assertEqual(self.events("login_failed")[0]["user"], "alice")
        self.assertIn("alice", [r["user"] for r in self.events("login")])
        self.assertEqual(self.events("unauthenticated")[0]["action"], "LIST")
        forbidden = self.events("forbidden")[0]
        self.assertEqual((forbidden["user"], forbidden["action"], forbidden["request_id"]), ("alice", "DELETE", "d"))

    def test_passwords_and_tokens_never_reach_the_audit_log(self):
        common.login("alice", "wrong-password", self.config)
        with open(self.server.audit_file, encoding="utf-8") as file:
            text = file.read()
        for secret in (ADMIN_PASSWORD, "wrong-password", self.token):
            self.assertNotIn(secret, text)

    def test_system_errors_are_recorded_with_the_request_id(self):
        with mock.patch("server.json.dump", side_effect=OSError("disk full")):
            self.register(make_asset(), request_id="failing")
        error = self.events("error")[0]
        self.assertEqual((error["action"], error["request_id"]), ("REGISTER", "failing"))
        self.assertIn("disk full", error["detail"])

    def test_rejected_messages_are_recorded(self):
        self.raw_exchange(self.frame(b"{broken"))
        self.raw_exchange(self.frame(json.dumps({"action": "LIST"}).encode("utf-8")))
        self.assertEqual(len(self.events("rejected_message")), 1)
        self.assertEqual(len(self.events("rejected_version")), 1)


class TlsTest(ServerTestCase):
    """NFR06."""

    def test_connections_use_tls_1_2_or_later(self):
        context = common.client_tls_context(self.config)
        with socket.create_connection(self.server.address, timeout=5) as raw_sock:
            with context.wrap_socket(raw_sock, server_hostname="127.0.0.1") as sock:
                self.assertIn(sock.version(), ("TLSv1.2", "TLSv1.3"))
                self.assertIsNotNone(sock.cipher())

    def test_a_client_rejects_a_server_with_an_untrusted_certificate(self):
        with tempfile.TemporaryDirectory() as other_folder:
            impostor_config = make_config(other_folder)  # a different, self-signed certificate
            impostor_config["servers"] = {"primary": self.config["servers"]["primary"]}
            with self.assertRaises(ssl.SSLCertVerificationError):
                common.send_request({"action": "LOGIN", "username": "admin", "password": ADMIN_PASSWORD}, impostor_config)
        self.assertEqual(len(self.events_named("login")), 1)  # only setUp's login reached the server

    def test_unencrypted_requests_are_refused(self):
        body = json.dumps({"action": "LOGIN", "protocol_version": 1, "username": "admin", "password": ADMIN_PASSWORD})
        with socket.create_connection(self.server.address, timeout=5) as sock:
            sock.sendall(self.frame(body.encode("utf-8")))
            sock.settimeout(5)
            try:
                received = sock.recv(4096)
            except ConnectionResetError:
                received = b""
        self.assertNotIn(b"token", received)
        self.wait_until_logged("tls_failed")

    def test_the_password_does_not_cross_the_network_as_plain_text(self):
        # Capture what the client actually sends by putting a relay in the middle.
        captured = bytearray()
        relay = socket.socket()
        relay.bind(("127.0.0.1", 0))
        relay.listen()
        self.addCleanup(relay.close)

        def forward(source, destination):
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    if source is client_side[0]:
                        captured.extend(data)
                    destination.sendall(data)
            except OSError:
                pass
            finally:
                destination.close()

        client_side = []

        def accept():
            conn, _ = relay.accept()
            client_side.append(conn)
            upstream = socket.create_connection(self.server.address)
            threading.Thread(target=forward, args=(conn, upstream), daemon=True).start()
            forward(upstream, conn)

        threading.Thread(target=accept, daemon=True).start()
        relayed = dict(self.config, servers={"primary": {"host": "127.0.0.1", "port": relay.getsockname()[1]}})
        reply = common.login("admin", ADMIN_PASSWORD, relayed)
        self.assertEqual(reply["status"], "OK")
        self.assertGreater(len(captured), 0)
        self.assertNotIn(ADMIN_PASSWORD.encode("utf-8"), bytes(captured))
        self.assertNotIn(b"LOGIN", bytes(captured))

    def events_named(self, name: str) -> list:
        with open(self.server.audit_file, encoding="utf-8") as file:
            return [record for record in map(json.loads, file) if record["event"] == name]

    def wait_until_logged(self, name: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.events_named(name):
                return
            time.sleep(0.05)
        self.fail(f"{name} was never logged")


class ReplicationTest(ServerTestCase):
    """NFR09."""

    def setUp(self) -> None:
        super().setUp()
        self.backup = self.start_server("backup")

    def wait_until(self, condition, explanation: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"Never {explanation}")

    def ids_on(self, node: server.AssetServer) -> list:
        with node.lock:
            return sorted(asset_id for asset_id, asset in node.assets.items() if not asset.get("deleted"))

    def test_each_change_reaches_the_backup_and_its_disk(self):
        self.register(make_asset("AST-0001"))
        self.edit("AST-0001", 1, location="Site B")
        self.register(make_asset("AST-0002"))
        self.delete("AST-0002", 1)
        self.wait_until(lambda: self.backup.seq == self.server.seq == 4, "copied every change")
        with self.backup.lock:
            self.assertEqual(self.backup.assets, self.server.assets)
        with open(self.backup.data_file, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["seq"], 4)

    def test_clients_carry_on_with_the_backup_when_the_primary_fails(self):
        self.register(make_asset("AST-0001"))
        self.wait_until(lambda: self.backup.seq == 1, "copied the first asset")
        self.stop_server("primary")

        # The same client code, the same token: it moves to the backup by itself.
        self.assertEqual(self.listed()[0]["asset_id"], "AST-0001")
        self.assertEqual(self.register(make_asset("AST-0002"))["status"], "OK")
        self.assertEqual(self.ids_on(self.backup), ["AST-0001", "AST-0002"])
        self.assertEqual(self.log_in("alice", USER_PASSWORD).count("."), 1)  # logins work too

    def test_a_returning_primary_takes_the_changes_made_on_the_backup(self):
        self.register(make_asset("AST-0001"))
        self.wait_until(lambda: self.backup.seq == 1, "copied the first asset")
        self.stop_server("primary")
        self.register(make_asset("AST-0002"))
        self.edit("AST-0001", 1, location="Moved during the outage")

        returned = self.start_server("primary")
        self.assertEqual(self.ids_on(returned), ["AST-0001", "AST-0002"])
        self.assertEqual(returned.assets["AST-0001"]["location"], "Moved during the outage")

        self.register(make_asset("AST-0003"))  # and new changes flow to the backup again
        self.wait_until(lambda: self.ids_on(self.backup) == ["AST-0001", "AST-0002", "AST-0003"], "copied AST-0003")

    def test_a_backup_that_was_down_is_brought_up_to_date(self):
        self.stop_server("backup")
        self.register(make_asset("AST-0001"))
        self.register(make_asset("AST-0002"))
        restarted = self.start_server("backup")
        self.wait_until(lambda: self.ids_on(restarted) == ["AST-0001", "AST-0002"], "caught the backup up")

    def test_a_retry_on_the_backup_of_a_change_the_primary_made_is_not_repeated(self):
        first = self.register(make_asset("AST-0001"), request_id="lost-reply")
        self.wait_until(lambda: self.backup.seq == 1, "copied the change")
        self.stop_server("primary")
        self.assertEqual(self.register(make_asset("AST-0001"), request_id="lost-reply"), first)
        self.assertEqual(self.backup.seq, 1)

    def test_clients_cannot_use_the_servers_own_actions(self):
        for action in ("STATE", "REPLICATE", "SYNC_STATUS"):
            reply = self.request({"action": action, "assets": [], "requests": {}, "seq": 99})
            self.assertEqual(reply["code"], common.UNAUTHENTICATED, action)
        self.assertEqual(self.backup.seq, 0)

    def test_a_client_reports_clearly_when_both_servers_are_down(self):
        self.stop_server("primary")
        self.stop_server("backup")
        with self.assertRaises(OSError):
            self.listed()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_query_client([])
        self.assertIn("Could not reach the server", output.getvalue())


class ChangeTest(ServerTestCase):
    """FR10 (soft deletion), FR11, NFR10, NFR11, NFR12."""

    def test_an_asset_can_be_edited_and_the_updated_record_is_returned(self):
        created = self.register(make_asset())["stored"]
        time.sleep(1.1)  # so updated_at visibly moves on
        reply = self.edit("AST-0001", 1, description="Laptop, new battery", location="Site B", status="not in use")
        self.assertEqual(reply["status"], "OK")
        stored = reply["stored"]
        self.assertEqual(
            (stored["description"], stored["location"], stored["status"], stored["version"]),
            ("Laptop, new battery", "Site B", "Not In Use", 2),
        )
        self.assertEqual(stored["created_at"], created["created_at"])
        self.assertGreater(stored["updated_at"], created["updated_at"])
        self.assertEqual(self.listed(), [stored])

    def test_edits_are_validated_like_registrations(self):
        self.register(make_asset())
        self.assertEqual(self.edit("AST-0001", 1, status="Lost")["context"]["field"], "status")
        self.assertEqual(self.edit("AST-0001", 1, description="")["context"]["field"], "description")
        reply = self.request({
            "action": "EDIT", "request_id": "id-edit", "asset_id": "AST-0001",
            "expected_version": 1, "changes": {"asset_id": "AST-9"},
        })
        self.assertEqual(reply["context"]["field"], "asset_id")
        self.assertEqual(self.edit("AST-0001", 1)["context"]["field"], "changes")
        self.assertEqual(self.listed()[0]["version"], 1)

    def test_an_edit_based_on_an_old_version_is_a_conflict(self):
        self.register(make_asset())
        self.edit("AST-0001", 1, location="Site B")
        reply = self.edit("AST-0001", 1, location="Site C")
        self.assertEqual(reply["code"], common.CONFLICT)
        self.assertEqual(reply["context"]["current_version"], 2)
        self.assertEqual(self.listed()[0]["location"], "Site B")

    def test_only_one_of_many_simultaneous_edits_of_a_version_wins(self):
        self.register(make_asset())
        replies = []
        threads = [
            threading.Thread(target=lambda n=n: replies.append(self.edit("AST-0001", 1, location=f"Room {n}")))
            for n in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(reply["status"] for reply in replies), ["ERROR"] * 19 + ["OK"])
        self.assertEqual(self.listed()[0]["version"], 2)

    def test_editing_or_deleting_an_unknown_asset_is_not_found(self):
        self.assertEqual(self.edit("AST-404", 1, location="X")["code"], common.NOT_FOUND)
        self.assertEqual(self.delete("AST-404", 1)["code"], common.NOT_FOUND)

    def test_a_deleted_asset_is_kept_marked_deleted_and_left_out_of_lists(self):
        self.register(make_asset("AST-0001"))
        self.register(make_asset("AST-0002"))
        reply = self.delete("AST-0001", 1)
        self.assertEqual(reply["deleted"], "AST-0001")
        self.assertTrue(reply["stored"]["deleted"])
        self.assertRegex(reply["stored"]["deleted_at"], r"Z$")
        self.assertEqual([asset["asset_id"] for asset in self.listed()], ["AST-0002"])
        with open(self.server.data_file, encoding="utf-8") as file:
            kept = {asset["asset_id"]: asset for asset in json.load(file)["assets"]}
        self.assertTrue(kept["AST-0001"]["deleted"])

    def test_a_deleted_asset_cannot_be_edited_and_its_id_is_not_reused(self):
        self.register(make_asset())
        self.delete("AST-0001", 1)
        self.assertEqual(self.edit("AST-0001", 2, location="X")["code"], common.NOT_FOUND)
        self.assertEqual(self.register(make_asset())["code"], common.DUPLICATE_ASSET)

    def test_a_delete_based_on_an_old_version_is_a_conflict(self):
        self.register(make_asset())
        self.edit("AST-0001", 1, location="Site B")
        self.assertEqual(self.delete("AST-0001", 1)["code"], common.CONFLICT)
        self.assertEqual(len(self.listed()), 1)

    def test_every_change_needs_a_request_id(self):
        reply = self.request({"action": "REGISTER", "asset": make_asset()})
        self.assertEqual(reply["context"]["field"], "request_id")
        self.assertEqual(self.listed(), [])

    def test_repeating_a_registration_stores_one_asset_and_returns_the_same_reply(self):
        first = self.register(make_asset(), request_id="retry-1")
        second = self.register(make_asset(), request_id="retry-1")
        self.assertEqual(first, second)  # not DUPLICATE_ASSET: it's the same request again
        self.assertEqual(len(self.listed()), 1)

    def test_repeating_an_edit_or_delete_does_not_repeat_the_change(self):
        self.register(make_asset())
        first = self.edit("AST-0001", 1, request_id="edit-1", location="Site B")
        self.assertEqual(self.edit("AST-0001", 1, request_id="edit-1", location="Site B"), first)
        self.assertEqual(self.listed()[0]["version"], 2)
        deleted = self.delete("AST-0001", 2, request_id="delete-1")
        self.assertEqual(self.delete("AST-0001", 2, request_id="delete-1"), deleted)

    def test_a_request_id_cannot_be_reused_for_a_different_request(self):
        self.register(make_asset("AST-0001"), request_id="same")
        reply = self.register(make_asset("AST-0002"), request_id="same")
        self.assertEqual((reply["code"], reply["context"]["field"]), (common.BAD_REQUEST, "request_id"))

    def test_request_ids_are_remembered_after_a_restart(self):
        first = self.register(make_asset(), request_id="before-restart")
        restarted = server.AssetServer(self.config)
        replay = restarted.process({
            "action": "REGISTER", "protocol_version": 1, "request_id": "before-restart",
            "asset": make_asset(), "token": self.token,
        })
        self.assertEqual(replay, first)

    def test_old_request_ids_are_eventually_forgotten(self):
        with mock.patch("server.MAX_REMEMBERED_REQUESTS", 3):
            for n in range(5):
                self.register(make_asset(f"AST-{n}"), request_id=f"r{n}")
        self.assertEqual(list(self.server.requests), ["r2", "r3", "r4"])

    def test_a_failed_save_of_an_edit_changes_nothing(self):
        self.register(make_asset())
        with mock.patch("server.json.dump", side_effect=OSError("disk full")):
            self.assertEqual(self.edit("AST-0001", 1, request_id="e", location="X")["code"], common.INTERNAL)
        self.assertEqual(self.listed()[0]["location"], "Site A")
        self.assertNotIn("e", self.server.requests)


if __name__ == "__main__":
    unittest.main()

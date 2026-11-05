"""
Automated tests for the Asset Management System.

    python -m unittest -v test_system

Each test starts real servers on spare ports, with their own temporary data files,
and talks to them over real sockets.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest

import contextlib
import io
from unittest import mock

import common
import receive
import send
import server


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
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as file:
        json.dump(config, file)
    return common.load_config(path)


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
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = temp.name
        self.config = make_config(self.folder)
        self.server = self.start_server("primary")

    def start_server(self, role: str) -> server.AssetServer:
        node = server.AssetServer(self.config, role)
        stop = threading.Event()
        thread = threading.Thread(target=node.serve, args=(stop,), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        self.wait_for_port(node.address)
        return node

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
        return common.send_request(request, self.config)

    def register(self, asset: dict) -> dict:
        return self.request({"action": "REGISTER", "asset": asset})

    def raw_exchange(self, payload: bytes) -> dict:
        """Send raw bytes to the primary and return its decoded reply."""
        with socket.create_connection(self.server.address, timeout=5) as sock:
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
        body = json.dumps({"action": "LIST", "protocol_version": 1}).encode("utf-8")
        reply = self.raw_exchange(self.frame(body))
        self.assertEqual(reply, {"status": "OK", "assets": []})

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
        reply = self.request({"action": "REGISTER", "asset": "laptop"})
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
        answers = iter(["AST-0009", "Projector", "AV", "Room 2", "3", "2"])
        prompts = []

        def fake_input(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        output = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(output):
            send.main(self.config)
        for word in ("Asset ID", "Description", "Category", "Location", "Status"):
            self.assertTrue(any(word in prompt for prompt in prompts), word)
        self.assertIn("Registered AST-0009", output.getvalue())
        self.assertIn("Please type a number", output.getvalue())  # "3" was not a valid choice
        self.assertEqual(self.request({"action": "LIST"})["assets"][0]["status"], "Not In Use")

    def test_the_query_client_says_when_there_are_no_assets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            receive.main(self.config)
        self.assertIn("No assets registered yet.", output.getvalue())

    def test_the_query_client_shows_every_asset(self):
        self.register(make_asset("AST-0001"))
        self.register(make_asset("AST-0002"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            receive.main(self.config)
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


if __name__ == "__main__":
    unittest.main()

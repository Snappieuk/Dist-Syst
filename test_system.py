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

import common
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
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as file:
        json.dump(config, file)
    return common.load_config(path)


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
        reply = self.request({"action": "REGISTER", "asset": {"description": "Laptop"}})
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


if __name__ == "__main__":
    unittest.main()

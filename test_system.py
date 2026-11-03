"""
Tests for the Asset Management System.

    python -m unittest -v test_system

These run a real server on a background thread and talk to it over real TCP
sockets, so they test the whole path a client uses. The server uses a temporary
data file and a spare port, so your real assets.json is never touched.
"""

import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest

import common
import server


def make_asset(asset_id="AST-1", **changes) -> dict:
    """Build a valid asset, optionally changing some of its fields."""
    asset = {
        "asset_id": asset_id,
        "description": "Dell laptop",
        "category": "IT Equipment",
        "location": "Site A",
        "status": "In Use",
    }
    asset.update(changes)
    return asset


def register(asset) -> dict:
    return common.send_request({"action": "REGISTER", "asset": asset})


def list_ids() -> list:
    reply = common.send_request({"action": "LIST"})
    return [asset["asset_id"] for asset in reply["assets"]]


def send_raw(data: bytes) -> dict:
    """Send exact bytes to the server, as a faulty client might, and read the reply."""
    with socket.create_connection((common.HOST, common.PORT), timeout=5) as sock:
        sock.sendall(data)
        return common.receive_message(sock)


def with_header(payload: bytes) -> bytes:
    """Add the 4-byte length header to some bytes."""
    return len(payload).to_bytes(4, "big") + payload


class SystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Port 0 asks the operating system for any free port.
        listener = server.create_listening_socket(common.HOST, 0)
        common.PORT = listener.getsockname()[1]
        threading.Thread(target=server.run_server, args=(listener,), daemon=True).start()

    def setUp(self):
        # Every test starts with no assets and its own temporary data file.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        server.ASSETS_FILE = os.path.join(self.temp_dir.name, "assets.json")
        server.assets.clear()

    # ---- Functional requirements: register and retrieve ----

    def test_list_starts_empty(self):
        self.assertEqual(list_ids(), [])

    def test_registered_asset_can_be_retrieved_with_all_fields(self):
        self.assertEqual(register(make_asset())["status"], "OK")

        stored = common.send_request({"action": "LIST"})["assets"][0]
        self.assertEqual(
            set(stored),
            {"asset_id", "description", "category", "location", "status", "date_registered"},
        )
        self.assertEqual(stored["description"], "Dell laptop")

    def test_server_sets_the_date_and_drops_extra_fields(self):
        reply = register(make_asset(date_registered="1999-01-01", extra="x"))
        self.assertNotEqual(reply["stored"]["date_registered"], "1999-01-01")
        self.assertNotIn("extra", reply["stored"])

    # ---- Validation ----

    def test_invalid_assets_are_rejected(self):
        missing_field = make_asset()
        del missing_field["location"]
        invalid_assets = {
            "not an object": [1, 2],
            "missing field": missing_field,
            "not a string": make_asset(category=7),
            "empty": make_asset(status=""),
            "only spaces": make_asset(status="   "),
        }
        for name, asset in invalid_assets.items():
            with self.subTest(name):
                self.assertEqual(register(asset)["code"], common.BAD_REQUEST)
        self.assertEqual(list_ids(), [])

    def test_duplicate_id_is_rejected(self):
        register(make_asset())
        self.assertEqual(register(make_asset())["code"], common.DUPLICATE_ID)
        self.assertEqual(list_ids(), ["AST-1"])

    def test_unknown_action_is_rejected(self):
        reply = common.send_request({"action": "EXPLODE"})
        self.assertEqual(reply["code"], common.BAD_REQUEST)

    # ---- Robustness: bad clients must not affect the server ----

    def test_bad_messages_get_a_clear_error(self):
        bad_messages = {
            "invalid JSON": with_header(b"not json"),
            "invalid text": with_header(b"\xff\xfe"),
            "JSON that is not an object": with_header(b"[1, 2]"),
            "very deeply nested JSON": with_header(b"[" * 500000),
        }
        for name, data in bad_messages.items():
            with self.subTest(name):
                self.assertEqual(send_raw(data)["code"], common.BAD_REQUEST)

    def test_claiming_a_huge_message_is_rejected(self):
        # A header claiming almost 4 GB, with no data following it.
        reply = send_raw((2 ** 32 - 1).to_bytes(4, "big"))
        self.assertEqual(reply["code"], common.TOO_LARGE)

    def test_client_refuses_to_send_a_huge_message(self):
        with self.assertRaises(common.MessageTooLargeError):
            register(make_asset(description="x" * (common.MAX_MESSAGE_BYTES + 1)))

    def test_server_survives_a_client_vanishing(self):
        def vanish(data: bytes) -> None:
            """Send some bytes, then drop the connection like a crashed client."""
            sock = socket.create_connection((common.HOST, common.PORT))
            sock.sendall(data)
            time.sleep(0.1)  # let the server start working on the request

            # A linger time of zero makes close() reset the connection abruptly,
            # instead of closing it politely. This used to crash the server.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()

        vanish(b"\x00")  # part-way through a message
        vanish(with_header(b'{"action": "LIST"}'))  # before reading the reply
        time.sleep(0.1)

        self.assertEqual(register(make_asset())["status"], "OK")

    def test_idle_client_does_not_block_others_and_is_dropped(self):
        original_timeout = server.CLIENT_TIMEOUT_SECONDS
        server.CLIENT_TIMEOUT_SECONDS = 0.5
        self.addCleanup(setattr, server, "CLIENT_TIMEOUT_SECONDS", original_timeout)

        with socket.create_connection((common.HOST, common.PORT), timeout=5) as idle:
            # Another client is served while this one sits idle.
            self.assertEqual(register(make_asset())["status"], "OK")

            # The server then closes the idle connection, with no reply.
            try:
                self.assertEqual(idle.recv(1), b"")
            except ConnectionError:
                pass  # some systems report the close this way instead

    def test_client_gets_a_clear_error_if_the_server_is_down(self):
        # Find a port with nothing listening on it.
        with socket.socket() as spare:
            spare.bind((common.HOST, 0))
            unused_port = spare.getsockname()[1]

        original_port = common.PORT
        common.PORT = unused_port
        self.addCleanup(setattr, common, "PORT", original_port)

        with self.assertRaisesRegex(ConnectionError, "server.py"):
            common.send_request({"action": "LIST"})

    # ---- Persistence and ACID ----

    def test_assets_are_on_disk_before_the_reply(self):
        register(make_asset())
        with open(server.ASSETS_FILE, encoding="utf-8") as file:
            self.assertEqual(json.load(file)[0]["asset_id"], "AST-1")

    def test_assets_are_loaded_after_a_restart(self):
        register(make_asset())

        # What a restart does: memory is lost, then the file is read.
        server.assets.clear()
        server.assets.extend(server.load_assets())

        self.assertEqual(list_ids(), ["AST-1"])
        self.assertEqual(register(make_asset())["code"], common.DUPLICATE_ID)

    def test_failed_save_leaves_nothing_behind(self):
        register(make_asset("AST-1"))

        # A folder in place of the temporary file makes the next save fail.
        blocker = server.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.assertEqual(register(make_asset("AST-2"))["code"], common.INTERNAL)
        os.rmdir(blocker)

        # Neither memory nor the file has the failed asset, and the file is intact.
        self.assertEqual(list_ids(), ["AST-1"])
        self.assertEqual(len(server.load_assets()), 1)

        # The same asset works once the problem has gone.
        self.assertEqual(register(make_asset("AST-2"))["status"], "OK")

    def test_corrupt_file_is_reported_not_overwritten(self):
        with open(server.ASSETS_FILE, "w", encoding="utf-8") as file:
            file.write("{corrupt")
        with self.assertRaises(ValueError):
            server.load_assets()

    # ---- Non-functional requirement: concurrency ----

    def run_at_once(self, assets: list) -> list:
        """Register all the assets at the same time, each from its own thread."""
        replies = []
        threads = [
            threading.Thread(target=lambda a=asset: replies.append(register(a)))
            for asset in assets
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return replies

    def test_50_concurrent_clients_lose_nothing(self):
        replies = self.run_at_once([make_asset(f"AST-{n}") for n in range(50)])

        self.assertEqual(len(replies), 50)
        self.assertTrue(all(reply["status"] == "OK" for reply in replies))
        self.assertEqual(len(list_ids()), 50)
        self.assertEqual(len(server.load_assets()), 50)

    def test_only_one_of_20_clients_racing_for_an_id_wins(self):
        # The gap between checking an ID is free and storing it is normally too
        # short for a race to happen by chance, so this test would pass even with
        # the lock removed. This list holds that gap open: it pauses each time the
        # duplicate check finishes looking through it, so without the lock every
        # client would see the ID as free.
        class SlowList(list):
            def __iter__(self):
                yield from super().__iter__()
                time.sleep(0.01)

        original_assets = server.assets
        server.assets = SlowList()
        self.addCleanup(setattr, server, "assets", original_assets)

        replies = self.run_at_once([make_asset("RACE") for _ in range(20)])

        self.assertEqual(sum(reply["status"] == "OK" for reply in replies), 1)
        self.assertEqual(list_ids(), ["RACE"])


if __name__ == "__main__":
    unittest.main()

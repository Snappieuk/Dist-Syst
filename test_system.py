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
import subprocess
import sys
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
    with socket.create_connection(common.SERVERS[0], timeout=5) as sock:
        sock.sendall(data)
        return common.receive_message(sock)


def with_header(payload: bytes) -> bytes:
    """Add the 4-byte length header to some bytes."""
    return len(payload).to_bytes(4, "big") + payload


def unused_address() -> tuple:
    """Find an address with nothing listening on it."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()


class SystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Port 0 asks the operating system for any free port.
        listener = server.create_listening_socket("127.0.0.1", 0)
        common.SERVERS = [listener.getsockname()]
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
            {"asset_id", "description", "category", "location", "status", "date_registered", "tags"},
        )
        self.assertEqual(stored["description"], "Dell laptop")

    def test_server_generates_tags_and_ignores_any_sent_by_the_client(self):
        reply = register(make_asset(description="The Dell laptop", tags=["client tag"]))
        tags = reply["stored"]["tags"]
        self.assertIn("ast-1", tags)
        self.assertIn("dell", tags)
        self.assertNotIn("the", tags)  # a stop word
        self.assertNotIn("client tag", tags)

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
            sock = socket.create_connection(common.SERVERS[0])
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

        with socket.create_connection(common.SERVERS[0], timeout=5) as idle:
            # Another client is served while this one sits idle.
            self.assertEqual(register(make_asset())["status"], "OK")

            # The server then closes the idle connection, with no reply.
            try:
                self.assertEqual(idle.recv(1), b"")
            except ConnectionError:
                pass  # some systems report the close this way instead

    def test_client_gets_a_clear_error_if_every_server_is_down(self):
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [unused_address(), unused_address()]

        with self.assertRaisesRegex(ConnectionError, "server.py"):
            common.send_request({"action": "LIST"})

    def test_client_fails_over_to_the_backup(self):
        # The first server in the list is down, and the second one is running.
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [unused_address(), common.SERVERS[0]]

        self.assertEqual(register(make_asset())["status"], "OK")

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

    # ---- Merging assets from the other server ----

    def test_merge_copies_only_what_is_missing(self):
        register(make_asset("AST-1"))
        stored = common.send_request({"action": "LIST"})["assets"]

        changed = server.merge_assets(stored + [make_asset("AST-2")], prefer_peer=False)

        self.assertEqual(changed, 1)
        self.assertEqual(list_ids(), ["AST-1", "AST-2"])
        self.assertEqual(len(server.load_assets()), 2)

    def test_primary_keeps_its_own_copy_in_a_conflict(self):
        server.merge_assets([make_asset(status="Primary")], prefer_peer=False)
        changed = server.merge_assets([make_asset(status="Backup")], prefer_peer=False)

        self.assertEqual(changed, 0)
        self.assertEqual(server.assets[0]["status"], "Primary")

    def test_backup_gives_way_to_the_primary_in_a_conflict(self):
        server.merge_assets([make_asset(status="Backup")], prefer_peer=False)
        changed = server.merge_assets([make_asset(status="Primary")], prefer_peer=True)

        self.assertEqual(changed, 1)
        self.assertEqual(server.assets[0]["status"], "Primary")
        self.assertEqual(server.load_assets()[0]["status"], "Primary")

    def test_failed_merge_leaves_nothing_behind(self):
        blocker = server.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)

        with self.assertRaises(OSError):
            server.merge_assets([make_asset()], prefer_peer=False)
        self.assertEqual(server.assets, [])

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


class BackupTest(unittest.TestCase):
    """Runs the primary and backup as real, separate programs, and stops one."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_path = temp_dir.name

        self.primary = unused_address()
        self.backup = unused_address()
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [self.primary, self.backup]

    def start_server(self, *options) -> subprocess.Popen:
        """Start server.py as its own process, using the test's addresses and files."""
        code = (
            "import common\n"
            f"common.SERVERS = {common.SERVERS!r}\n"
            "import server\n"
            f"server.ASSETS_FILE = {os.path.join(self.temp_path, 'primary.json')!r}\n"
            f"server.BACKUP_ASSETS_FILE = {os.path.join(self.temp_path, 'backup.json')!r}\n"
            "server.main()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, *options],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def wait_until_has(self, address: tuple, asset_id: str) -> None:
        """Wait for the server at `address` to have an asset, failing after a while."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                reply = common.request_from(address, {"action": "LIST"})
                if asset_id in [asset["asset_id"] for asset in reply["assets"]]:
                    return
            except OSError:
                pass  # the server may still be starting
            time.sleep(0.2)
        self.fail(f"{address} never received {asset_id}")

    def test_system_keeps_working_when_the_primary_fails(self):
        primary = self.start_server()
        self.start_server("--backup")
        time.sleep(1)  # give both a moment to start listening

        # An asset registered with the primary reaches the backup.
        self.assertEqual(register(make_asset("BEFORE"))["status"], "OK")
        self.wait_until_has(self.backup, "BEFORE")

        # With the primary gone, clients carry on using the backup, with nothing lost.
        primary.kill()
        primary.wait()
        self.assertEqual(register(make_asset("DURING"))["status"], "OK")
        self.assertEqual(sorted(list_ids()), ["BEFORE", "DURING"])

        # The restarted primary catches up, and is used by clients again.
        self.start_server()
        self.wait_until_has(self.primary, "DURING")
        self.wait_until_has(self.primary, "BEFORE")
        self.assertEqual(register(make_asset("AFTER"))["status"], "OK")
        self.wait_until_has(self.primary, "AFTER")
        self.wait_until_has(self.backup, "AFTER")


if __name__ == "__main__":
    unittest.main()

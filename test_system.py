"""
Tests for the Asset Management System.

    python -m unittest -v test_system

These run real servers and talk to them over real TCP sockets, so they test the
whole path a client uses. SystemTest runs one server on a background thread, and
BackupTest runs the primary and backup as separate programs. Every server uses
temporary data files and spare ports, so your real assets.json is never touched.
"""

import contextlib
import io
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
from unittest import mock

import common
import manage
import receive
import server


def make_asset(description="Dell laptop", **changes) -> dict:
    """Build what a client sends to register an asset, optionally changing fields."""
    asset = {
        "description": description,
        "category": "IT Equipment",
        "location": "Site A",
        "status": "In Use",
    }
    asset.update(changes)
    return asset


def make_record(asset_id="AST-P000001", description=None, **changes) -> dict:
    """Build a record as a server stores it. Each ID gets its own description by
    default, so that different records count as different items."""
    return make_asset(description or f"Item {asset_id}", asset_id=asset_id, **changes)


def register(asset) -> dict:
    return common.send_request(common.new_register_request(asset))


def list_assets() -> list:
    return common.send_request({"action": "LIST"})["assets"]


def list_ids() -> list:
    return [asset["asset_id"] for asset in list_assets()]


def list_descriptions() -> list:
    return [asset["description"] for asset in list_assets()]


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


def start_fake_server(test: unittest.TestCase, reply) -> tuple:
    """Run a server that gives every request the same reply, and return its address.

    A reply of None reads the request and then drops the connection, as if the
    reply were lost on its way back.
    """
    listener = server.create_listening_socket("127.0.0.1", 0)
    test.addCleanup(listener.close)

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # the test has finished and closed the listener
            with conn:
                common.receive_message(conn)
                if reply is not None:
                    common.send_message(conn, reply)

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()


class NodeTestCase(unittest.TestCase):
    """Starts each test with an empty server and its own temporary data file."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        server.ASSETS_FILE = os.path.join(self.temp_dir.name, "assets.json")
        server.assets.clear()
        server.deleted_ids.clear()
        server.ID_PREFIX = "AST-P"


class SystemTest(NodeTestCase):
    @classmethod
    def setUpClass(cls):
        # Port 0 asks the operating system for any free port.
        listener = server.create_listening_socket("127.0.0.1", 0)
        common.SERVERS = [listener.getsockname()]
        threading.Thread(target=server.run_server, args=(listener,), daemon=True).start()

    # ---- Functional requirements: register and retrieve ----

    def test_list_starts_empty(self):
        self.assertEqual(list_ids(), [])

    def test_registered_asset_can_be_retrieved_with_all_fields(self):
        self.assertEqual(register(make_asset())["status"], "OK")

        stored = list_assets()[0]
        self.assertEqual(
            set(stored),
            {
                "asset_id", "description", "category", "location", "status",
                "date_registered", "tags", "version", "request_id",
            },
        )
        self.assertEqual(stored["description"], "Dell laptop")

    def test_server_generates_tags_and_ignores_any_sent_by_the_client(self):
        reply = register(make_asset(description="The Dell laptop", tags=["client tag"]))
        tags = reply["stored"]["tags"]
        self.assertIn("ast-p000001", tags)
        self.assertIn("dell", tags)
        self.assertNotIn("the", tags)  # a stop word
        self.assertNotIn("client tag", tags)

    def test_server_sets_the_date_and_drops_extra_fields(self):
        reply = register(make_asset(date_registered="1999-01-01", extra="x"))
        self.assertNotEqual(reply["stored"]["date_registered"], "1999-01-01")
        self.assertNotIn("extra", reply["stored"])

    def test_tags_work_in_any_language(self):
        tags = common.generate_asset_tags(make_asset(description="Büro 笔记本电脑 हिन्दी"))
        self.assertIn("büro", tags)
        self.assertIn("笔记本电脑", tags)
        self.assertIn("हिन्दी", tags)

    def test_tags_replace_punctuation(self):
        tags = common.generate_asset_tags(make_record("AST_0001", location="Site A - IT Store"))
        self.assertIn("ast-0001", tags)
        self.assertIn("site a it store", tags)

    def test_query_client_shows_status_and_tags_for_assets_saved_without_tags(self):
        server.assets.append(make_record(description="Dell laptop"))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            receive.main()

        self.assertIn("In Use", output.getvalue())
        self.assertIn("dell", output.getvalue())

    def test_a_just_registered_asset_is_shown_even_if_the_list_lacks_it(self):
        listed, stored = make_record("AST-1"), make_record("AST-2")
        self.assertEqual(common.include_asset([listed], stored), [listed, stored])
        self.assertEqual(common.include_asset([listed, stored], stored), [listed, stored])

    # ---- Asset IDs: created by the server, and checked before being issued ----

    def test_server_creates_the_id_and_ignores_any_sent_by_the_client(self):
        reply = register(make_asset(asset_id="MY-ID"))
        self.assertEqual(reply["stored"]["asset_id"], "AST-P000001")

    def test_ids_count_up(self):
        self.assertEqual(register(make_asset("One"))["stored"]["asset_id"], "AST-P000001")
        self.assertEqual(register(make_asset("Two"))["stored"]["asset_id"], "AST-P000002")

    def test_an_id_already_in_use_is_never_issued(self):
        # The next ID in sequence is taken, so it must be passed over.
        server.assets.append(make_record("AST-P000002"))
        self.assertEqual(register(make_asset())["stored"]["asset_id"], "AST-P000003")

    def test_a_deleted_id_is_never_issued_again(self):
        first = register(make_asset("One"))["stored"]["asset_id"]
        second = register(make_asset("Two"))["stored"]["asset_id"]
        common.send_request({"action": "DELETE", "asset_id": second})

        third = register(make_asset("Three"))["stored"]["asset_id"]

        self.assertNotIn(third, (first, second))

    def test_each_server_issues_ids_from_its_own_range(self):
        server.ID_PREFIX = "AST-B"
        self.assertEqual(register(make_asset())["stored"]["asset_id"], "AST-B000001")

    # ---- Duplicates ----

    def test_invalid_request_ids_are_rejected(self):
        for request_id in (7, "", "   ", "x" * (server.MAX_FIELD_LENGTH + 1)):
            with self.subTest(request_id=request_id):
                reply = common.send_request({"action": "REGISTER", "request_id": request_id, "asset": make_asset()})
                self.assertEqual(reply["code"], common.BAD_REQUEST)
        self.assertEqual(list_ids(), [])

    def test_repeating_a_registration_stores_one_asset(self):
        request = common.new_register_request(make_asset())

        first = common.send_request(request)
        second = common.send_request(request)

        self.assertEqual(first, second)
        self.assertEqual(len(list_ids()), 1)

    def test_repeating_a_registration_after_a_status_change_keeps_the_change(self):
        request = common.new_register_request(make_asset())
        asset_id = common.send_request(request)["stored"]["asset_id"]
        common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE})

        reply = common.send_request(request)

        self.assertEqual(reply["stored"]["status"], common.NOT_IN_USE)
        self.assertEqual(len(list_ids()), 1)

    def test_reusing_a_request_id_for_a_different_asset_is_rejected(self):
        request = common.new_register_request(make_asset())
        common.send_request(request)

        request["asset"] = make_asset(description="Something else")

        self.assertEqual(common.send_request(request)["code"], common.BAD_REQUEST)
        self.assertEqual(list_descriptions(), ["Dell laptop"])

    def test_repeating_a_registration_cannot_bring_back_a_deleted_asset(self):
        request = common.new_register_request(make_asset())
        asset_id = common.send_request(request)["stored"]["asset_id"]
        common.send_request({"action": "DELETE", "asset_id": asset_id})

        self.assertEqual(common.send_request(request)["code"], common.BAD_REQUEST)
        self.assertEqual(list_ids(), [])

    # ---- Changing an asset's status ----

    def test_an_asset_can_be_marked_not_in_use_and_back(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        for status, tag in ((common.NOT_IN_USE, "not in use"), (common.IN_USE, "in use")):
            reply = common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": status})
            self.assertEqual(reply["status"], "OK")

            stored = list_assets()[0]
            self.assertEqual(stored["status"], status)
            self.assertIn(tag, stored["tags"])  # the tags follow the status

        self.assertEqual(stored["version"], 3)
        self.assertEqual(server.load_assets()[0][0]["status"], common.IN_USE)

    def test_setting_the_same_status_changes_nothing(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": common.IN_USE})

        self.assertEqual(list_assets()[0]["version"], 1)

    def test_status_of_an_unknown_asset_is_not_found(self):
        reply = common.send_request({"action": "SET_STATUS", "asset_id": "AST-X", "status": common.NOT_IN_USE})
        self.assertEqual(reply["code"], common.NOT_FOUND)

    def test_invalid_status_requests_are_rejected(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        requests = (
            {"asset_id": asset_id, "status": ""},
            {"asset_id": asset_id, "status": 7},
            {"asset_id": asset_id},
            {"status": common.NOT_IN_USE},
        )
        for request in requests:
            with self.subTest(request=request):
                reply = common.send_request({"action": "SET_STATUS", **request})
                self.assertEqual(reply["code"], common.BAD_REQUEST)

    def test_failed_status_save_leaves_nothing_behind(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        blocker = server.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)
        reply = common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE})

        self.assertEqual(reply["code"], common.INTERNAL)
        self.assertEqual(list_assets()[0]["status"], common.IN_USE)

    # ---- Simultaneous changes ----

    def run_at_once(self, requests: list) -> list:
        """Send all the requests at the same time, each from its own thread."""
        replies = []
        threads = [
            threading.Thread(target=lambda r=request: replies.append(common.send_request(r)))
            for request in requests
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return replies

    def test_50_concurrent_clients_lose_nothing_and_get_unique_ids(self):
        replies = self.run_at_once([common.new_register_request(make_asset(f"Asset {n}")) for n in range(50)])

        self.assertEqual(len(replies), 50)
        self.assertTrue(all(reply["status"] == "OK" for reply in replies))
        self.assertEqual(len({reply["stored"]["asset_id"] for reply in replies}), 50)
        self.assertEqual(len(list_ids()), 50)
        self.assertEqual(len(server.load_assets()[0]), 50)

    def use_slow_list(self) -> None:
        """Hold open the gap between checking the data and changing it.

        That gap is normally too short for a race to happen by chance, so the tests
        below would pass even with the lock removed. This list pauses each time a
        check finishes looking through it, so without the lock, every client would
        see that there is nothing in the way.
        """
        class SlowList(list):
            def __iter__(self):
                yield from super().__iter__()
                time.sleep(0.01)

        original_assets = server.assets
        server.assets = SlowList()
        self.addCleanup(setattr, server, "assets", original_assets)

    def test_20_clients_racing_with_one_registration_store_one_asset(self):
        self.use_slow_list()

        request = common.new_register_request(make_asset())
        replies = self.run_at_once([request] * 20)

        self.assertTrue(all(reply["status"] == "OK" for reply in replies))
        self.assertEqual(len(list_ids()), 1)

    # ---- Deleting an asset ----

    def test_a_deleted_asset_is_gone_even_after_a_restart(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        kept_id = register(make_asset("Kept"))["stored"]["asset_id"]

        reply = common.send_request({"action": "DELETE", "asset_id": asset_id})
        self.assertEqual(reply["deleted"], asset_id)
        self.assertEqual(list_ids(), [kept_id])

        loaded_assets, loaded_deleted = server.load_assets()
        self.assertEqual([asset["asset_id"] for asset in loaded_assets], [kept_id])
        self.assertIn(asset_id, loaded_deleted)

    def test_deleting_twice_is_safe_and_unknown_ids_are_not_found(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        request = {"action": "DELETE", "asset_id": asset_id}

        self.assertEqual(common.send_request(request)["status"], "OK")
        self.assertEqual(common.send_request(request)["status"], "OK")

        reply = common.send_request({"action": "DELETE", "asset_id": "AST-X"})
        self.assertEqual(reply["code"], common.NOT_FOUND)

    def test_status_of_a_deleted_asset_is_not_found(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        common.send_request({"action": "DELETE", "asset_id": asset_id})

        reply = common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE})
        self.assertEqual(reply["code"], common.NOT_FOUND)

    def test_failed_delete_save_leaves_nothing_behind(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        blocker = server.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)
        reply = common.send_request({"action": "DELETE", "asset_id": asset_id})

        self.assertEqual(reply["code"], common.INTERNAL)
        self.assertEqual(list_ids(), [asset_id])
        self.assertEqual(server.deleted_ids, {})

    # ---- The management client ----

    def run_manage(self, *answers) -> str:
        """Run manage.py, giving it these answers to its prompts, and return its output."""
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(output):
            manage.main()
        return output.getvalue()

    def test_manage_client_changes_status(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        self.run_manage(asset_id, "1")
        self.assertEqual(list_assets()[0]["status"], common.NOT_IN_USE)

        self.run_manage(asset_id, "2")
        self.assertEqual(list_assets()[0]["status"], common.IN_USE)

    def test_manage_client_deletes_only_after_confirmation(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        self.assertIn("Nothing was deleted", self.run_manage(asset_id, "3", "no"))
        self.assertEqual(list_ids(), [asset_id])

        self.run_manage(asset_id, "3", "DELETE")
        self.assertEqual(list_ids(), [])

    def test_manage_client_reports_an_unknown_id(self):
        self.assertIn("No asset has the ID", self.run_manage("AST-X"))

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
            "too long": make_asset(description="x" * (server.MAX_FIELD_LENGTH + 1)),
        }
        for name, asset in invalid_assets.items():
            with self.subTest(name):
                self.assertEqual(register(asset)["code"], common.BAD_REQUEST)
        self.assertEqual(list_ids(), [])

    def test_largest_allowed_asset_gets_a_reply_and_can_be_listed(self):
        longest = "€" * server.MAX_FIELD_LENGTH
        asset = make_asset(longest, category=longest, location=longest, status=longest)

        self.assertEqual(register(asset)["status"], "OK")
        self.assertEqual(list_descriptions(), [longest])

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

    def test_a_list_too_large_to_send_gets_a_clear_error(self):
        server.assets.append(make_record(description="x" * (common.MAX_MESSAGE_BYTES + 1)))

        reply = common.send_request({"action": "LIST"})

        self.assertEqual(reply["code"], common.TOO_LARGE)

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

    # ---- Client failover ----

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

    def test_client_tries_the_backup_if_the_primary_replies_internal(self):
        failing = start_fake_server(self, {"status": "ERROR", "code": common.INTERNAL, "detail": "disk full"})
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [failing, common.SERVERS[0]]

        self.assertEqual(register(make_asset())["status"], "OK")

    def test_client_gets_the_error_if_every_server_replies_internal(self):
        failing = start_fake_server(self, {"status": "ERROR", "code": common.INTERNAL, "detail": "disk full"})
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [failing, failing]

        self.assertEqual(register(make_asset())["code"], common.INTERNAL)

    def test_client_tries_the_other_server_if_an_asset_is_not_found(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        behind = start_fake_server(self, {"status": "ERROR", "code": common.NOT_FOUND, "detail": "no such asset"})
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [behind, common.SERVERS[0]]

        self.assertEqual(common.send_request({"action": "DELETE", "asset_id": asset_id})["status"], "OK")
        self.assertEqual(common.send_request({"action": "DELETE", "asset_id": "AST-X"})["code"], common.NOT_FOUND)

    def test_retry_after_a_lost_reply_stores_one_asset(self):
        request = common.new_register_request(make_asset())
        first = common.send_request(request)

        # The reply is lost, so the client moves on and tries the same request again.
        lost_reply = start_fake_server(self, None)
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [lost_reply, common.SERVERS[0]]

        self.assertEqual(common.send_request(request), first)
        self.assertEqual(len(list_ids()), 1)

    # ---- Persistence and ACID ----

    def test_assets_are_on_disk_before_the_reply(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        with open(server.ASSETS_FILE, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["assets"][0]["asset_id"], asset_id)

    def test_assets_are_loaded_after_a_restart(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        # What a restart does: memory is lost, then the file is read.
        server.assets.clear()
        server.assets.extend(server.load_assets()[0])

        self.assertEqual(list_ids(), [asset_id])

    def test_a_file_from_before_delete_was_added_still_loads(self):
        with open(server.ASSETS_FILE, "w", encoding="utf-8") as file:
            json.dump([make_record()], file)

        self.assertEqual(server.load_assets(), ([make_record()], {}))

    def test_failed_save_leaves_nothing_behind(self):
        register(make_asset("First"))

        # A folder in place of the temporary file makes the next save fail.
        blocker = server.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.assertEqual(register(make_asset("Second"))["code"], common.INTERNAL)
        os.rmdir(blocker)

        # Neither memory nor the file has the failed asset, and the file is intact.
        self.assertEqual(list_descriptions(), ["First"])
        self.assertEqual(len(server.load_assets()[0]), 1)

        # The same asset works once the problem has gone.
        self.assertEqual(register(make_asset("Second"))["status"], "OK")

    def test_corrupt_file_is_reported_not_overwritten(self):
        for content in ("{corrupt", "7", '{"assets": []}'):
            with self.subTest(content):
                with open(server.ASSETS_FILE, "w", encoding="utf-8") as file:
                    file.write(content)
                with self.assertRaises(ValueError):
                    server.load_assets()


class MergeTest(NodeTestCase):
    setUpClass = SystemTest.setUpClass

    """Merging the other server's assets into this one's."""

    def test_merge_copies_only_what_is_missing(self):
        server.merge_assets([make_record("AST-1")], prefer_peer=False)

        changed = server.merge_assets([make_record("AST-1"), make_record("AST-2")], prefer_peer=False)

        self.assertEqual(changed, 1)
        self.assertEqual(list_ids(), ["AST-1", "AST-2"])
        self.assertEqual(len(server.load_assets()[0]), 2)

    def test_primary_keeps_its_own_copy_in_a_conflict(self):
        server.merge_assets([make_record(status="Primary")], prefer_peer=False)
        changed = server.merge_assets([make_record(status="Backup")], prefer_peer=False)

        self.assertEqual(changed, 0)
        self.assertEqual(server.assets[0]["status"], "Primary")

    def test_backup_gives_way_to_the_primary_in_a_conflict(self):
        server.merge_assets([make_record(status="Backup")], prefer_peer=False)
        changed = server.merge_assets([make_record(status="Primary")], prefer_peer=True)

        self.assertEqual(changed, 1)
        self.assertEqual(server.assets[0]["status"], "Primary")

    def test_failed_merge_leaves_nothing_behind(self):
        blocker = server.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)

        with self.assertRaises(OSError):
            server.merge_assets([make_record()], prefer_peer=False)
        self.assertEqual(server.assets, [])

    def test_sync_skips_invalid_records_and_copies_the_rest(self):
        records = [make_record("GOOD"), {"no_id": 1}, {"asset_id": "JUNK"}, [1], make_record("BAD", tags=7)]
        peer = start_fake_server(self, {"status": "OK", "assets": records, "deleted": {}})

        self.assertEqual(server.sync_once(peer, prefer_peer=False), (1, 4))
        self.assertEqual(list_ids(), ["GOOD"])

    def test_sync_treats_an_error_reply_as_a_failure(self):
        register(make_asset())
        peer = start_fake_server(self, {"status": "ERROR", "code": common.TOO_LARGE, "detail": "too large"})

        with self.assertRaisesRegex(ValueError, "too large"):
            server.sync_once(peer, prefer_peer=True)
        self.assertEqual(len(list_ids()), 1)

    def test_merge_keeps_the_newest_version_on_either_server(self):
        older, newer = make_record(status="Old", version=1), make_record(status="New", version=2)

        for prefer_peer in (False, True):
            with self.subTest(prefer_peer=prefer_peer):
                server.assets.clear()
                server.merge_assets([older], prefer_peer=False)

                # The newer copy is taken, and an older one does not undo that.
                self.assertEqual(server.merge_assets([newer], prefer_peer), 1)
                self.assertEqual(server.merge_assets([older], prefer_peer), 0)
                self.assertEqual(server.assets[0]["status"], "New")

    def test_merge_removes_a_deleted_asset_and_keeps_it_deleted(self):
        server.merge_assets([make_record("AST-1"), make_record("AST-2")], prefer_peer=False)

        # The other server deleted AST-1, and this is saved.
        self.assertEqual(server.merge_assets([make_record("AST-2")], False, {"AST-1": None}), 1)
        self.assertEqual(list_ids(), ["AST-2"])
        self.assertIn("AST-1", server.load_assets()[1])

        # Even a newer copy from somewhere cannot bring it back.
        self.assertEqual(server.merge_assets([make_record("AST-1", version=9)], True), 0)
        self.assertEqual(list_ids(), ["AST-2"])


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

    def wait_until_has(self, address: tuple, description: str) -> None:
        """Wait for the server at `address` to have an asset, failing after a while."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                reply = common.request_from(address, {"action": "LIST"})
                if description in [asset["description"] for asset in reply["assets"]]:
                    return
            except OSError:
                pass  # the server may still be starting
            time.sleep(0.2)
        self.fail(f"{address} never received {description}")

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
        self.assertEqual(sorted(list_descriptions()), ["BEFORE", "DURING"])

        # The restarted primary catches up, and is used by clients again.
        self.start_server()
        self.wait_until_has(self.primary, "DURING")
        self.wait_until_has(self.primary, "BEFORE")
        self.assertEqual(register(make_asset("AFTER"))["status"], "OK")
        self.wait_until_has(self.primary, "AFTER")
        self.wait_until_has(self.backup, "AFTER")

    def test_a_delete_reaches_the_other_server_and_sticks(self):
        self.start_server()
        self.start_server("--backup")
        time.sleep(1)

        asset_id = register(make_asset("DELETED"))["stored"]["asset_id"]
        self.wait_until_has(self.backup, "DELETED")
        self.assertEqual(common.send_request({"action": "DELETE", "asset_id": asset_id})["status"], "OK")

        time.sleep(server.SYNC_INTERVAL_SECONDS * 3)  # long enough for it to come back
        for address in (self.primary, self.backup):
            self.assertEqual(common.request_from(address, {"action": "LIST"})["assets"], [])


if __name__ == "__main__":
    unittest.main()

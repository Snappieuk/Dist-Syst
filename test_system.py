"""
Tests for the Asset Management System.

    python -m unittest -v test_system

These run real servers and talk to them over real TCP sockets, so they test the
whole path a client uses. SystemTest runs one server on a background thread, and
DistributedTest runs the primary and backup as separate programs. Every server uses
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

import backup
import common
import manage
import node
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
    listener = node.create_listening_socket("127.0.0.1", 0)
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
    """Starts each test with an empty node and its own temporary data file."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        node.ASSETS_FILE = os.path.join(self.temp_dir.name, "assets.json")
        node.assets.clear()
        node.deleted_ids.clear()
        node.last_seq = 0
        node.accepts_writes = True
        node.on_change = None
        node.ID_PREFIX = "AST-P"
        node.ROLE = "primary"


class SystemTest(NodeTestCase):
    @classmethod
    def setUpClass(cls):
        # Port 0 asks the operating system for any free port.
        listener = node.create_listening_socket("127.0.0.1", 0)
        common.SERVERS = [listener.getsockname()]
        threading.Thread(target=node.run_server, args=(listener,), daemon=True).start()

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

    def test_replies_name_the_server_that_answered(self):
        self.assertEqual(common.send_request({"action": "LIST"})["served_by"], "primary")

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
        node.assets.append(make_record(description="Dell laptop"))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            receive.main()

        self.assertIn("In Use", output.getvalue())
        self.assertIn("dell", output.getvalue())
        self.assertIn("primary", output.getvalue())

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
        node.assets.append(make_record("AST-P000002"))
        self.assertEqual(register(make_asset())["stored"]["asset_id"], "AST-P000003")

    def test_a_deleted_id_is_never_issued_again(self):
        first = register(make_asset("One"))["stored"]["asset_id"]
        second = register(make_asset("Two"))["stored"]["asset_id"]
        common.send_request({"action": "DELETE", "asset_id": second})

        third = register(make_asset("Three"))["stored"]["asset_id"]

        self.assertNotIn(third, (first, second))

    def test_each_server_issues_ids_from_its_own_range(self):
        node.ID_PREFIX = "AST-B"
        self.assertEqual(register(make_asset())["stored"]["asset_id"], "AST-B000001")

    # ---- Duplicates ----

    def test_the_same_item_cannot_be_registered_twice(self):
        first = register(make_asset())["stored"]["asset_id"]

        reply = register(make_asset())

        self.assertEqual(reply["code"], common.DUPLICATE_ASSET)
        self.assertEqual(reply["existing_id"], first)
        self.assertEqual(list_ids(), [first])

    def test_capitals_spacing_and_punctuation_do_not_hide_a_duplicate(self):
        register(make_asset("Dell laptop"))
        reply = register(make_asset("  DELL-laptop ", location="site  a", category="it equipment"))
        self.assertEqual(reply["code"], common.DUPLICATE_ASSET)

    def test_a_different_status_does_not_hide_a_duplicate(self):
        register(make_asset())
        reply = register(make_asset(status=common.NOT_IN_USE))
        self.assertEqual(reply["code"], common.DUPLICATE_ASSET)

    def test_a_different_location_is_a_different_item(self):
        register(make_asset())
        self.assertEqual(register(make_asset(location="Site B"))["status"], "OK")

    def test_a_deleted_item_can_be_registered_again_with_a_new_id(self):
        first = register(make_asset())["stored"]["asset_id"]
        common.send_request({"action": "DELETE", "asset_id": first})

        reply = register(make_asset())

        self.assertEqual(reply["status"], "OK")
        self.assertNotEqual(reply["stored"]["asset_id"], first)

    def test_invalid_request_ids_are_rejected(self):
        for request_id in (7, "", "   ", "x" * (node.MAX_FIELD_LENGTH + 1)):
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
        self.assertEqual(node.load_assets()[0][0]["status"], common.IN_USE)

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
            {"asset_id": asset_id, "status": common.NOT_IN_USE, "expected_version": "1"},
        )
        for request in requests:
            with self.subTest(request=request):
                reply = common.send_request({"action": "SET_STATUS", **request})
                self.assertEqual(reply["code"], common.BAD_REQUEST)

    def test_failed_status_save_leaves_nothing_behind(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)
        reply = common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE})

        self.assertEqual(reply["code"], common.INTERNAL)
        self.assertEqual(list_assets()[0]["status"], common.IN_USE)

    # ---- Simultaneous changes ----

    def test_a_change_based_on_an_old_version_is_rejected(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        # Two people read version 1. The first change works, and takes it to version 2.
        first = {"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE, "expected_version": 1}
        self.assertEqual(common.send_request(first)["status"], "OK")

        # The second person has not seen that, so their change must not overwrite it.
        second = {"action": "SET_STATUS", "asset_id": asset_id, "status": "Broken", "expected_version": 1}
        self.assertEqual(common.send_request(second)["code"], common.CONFLICT)
        self.assertEqual(list_assets()[0]["status"], common.NOT_IN_USE)

    def test_a_delete_based_on_an_old_version_is_rejected(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE})

        reply = common.send_request({"action": "DELETE", "asset_id": asset_id, "expected_version": 1})

        self.assertEqual(reply["code"], common.CONFLICT)
        self.assertEqual(list_ids(), [asset_id])

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

    def test_only_one_of_20_simultaneous_changes_to_a_version_wins(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        # Pause just after each version check, to hold open the gap before the change.
        # Without the lock, every client would pass the check before any had changed it.
        real_check = node.version_conflict

        def slow_check(asset, expected_version):
            result = real_check(asset, expected_version)
            time.sleep(0.05)
            return result

        with mock.patch.object(node, "version_conflict", slow_check):
            replies = self.run_at_once([
                {"action": "SET_STATUS", "asset_id": asset_id, "status": f"Status {n}", "expected_version": 1}
                for n in range(20)
            ])

        self.assertEqual(sum(reply["status"] == "OK" for reply in replies), 1)
        self.assertEqual(sum(reply.get("code") == common.CONFLICT for reply in replies), 19)
        self.assertEqual(list_assets()[0]["version"], 2)

    def test_50_concurrent_clients_lose_nothing_and_get_unique_ids(self):
        replies = self.run_at_once([common.new_register_request(make_asset(f"Asset {n}")) for n in range(50)])

        self.assertEqual(len(replies), 50)
        self.assertTrue(all(reply["status"] == "OK" for reply in replies))
        self.assertEqual(len({reply["stored"]["asset_id"] for reply in replies}), 50)
        self.assertEqual(len(list_ids()), 50)
        self.assertEqual(len(node.load_assets()[0]), 50)

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

        original_assets = node.assets
        node.assets = SlowList()
        self.addCleanup(setattr, node, "assets", original_assets)

    def test_20_clients_racing_with_one_registration_store_one_asset(self):
        self.use_slow_list()

        request = common.new_register_request(make_asset())
        replies = self.run_at_once([request] * 20)

        self.assertTrue(all(reply["status"] == "OK" for reply in replies))
        self.assertEqual(len(list_ids()), 1)

    def test_20_clients_racing_to_register_one_item_store_one_asset(self):
        self.use_slow_list()

        replies = self.run_at_once([common.new_register_request(make_asset()) for _ in range(20)])

        self.assertEqual(sum(reply["status"] == "OK" for reply in replies), 1)
        self.assertEqual(sum(reply.get("code") == common.DUPLICATE_ASSET for reply in replies), 19)
        self.assertEqual(len(list_ids()), 1)

    # ---- Deleting an asset ----

    def test_a_deleted_asset_is_gone_even_after_a_restart(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        kept_id = register(make_asset("Kept"))["stored"]["asset_id"]

        reply = common.send_request({"action": "DELETE", "asset_id": asset_id})
        self.assertEqual(reply["deleted"], asset_id)
        self.assertEqual(list_ids(), [kept_id])

        loaded_assets, loaded_deleted, loaded_seq = node.load_assets()
        self.assertEqual([asset["asset_id"] for asset in loaded_assets], [kept_id])
        self.assertIn(asset_id, loaded_deleted)
        self.assertEqual(loaded_seq, 3)

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

        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)
        reply = common.send_request({"action": "DELETE", "asset_id": asset_id})

        self.assertEqual(reply["code"], common.INTERNAL)
        self.assertEqual(list_ids(), [asset_id])
        self.assertEqual(node.deleted_ids, {})

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
            "too long": make_asset(description="x" * (node.MAX_FIELD_LENGTH + 1)),
        }
        for name, asset in invalid_assets.items():
            with self.subTest(name):
                self.assertEqual(register(asset)["code"], common.BAD_REQUEST)
        self.assertEqual(list_ids(), [])

    def test_largest_allowed_asset_gets_a_reply_and_can_be_listed(self):
        longest = "€" * node.MAX_FIELD_LENGTH
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
        node.assets.append(make_record(description="x" * (common.MAX_MESSAGE_BYTES + 1)))

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
        original_timeout = node.CLIENT_TIMEOUT_SECONDS
        node.CLIENT_TIMEOUT_SECONDS = 0.5
        self.addCleanup(setattr, node, "CLIENT_TIMEOUT_SECONDS", original_timeout)

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

    def test_client_waits_for_a_takeover_then_succeeds(self):
        node.accepts_writes = False
        threading.Timer(1.5, setattr, (node, "accepts_writes", True)).start()

        self.assertEqual(register(make_asset())["status"], "OK")

    def test_client_stops_waiting_if_no_server_ever_accepts_changes(self):
        node.accepts_writes = False
        self.addCleanup(setattr, common, "FAILOVER_WAIT_SECONDS", common.FAILOVER_WAIT_SECONDS)
        common.FAILOVER_WAIT_SECONDS = 1

        self.assertEqual(register(make_asset())["code"], common.NOT_PRIMARY)

    def test_a_server_on_standby_still_answers_list(self):
        node.accepts_writes = False
        self.assertEqual(common.send_request({"action": "LIST"})["status"], "OK")

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
        with open(node.ASSETS_FILE, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["assets"][0]["asset_id"], asset_id)

    def test_assets_are_loaded_after_a_restart(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        # What a restart does: memory is lost, then the file is read.
        node.assets.clear()
        node.assets.extend(node.load_assets()[0])

        self.assertEqual(list_ids(), [asset_id])

    def test_a_file_from_before_delete_was_added_still_loads(self):
        with open(node.ASSETS_FILE, "w", encoding="utf-8") as file:
            json.dump([make_record()], file)

        self.assertEqual(node.load_assets(), ([make_record()], {}, 0))

    def test_failed_save_leaves_nothing_behind(self):
        register(make_asset("First"))

        # A folder in place of the temporary file makes the next save fail.
        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.assertEqual(register(make_asset("Second"))["code"], common.INTERNAL)
        os.rmdir(blocker)

        # Neither memory nor the file has the failed asset, and the file is intact.
        self.assertEqual(list_descriptions(), ["First"])
        self.assertEqual(len(node.load_assets()[0]), 1)
        self.assertEqual(node.last_seq, 1)

        # The same asset works once the problem has gone.
        self.assertEqual(register(make_asset("Second"))["status"], "OK")

    def test_corrupt_file_is_reported_not_overwritten(self):
        for content in ("{corrupt", "7", '{"assets": []}'):
            with self.subTest(content):
                with open(node.ASSETS_FILE, "w", encoding="utf-8") as file:
                    file.write(content)
                with self.assertRaises(ValueError):
                    node.load_assets()


class ReconcileTest(NodeTestCase):
    """The decisions the primary makes when the two servers' data differs."""

    def test_merge_copies_only_what_is_missing(self):
        node.assets.append(make_record("AST-P000001"))

        changed = server.merge_state([make_record("AST-P000001"), make_record("AST-B000001")], {})

        self.assertEqual(changed, 1)
        self.assertEqual([asset["asset_id"] for asset in node.assets], ["AST-P000001", "AST-B000001"])

    def test_with_equal_versions_the_primary_keeps_its_own_copy(self):
        node.assets.append(make_record(status="Primary", version=2))

        self.assertEqual(server.merge_state([make_record(status="Backup", version=2)], {}), 0)
        self.assertEqual(node.assets[0]["status"], "Primary")

    def test_the_higher_version_wins(self):
        node.assets.append(make_record(status="Primary", version=2))

        self.assertEqual(server.merge_state([make_record(status="Backup", version=3)], {}), 1)
        self.assertEqual(node.assets[0]["status"], "Backup")

    def test_a_delete_beats_a_newer_change(self):
        node.assets.append(make_record("AST-P000001"))
        node.assets.append(make_record("AST-P000002"))

        changed = server.merge_state([make_record("AST-P000001", version=9)], {"AST-P000001": None})

        self.assertEqual(changed, 1)
        self.assertEqual([asset["asset_id"] for asset in node.assets], ["AST-P000002"])
        self.assertIn("AST-P000001", node.deleted_ids)

    def test_a_deleted_asset_cannot_come_back_from_the_other_server(self):
        node.deleted_ids["AST-P000001"] = None

        self.assertEqual(server.merge_state([make_record("AST-P000001", version=9)], {}), 0)
        self.assertEqual(node.assets, [])

    def test_the_same_item_on_both_servers_keeps_the_primarys_record(self):
        node.assets.append(make_record("AST-P000005", description="Same item"))

        changed = server.merge_state([make_record("AST-B000001", description="Same item")], {})

        self.assertEqual(changed, 2)  # one copied across, then removed as a duplicate
        self.assertEqual([asset["asset_id"] for asset in node.assets], ["AST-P000005"])
        self.assertIn("AST-B000001", node.deleted_ids)

    def test_invalid_records_from_the_backup_are_recognised(self):
        for record in ({"no_id": 1}, {"asset_id": "JUNK"}, [1], make_record(tags=7), make_record(version="1")):
            with self.subTest(record=record):
                self.assertFalse(server.is_valid_peer_asset(record))
        self.assertTrue(server.is_valid_peer_asset(make_record()))


class ReplicaTest(NodeTestCase):
    """How the backup treats changes sent by the primary."""

    def setUp(self):
        super().setUp()
        node.accepts_writes = False
        node.ID_PREFIX = "AST-B"
        node.ROLE = "backup"

    def test_changes_are_applied_in_order_and_saved(self):
        self.assertEqual(backup.apply_change({"seq": 1, "asset": make_record("AST-P000001", status="One")})["status"], "OK")
        self.assertEqual(backup.apply_change({"seq": 2, "asset": make_record("AST-P000001", status="Two")})["status"], "OK")
        self.assertEqual(backup.apply_change({"seq": 3, "asset": make_record("AST-P000002")})["status"], "OK")
        self.assertEqual(backup.apply_change({"seq": 4, "deleted": "AST-P000002", "request_id": None})["status"], "OK")

        assets, deleted, seq = node.load_assets()
        self.assertEqual([asset["status"] for asset in assets], ["Two"])
        self.assertIn("AST-P000002", deleted)
        self.assertEqual(seq, 4)

    def test_a_missed_change_is_detected(self):
        reply = backup.apply_change({"seq": 5, "asset": make_record()})

        self.assertEqual(reply["code"], common.OUT_OF_SYNC)
        self.assertEqual(node.assets, [])

    def test_a_backup_that_took_over_refuses_copies(self):
        node.accepts_writes = True

        reply = backup.apply_change({"seq": 1, "asset": make_record()})

        self.assertEqual(reply["code"], common.NOT_STANDBY)

    def test_a_failed_save_leaves_nothing_behind(self):
        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)

        with self.assertRaises(OSError):
            backup.apply_change({"seq": 1, "asset": make_record()})

        self.assertEqual(node.assets, [])
        self.assertEqual(node.last_seq, 0)

    def test_standby_refuses_changes_from_clients(self):
        for reply in (
            node.register_asset(make_asset(), None),
            node.set_asset_status("AST-P000001", common.NOT_IN_USE),
            node.delete_asset("AST-P000001"),
        ):
            self.assertEqual(reply["code"], common.NOT_PRIMARY)

    def test_handover_stops_changes_and_sends_everything(self):
        node.accepts_writes = True
        node.register_asset(node.validate_asset(make_asset()), None)

        reply = backup.hand_over({})

        self.assertFalse(node.accepts_writes)
        self.assertEqual([asset["asset_id"] for asset in reply["assets"]], ["AST-B000001"])
        self.assertEqual(reply["seq"], 1)

    def test_installing_state_replaces_the_data(self):
        node.assets.append(make_record("AST-B000001"))

        backup.install_state({"assets": [make_record("AST-P000001")], "deleted": {"AST-B000001": None}, "seq": 9})

        assets, deleted, seq = node.load_assets()
        self.assertEqual([asset["asset_id"] for asset in assets], ["AST-P000001"])
        self.assertEqual(deleted, {"AST-B000001": None})
        self.assertEqual(seq, 9)


class DistributedTest(unittest.TestCase):
    """Runs the primary and backup as real, separate programs, and stops them."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_path = temp_dir.name

        self.primary = unused_address()
        self.backup = unused_address()
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [self.primary, self.backup]

    def start_program(self, module: str) -> subprocess.Popen:
        """Start server.py or backup.py as its own process, using the test's addresses
        and its own data file."""
        code = (
            "import common\n"
            f"common.SERVERS = {common.SERVERS!r}\n"
            f"import {module} as program\n"
            f"program.ASSETS_FILE = {os.path.join(self.temp_path, module + '.json')!r}\n"
            "program.main()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def start_primary(self) -> subprocess.Popen:
        return self.start_program("server")

    def start_backup(self) -> subprocess.Popen:
        return self.start_program("backup")

    def stop(self, process: subprocess.Popen) -> None:
        process.kill()
        process.wait()

    def wait_until(self, condition, explanation: str) -> None:
        """Wait for a condition, failing after a while."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except OSError:
                pass  # the server may still be starting
            time.sleep(0.2)
        self.fail(f"Never {explanation}")

    def assets_on(self, address: tuple) -> list:
        return common.request_from(address, {"action": "LIST"})["assets"]

    def descriptions_on(self, address: tuple) -> list:
        return sorted(asset["description"] for asset in self.assets_on(address))

    def wait_until_in_sync(self) -> None:
        """Wait until the backup is on standby with the same data as the primary."""
        def in_sync() -> bool:
            backup_state = common.request_from(self.backup, {"action": "PING"})
            return (
                not backup_state["accepts_writes"]
                and self.descriptions_on(self.primary) == self.descriptions_on(self.backup)
            )

        self.wait_until(in_sync, "got the servers in sync")

    def wait_until_taken_over(self) -> None:
        self.wait_until(
            lambda: common.request_from(self.backup, {"action": "PING"})["accepts_writes"],
            "saw the backup take over",
        )

    def test_a_change_is_on_the_backup_before_the_client_hears_it_worked(self):
        self.start_backup()
        self.start_primary()
        self.wait_until_in_sync()

        for n in range(5):
            self.assertEqual(register(make_asset(f"Asset {n}"))["status"], "OK")
            # No waiting: this is the point of synchronous replication.
            self.assertIn(f"Asset {n}", self.descriptions_on(self.backup))

    def test_status_changes_and_deletes_reach_the_backup_immediately(self):
        self.start_backup()
        self.start_primary()
        self.wait_until_in_sync()

        changed_id = register(make_asset("Changed"))["stored"]["asset_id"]
        deleted_id = register(make_asset("Deleted"))["stored"]["asset_id"]

        common.send_request({"action": "SET_STATUS", "asset_id": changed_id, "status": common.NOT_IN_USE})
        common.send_request({"action": "DELETE", "asset_id": deleted_id})

        on_backup = self.assets_on(self.backup)
        self.assertEqual([asset["asset_id"] for asset in on_backup], [changed_id])
        self.assertEqual(on_backup[0]["status"], common.NOT_IN_USE)

    def test_the_backup_refuses_changes_while_the_primary_is_alive(self):
        self.start_backup()
        self.start_primary()
        self.wait_until_in_sync()

        reply = common.request_from(self.backup, common.new_register_request(make_asset()))

        self.assertEqual(reply["code"], common.NOT_PRIMARY)
        self.assertEqual(reply["served_by"], "backup")

    def test_the_backup_takes_over_and_hands_back_when_the_primary_returns(self):
        primary = self.start_primary()
        self.start_backup()
        self.wait_until_in_sync()
        self.assertEqual(register(make_asset("BEFORE"))["stored"]["asset_id"], "AST-P000001")

        # With the primary gone, the client waits for the takeover, then carries on.
        self.stop(primary)
        reply = register(make_asset("DURING"))
        self.assertEqual(reply["status"], "OK")
        self.assertEqual(reply["served_by"], "backup")
        self.assertTrue(reply["stored"]["asset_id"].startswith("AST-B"))
        self.assertEqual(sorted(list_descriptions()), ["BEFORE", "DURING"])

        # Changes made during the outage survive the primary's return.
        during_id = reply["stored"]["asset_id"]
        common.send_request({"action": "SET_STATUS", "asset_id": during_id, "status": common.NOT_IN_USE})

        self.start_primary()
        self.wait_until_in_sync()
        self.assertEqual(self.descriptions_on(self.primary), ["BEFORE", "DURING"])
        statuses = {asset["description"]: asset["status"] for asset in self.assets_on(self.primary)}
        self.assertEqual(statuses["DURING"], common.NOT_IN_USE)

        # The primary is in charge again, and the IDs it issues do not clash.
        reply = register(make_asset("AFTER"))
        self.assertEqual(reply["served_by"], "primary")
        self.assertIn("AFTER", self.descriptions_on(self.backup))

    def test_the_primary_carries_on_alone_and_the_backup_catches_up(self):
        backup_process = self.start_backup()
        self.start_primary()
        self.wait_until_in_sync()

        self.stop(backup_process)
        self.assertEqual(register(make_asset("ALONE"))["served_by"], "primary")

        self.start_backup()
        self.wait_until_in_sync()
        self.assertEqual(self.descriptions_on(self.backup), ["ALONE"])
        self.assertEqual(register(make_asset("BOTH"))["status"], "OK")
        self.assertEqual(self.descriptions_on(self.backup), ["ALONE", "BOTH"])

    def test_two_servers_that_each_took_changes_alone_are_reconciled(self):
        # Each server runs alone in turn, as they would if the network split. They
        # issue IDs at the same time without being able to ask each other.
        same_item = common.new_register_request(make_asset("ON BOTH"))

        primary = self.start_primary()
        self.wait_until(lambda: self.assets_on(self.primary) == [], "saw the primary start")
        kept_id = common.request_from(self.primary, same_item)["stored"]["asset_id"]
        self.assertEqual(common.request_from(self.primary, common.new_register_request(make_asset("PRIMARY ONLY")))["status"], "OK")
        self.stop(primary)

        self.start_backup()
        self.wait_until_taken_over()
        self.assertEqual(common.request_from(self.backup, same_item)["status"], "OK")
        self.assertEqual(common.request_from(self.backup, common.new_register_request(make_asset("BACKUP ONLY")))["status"], "OK")

        # When the two can reach each other, each ends up with one record of each item.
        self.start_primary()
        self.wait_until_in_sync()
        expected = ["BACKUP ONLY", "ON BOTH", "PRIMARY ONLY"]
        self.assertEqual(self.descriptions_on(self.primary), expected)
        self.assertEqual(self.descriptions_on(self.backup), expected)

        ids = [asset["asset_id"] for asset in self.assets_on(self.primary)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(kept_id, ids)

    def test_deleted_assets_stay_deleted_through_a_restart_of_both(self):
        primary = self.start_primary()
        backup_process = self.start_backup()
        self.wait_until_in_sync()
        deleted_id = register(make_asset("Deleted"))["stored"]["asset_id"]
        register(make_asset("Kept"))
        common.send_request({"action": "DELETE", "asset_id": deleted_id})

        self.stop(primary)
        self.stop(backup_process)
        self.start_primary()
        self.start_backup()
        self.wait_until_in_sync()

        self.assertEqual(self.descriptions_on(self.primary), ["Kept"])
        self.assertEqual(self.descriptions_on(self.backup), ["Kept"])


if __name__ == "__main__":
    unittest.main()

"""
Tests for the Asset Management System.

    python -m unittest -v tests.test_system

These run real servers and talk to them over real TCP sockets, so they test the
whole path a client uses. SystemTest runs one server on a background thread, and
DistributedTest runs the primary and backup as separate programs. Every server uses
temporary data files and spare ports, so your real assets.json is never touched. The
programs do share secret.key, which is created beside the code if it doesn't exist yet.
"""

import contextlib
import datetime
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Optional
from unittest import mock

import accounts
import cli
import common
import node
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
    """Send exact bytes to the server, as a faulty client might, and read the reply. The
    bytes go inside an encrypted connection, as every connection must."""
    with common.connect(common.SERVERS[0], timeout=5) as sock:
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
            with common.tls_context(server_side=True).wrap_socket(conn, server_side=True) as tls:
                common.receive_message(tls)
                if reply is not None:
                    common.send_message(tls, reply)

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
        node.deleted_at.clear()
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
                "date_registered", "tags", "version", "request_id", "created_at", "updated_at",
                "history",
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
            cli.list_assets()

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

    def test_only_the_two_statuses_are_accepted(self):
        for status in ("Broken", "in use", "In  Use", "Spare", "Not in use"):
            with self.subTest(status):
                reply = register(make_asset(status=status))
                self.assertEqual(reply["code"], common.BAD_REQUEST)
                self.assertIn("In Use, Not In Use", reply["detail"])
        self.assertEqual(list_ids(), [])

        asset_id = register(make_asset())["stored"]["asset_id"]
        reply = common.send_request({"action": "SET_STATUS", "asset_id": asset_id, "status": "Broken"})
        self.assertEqual(reply["code"], common.BAD_REQUEST)
        self.assertEqual(list_assets()[0]["status"], common.IN_USE)

    def test_surrounding_spaces_around_a_status_are_ignored(self):
        self.assertEqual(register(make_asset(status="  Not In Use "))["stored"]["status"], common.NOT_IN_USE)

    def test_older_assets_with_other_statuses_still_load_and_can_be_changed(self):
        node.assets.append(make_record("AST-OLD", status="Spare", version=1))

        reply = common.send_request({"action": "SET_STATUS", "asset_id": "AST-OLD", "status": common.IN_USE})

        self.assertEqual(reply["stored"]["status"], common.IN_USE)

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

    # ---- Searching and paging ----

    def search(self, **options) -> dict:
        return common.send_request({"action": "LIST", **options})

    def add_examples(self) -> list:
        """Register five assets, returning their IDs in the order they were stored."""
        examples = (
            make_asset("Dell laptop", category="IT Equipment", location="Site A"),
            make_asset("HP printer", category="Printers", location="Site B", status=common.NOT_IN_USE),
            make_asset("Dell monitor", category="IT Equipment", location="Site B"),
            make_asset("Office chair", category="Furniture", location="Site A", status=common.NOT_IN_USE),
            make_asset("Projector", category="AV", location="Dell Room"),
        )
        return [register(asset)["stored"]["asset_id"] for asset in examples]

    def descriptions(self, reply: dict) -> list:
        return [asset["description"] for asset in reply["assets"]]

    def test_list_without_options_returns_everything_in_order(self):
        ids = self.add_examples()

        reply = self.search()

        self.assertEqual([asset["asset_id"] for asset in reply["assets"]], ids)
        self.assertEqual(reply["total"], 5)
        self.assertEqual(reply["assets"], list(node.assets))

    def test_the_query_is_found_in_any_field_ignoring_capitals(self):
        ids = self.add_examples()

        self.assertEqual(self.descriptions(self.search(query="dell")), ["Dell laptop", "Dell monitor", "Projector"])
        self.assertEqual(self.descriptions(self.search(query="  PRINTERS ")), ["HP printer"])
        self.assertEqual(self.descriptions(self.search(query="site b")), ["HP printer", "Dell monitor"])
        self.assertEqual(self.descriptions(self.search(query=ids[3].lower())), ["Office chair"])
        self.assertEqual(self.search(query="not in use")["total"], 2)
        self.assertEqual(self.search(query="nothing like this")["assets"], [])

    def test_the_status_filter_matches_exactly(self):
        self.add_examples()

        reply = self.search(status=common.NOT_IN_USE)

        self.assertEqual(self.descriptions(reply), ["HP printer", "Office chair"])
        self.assertEqual(reply["total"], 2)
        self.assertEqual(self.search(status="not in use")["total"], 0)

    def test_query_and_status_combine(self):
        self.add_examples()

        reply = self.search(query="site a", status=common.IN_USE)

        self.assertEqual(self.descriptions(reply), ["Dell laptop"])

    def test_offset_and_limit_page_through_the_matches(self):
        self.add_examples()

        pages = [self.search(offset=offset, limit=2) for offset in (0, 2, 4, 6)]

        self.assertEqual([self.descriptions(page) for page in pages],
                         [["Dell laptop", "HP printer"], ["Dell monitor", "Office chair"], ["Projector"], []])
        self.assertEqual({page["total"] for page in pages}, {5})

    def test_paging_applies_after_searching(self):
        self.add_examples()

        reply = self.search(query="dell", offset=1, limit=1)

        self.assertEqual(self.descriptions(reply), ["Dell monitor"])
        self.assertEqual(reply["total"], 3)

    def test_invalid_search_options_are_rejected(self):
        for options in ({"query": 7}, {"status": ["In Use"]}, {"query": "x" * (node.MAX_FIELD_LENGTH + 1)},
                        {"offset": -1}, {"offset": "1"}, {"limit": 0}, {"limit": True}, {"limit": 2.5}):
            with self.subTest(options):
                self.assertEqual(self.search(**options)["code"], common.BAD_REQUEST)

    def test_older_assets_missing_fields_can_still_be_searched(self):
        node.assets.append({"asset_id": "AST-OLD", "description": "Old thing"})

        self.assertEqual(self.search(query="old")["total"], 1)
        self.assertEqual(self.search(query="site")["total"], 0)

    def test_the_backup_can_search_too(self):
        self.add_examples()
        node.accepts_writes = False  # on standby, which still answers LIST

        self.assertEqual(self.search(query="dell")["total"], 3)

    # ---- Editing an asset ----

    def edit(self, asset_id: str, expected_version: object = None, **changes) -> dict:
        request = {"action": "EDIT", "asset_id": asset_id, "changes": changes}
        if expected_version is not None:
            request["expected_version"] = expected_version
        return common.send_request(request)

    def test_any_of_the_four_fields_can_be_edited(self):
        original = register(make_asset())["stored"]

        reply = self.edit(original["asset_id"], 1, description="HP EliteBook", category="Laptops",
                          location="Site B", status=common.NOT_IN_USE)

        stored = reply["stored"]
        self.assertEqual(reply["status"], "OK")
        self.assertEqual((stored["description"], stored["category"], stored["location"], stored["status"]),
                         ("HP EliteBook", "Laptops", "Site B", common.NOT_IN_USE))
        self.assertEqual(stored["version"], 2)
        self.assertEqual(stored["created_at"], original["created_at"])
        self.assertIn("elitebook", stored["tags"])
        self.assertNotIn("dell", stored["tags"])
        self.assertEqual(list_assets(), [stored])
        self.assertEqual(node.load_assets()[0], [stored])

    def test_only_the_fields_sent_are_changed(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        stored = self.edit(asset_id, location="  Site C  ")["stored"]

        self.assertEqual(stored["location"], "Site C")
        self.assertEqual(stored["description"], "Dell laptop")

    def test_an_edit_that_changes_nothing_keeps_the_version(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        reply = self.edit(asset_id, 1, description="Dell laptop")

        self.assertEqual(reply["stored"]["version"], 1)

    def test_an_edit_based_on_an_old_version_is_rejected(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        self.edit(asset_id, 1, description="First")

        reply = self.edit(asset_id, 1, description="Second")

        self.assertEqual(reply["code"], common.CONFLICT)
        self.assertEqual(list_descriptions(), ["First"])

    def test_an_edit_cannot_make_a_duplicate(self):
        register(make_asset("Dell laptop"))
        other_id = register(make_asset("HP laptop"))["stored"]["asset_id"]

        reply = self.edit(other_id, description="dell  LAPTOP")

        self.assertEqual(reply["code"], common.DUPLICATE_ASSET)
        self.assertEqual(sorted(list_descriptions()), ["Dell laptop", "HP laptop"])

    def test_an_asset_can_be_edited_to_differ_only_in_capitals_from_itself(self):
        asset_id = register(make_asset("dell laptop"))["stored"]["asset_id"]
        self.assertEqual(self.edit(asset_id, description="Dell Laptop")["status"], "OK")

    def test_invalid_edits_are_rejected(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        requests = {
            "no changes": {"asset_id": asset_id},
            "empty changes": {"asset_id": asset_id, "changes": {}},
            "changes not an object": {"asset_id": asset_id, "changes": ["description"]},
            "unknown field": {"asset_id": asset_id, "changes": {"asset_id": "MINE"}},
            "server field": {"asset_id": asset_id, "changes": {"created_at": "2000-01-01T00:00:00Z"}},
            "empty value": {"asset_id": asset_id, "changes": {"description": "  "}},
            "other status": {"asset_id": asset_id, "changes": {"status": "Broken"}},
            "too long": {"asset_id": asset_id, "changes": {"location": "x" * (node.MAX_FIELD_LENGTH + 1)}},
            "no asset_id": {"changes": {"description": "New"}},
            "bad version": {"asset_id": asset_id, "changes": {"description": "New"}, "expected_version": "1"},
        }
        for name, request in requests.items():
            with self.subTest(name):
                self.assertEqual(common.send_request({"action": "EDIT", **request})["code"], common.BAD_REQUEST)
        self.assertEqual(list_assets()[0]["version"], 1)

    def test_editing_an_unknown_asset_is_not_found(self):
        self.assertEqual(self.edit("AST-X", description="New")["code"], common.NOT_FOUND)

    def test_a_failed_edit_save_leaves_nothing_behind(self):
        asset_id = register(make_asset())["stored"]["asset_id"]
        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)

        self.assertEqual(self.edit(asset_id, description="New")["code"], common.INTERNAL)
        self.assertEqual(list_descriptions(), ["Dell laptop"])

    # ---- Simultaneous changes ----

    def test_a_change_based_on_an_old_version_is_rejected(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        # Two people read version 1. The first change works, and takes it to version 2.
        first = {"action": "SET_STATUS", "asset_id": asset_id, "status": common.NOT_IN_USE, "expected_version": 1}
        self.assertEqual(common.send_request(first)["status"], "OK")

        # The second person has not seen that, so their change must not overwrite it.
        second = {"action": "SET_STATUS", "asset_id": asset_id, "status": common.IN_USE, "expected_version": 1}
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

        # Each request makes a different edit, so none of them is already true.
        with mock.patch.object(node, "version_conflict", slow_check):
            replies = self.run_at_once([
                {"action": "EDIT", "asset_id": asset_id, "changes": {"description": f"Edit {n}"},
                 "expected_version": 1}
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
        """Run cli.py change, giving it these answers to its prompts, and return its output."""
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(output):
            cli.change_asset()
        return output.getvalue()

    def test_manage_client_edits_only_what_is_typed(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        output = self.run_manage(asset_id, "4", "HP laptop", "", "Site B", "2")

        stored = list_assets()[0]
        self.assertEqual((stored["description"], stored["category"], stored["location"], stored["status"]),
                         ("HP laptop", "IT Equipment", "Site B", common.NOT_IN_USE))
        self.assertIn("'status': 'OK'", output)

    def test_manage_client_asks_again_for_a_bad_status_number(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        output = self.run_manage(asset_id, "4", "", "", "", "Broken", "9", "2")

        self.assertEqual(output.count("Please type a number"), 2)
        self.assertEqual(list_assets()[0]["status"], common.NOT_IN_USE)

    def test_manage_client_sends_nothing_if_nothing_changed(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        output = self.run_manage(asset_id, "4", "Dell laptop", "", "", "")  # the same description

        self.assertIn("Nothing was changed", output)
        self.assertEqual(list_assets()[0]["version"], 1)

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
        asset = make_asset(longest, category=longest, location=longest)

        self.assertEqual(register(asset)["status"], "OK")
        self.assertEqual(list_descriptions(), [longest])

    def test_unknown_action_is_rejected(self):
        reply = common.send_request({"action": "EXPLODE"})
        self.assertEqual(reply["code"], common.BAD_REQUEST)

    # ---- Authorization: only programs with the shared secret may use the server ----

    def test_requests_without_the_secret_are_refused(self):
        register(make_asset())
        attempts = {
            "no secret": {"action": "LIST"},
            "wrong secret": {"action": "LIST", "auth": "wrong"},
            "not text": {"action": "LIST", "auth": 7},
            "not plain text": {"action": "LIST", "auth": "é€"},
            "cannot change data": {"action": "REGISTER", "asset": make_asset("New"), "auth": "wrong"},
        }
        for name, request in attempts.items():
            with self.subTest(name):
                reply = send_raw(with_header(json.dumps(request).encode("utf-8")))
                self.assertEqual(reply["code"], common.UNAUTHORIZED)
                self.assertNotIn("assets", reply)
        self.assertEqual(len(list_ids()), 1)

    # ---- Protocol version: every request says which message format it uses ----

    def test_requests_without_a_supported_protocol_version_are_refused(self):
        register(make_asset())
        for name, version in {"missing": None, "unknown": 99, "text": "1", "true": True, "list": [1]}.items():
            with self.subTest(name):
                request = {"action": "LIST", "auth": common.shared_secret()}
                if version is not None:
                    request["protocol_version"] = version
                reply = send_raw(with_header(json.dumps(request).encode("utf-8")))
                self.assertEqual(reply["code"], common.UNSUPPORTED_VERSION)
                self.assertEqual(reply["supported_versions"], list(common.SUPPORTED_PROTOCOL_VERSIONS))
                self.assertNotIn("assets", reply)

    def test_an_unsupported_version_cannot_change_data(self):
        request = {"action": "REGISTER", "asset": make_asset(), "auth": common.shared_secret(), "protocol_version": 99}
        reply = send_raw(with_header(json.dumps(request).encode("utf-8")))

        self.assertEqual(reply["code"], common.UNSUPPORTED_VERSION)
        self.assertEqual(list_ids(), [])

    def test_every_request_is_sent_with_the_current_version(self):
        request = {"action": "LIST", "auth": common.shared_secret(), "protocol_version": common.PROTOCOL_VERSION}
        self.assertEqual(send_raw(with_header(json.dumps(request).encode("utf-8")))["status"], "OK")

    def test_the_secret_is_kept_after_it_is_created(self):
        self.addCleanup(setattr, common, "SECRET_FILE", common.SECRET_FILE)
        self.addCleanup(setattr, common, "_secret", common._secret)
        common.SECRET_FILE = os.path.join(self.temp_dir.name, "secret.key")

        common._secret = None
        created = common.shared_secret()
        common._secret = None

        self.assertGreater(len(created), 32)
        self.assertEqual(common.shared_secret(), created)

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
            sock = common.connect(common.SERVERS[0])
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

        # A plain connection that never even starts the encrypted handshake.
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


class ReviewLoanHistoryTest(NodeTestCase):
    """Flagging assets added while the primary was offline, lending assets, and history."""

    def ask(self, **request) -> dict:
        return node.process_request(request)

    def add(self, description="Dell laptop", **extra) -> dict:
        reply = self.ask(action="REGISTER", asset=make_asset(description, **extra))
        self.assertEqual(reply["status"], "OK", reply)
        return reply["stored"]

    def test_only_assets_added_by_a_backup_are_flagged(self):
        self.assertNotIn("flagged", self.add("On the primary"))
        node.ROLE = "backup"
        flagged = self.add("On the backup")
        self.assertTrue(flagged["flagged"])
        self.assertIn("primary was offline", flagged["history"][0]["detail"])

    def test_approving_clears_the_flag_and_is_recorded(self):
        node.ROLE = "backup"
        asset = self.add()
        reply = self.ask(action="APPROVE", asset_id=asset["asset_id"], expected_version=1, user="alice")
        self.assertFalse(reply["stored"]["flagged"])
        self.assertEqual(reply["stored"]["version"], 2)
        last = reply["stored"]["history"][-1]
        self.assertEqual((last["action"], last["user"]), ("APPROVE", "alice"))

    def test_a_loan_is_recorded_and_returned(self):
        asset = self.add()
        out = self.ask(action="CHECK_OUT", asset_id=asset["asset_id"], borrower="Sam Lee",
                       start="2026-11-01", due="2026-11-14")["stored"]
        self.assertEqual(out["status"], common.IN_USE)
        self.assertEqual(out["loans"], [{"borrower": "Sam Lee", "start": "2026-11-01", "due": "2026-11-14",
                                         "returned": None}])

        back = self.ask(action="CHECK_IN", asset_id=asset["asset_id"], returned="2026-11-10")["stored"]
        self.assertEqual(back["status"], common.NOT_IN_USE)
        self.assertEqual(back["loans"][0]["returned"], "2026-11-10")
        self.assertEqual([entry["action"] for entry in back["history"]], ["REGISTER", "CHECK_OUT", "CHECK_IN"])

    def test_bad_loans_are_refused(self):
        asset_id = self.add()["asset_id"]
        for request in (
            {"borrower": "Sam", "start": "2026-11-10", "due": "2026-11-01"},  # due before start
            {"borrower": "Sam", "start": "10/11/2026", "due": "2026-11-20"},  # not YYYY-MM-DD
            {"borrower": "", "start": "2026-11-01", "due": "2026-11-20"},    # no borrower
        ):
            with self.subTest(request):
                self.assertEqual(self.ask(action="CHECK_OUT", asset_id=asset_id, **request)["code"], common.BAD_REQUEST)
        self.assertEqual(self.ask(action="CHECK_IN", asset_id=asset_id)["code"], common.BAD_REQUEST)  # not on loan

    def test_an_asset_on_loan_cant_be_lent_again(self):
        asset_id = self.add()["asset_id"]
        loan = {"borrower": "Sam", "start": "2026-11-01", "due": "2026-11-20"}
        self.ask(action="CHECK_OUT", asset_id=asset_id, **loan)
        reply = self.ask(action="CHECK_OUT", asset_id=asset_id, **dict(loan, borrower="Alex"))
        self.assertEqual(reply["code"], common.CONFLICT)
        self.assertIn("Sam", reply["detail"])

    def test_status_changes_and_edits_are_in_the_history(self):
        asset_id = self.add()["asset_id"]
        self.ask(action="SET_STATUS", asset_id=asset_id, status=common.NOT_IN_USE)
        edited = self.ask(action="EDIT", asset_id=asset_id, changes={"location": "Site B"})["stored"]
        details = [entry["detail"] for entry in edited["history"]]
        self.assertEqual(details[1], "Status changed from In Use to Not In Use")
        self.assertIn("location to 'Site B'", details[2])

    def test_sample_assets_are_tagged(self):
        sample = self.add("Made up", sample=True)
        self.assertTrue(sample["sample"])
        self.assertIn(common.SAMPLE_TAG, sample["tags"])
        self.assertNotIn(common.SAMPLE_TAG, self.add("Real")["tags"])


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
        self.assertEqual(server.apply_change({"seq": 1, "asset": make_record("AST-P000001", status="One")})["status"], "OK")
        self.assertEqual(server.apply_change({"seq": 2, "asset": make_record("AST-P000001", status="Two")})["status"], "OK")
        self.assertEqual(server.apply_change({"seq": 3, "asset": make_record("AST-P000002")})["status"], "OK")
        self.assertEqual(server.apply_change({"seq": 4, "deleted": "AST-P000002", "request_id": None})["status"], "OK")

        assets, deleted, seq = node.load_assets()
        self.assertEqual([asset["status"] for asset in assets], ["Two"])
        self.assertIn("AST-P000002", deleted)
        self.assertEqual(seq, 4)

    def test_a_missed_change_is_detected(self):
        reply = server.apply_change({"seq": 5, "asset": make_record()})

        self.assertEqual(reply["code"], common.OUT_OF_SYNC)
        self.assertEqual(node.assets, [])

    def test_a_backup_that_took_over_refuses_copies(self):
        node.accepts_writes = True

        reply = server.apply_change({"seq": 1, "asset": make_record()})

        self.assertEqual(reply["code"], common.NOT_STANDBY)

    def test_a_failed_save_leaves_nothing_behind(self):
        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)

        with self.assertRaises(OSError):
            server.apply_change({"seq": 1, "asset": make_record()})

        self.assertEqual(node.assets, [])
        self.assertEqual(node.last_seq, 0)

    def test_standby_refuses_changes_from_clients(self):
        for reply in (
            node.register_asset(make_asset(), None),
            node.set_asset_status("AST-P000001", common.NOT_IN_USE),
            node.edit_asset("AST-P000001", {"description": "New"}),
            node.delete_asset("AST-P000001"),
        ):
            self.assertEqual(reply["code"], common.NOT_PRIMARY)

    def test_handover_stops_changes_and_sends_everything(self):
        node.accepts_writes = True
        node.register_asset(node.validate_asset(make_asset()), None)

        reply = server.hand_over({})

        self.assertFalse(node.accepts_writes)
        self.assertEqual([asset["asset_id"] for asset in reply["assets"]], ["AST-B000001"])
        self.assertEqual(reply["seq"], 1)

    def test_installing_state_replaces_the_data(self):
        node.assets.append(make_record("AST-B000001"))

        server.install_state({"assets": [make_record("AST-P000001")], "deleted": {"AST-B000001": None}, "seq": 9})

        assets, deleted, seq = node.load_assets()
        self.assertEqual([asset["asset_id"] for asset in assets], ["AST-P000001"])
        self.assertEqual(deleted, {"AST-B000001": None})
        self.assertEqual(seq, 9)


class AuditTest(NodeTestCase):
    """Every change a server makes is added to its audit log, saying who made it."""

    def audited(self, *requests) -> list:
        """Run requests through the server's rules, returning the audit records written."""
        with self.assertLogs(common.audit_logger, "INFO") as logs:
            common.audit("TEST_START")  # so assertLogs has a record even if nothing else does
            for request in requests:
                node.process_request(request)
        return [json.loads(line.split(":", 2)[2]) for line in logs.output][1:]

    def register_request(self, description="Dell laptop", **extra) -> dict:
        return {"action": "REGISTER", "request_id": f"req-{description}", "asset": make_asset(description), **extra}

    def test_each_kind_of_change_is_recorded(self):
        records = self.audited(
            self.register_request(user="alex"),
            {"action": "SET_STATUS", "asset_id": "AST-P000001", "status": common.NOT_IN_USE, "user": "alex"},
            {"action": "EDIT", "asset_id": "AST-P000001", "changes": {"location": "B"}, "user": "sam"},
            {"action": "DELETE", "asset_id": "AST-P000001", "request_id": "del-1"},
        )

        self.assertEqual([(r["action"], r["asset_id"], r["user"], r["seq"]) for r in records], [
            ("REGISTER", "AST-P000001", "alex", 1),
            ("SET_STATUS", "AST-P000001", "alex", 2),
            ("EDIT", "AST-P000001", "sam", 3),
            ("DELETE", "AST-P000001", None, 4),  # from the GUI or command line, which have no login
        ])
        self.assertEqual(records[0]["request_id"], "req-Dell laptop")
        self.assertEqual(records[3]["request_id"], "del-1")
        self.assertTrue(all(r["server"] == "primary" for r in records))
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r["time"]) for r in records))

    def test_requests_that_change_nothing_are_not_recorded(self):
        node.process_request(self.register_request())
        records = self.audited(
            self.register_request(),  # a retry, which returns the same asset
            self.register_request("Dell laptop", request_id="other"),  # a duplicate
            {"action": "SET_STATUS", "asset_id": "AST-P000001", "status": common.IN_USE},  # already true
            {"action": "EDIT", "asset_id": "AST-P000001", "changes": {"location": "B"}, "expected_version": 9},
            {"action": "DELETE", "asset_id": "AST-NOPE"},
            {"action": "LIST"},
        )
        self.assertEqual(records, [])

    def test_a_change_that_fails_to_save_is_not_recorded(self):
        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)

        with mock.patch.object(node, "audit") as audit, self.assertRaises(OSError):
            node.process_request(self.register_request())

        audit.assert_not_called()
        self.assertEqual(node.assets, [])

    def test_a_bad_user_field_is_refused(self):
        for user in (7, "", "x" * (node.MAX_USER_LENGTH + 1)):
            with self.subTest(user=user):
                reply = node.process_request(self.register_request(user=user))
                self.assertEqual(reply["code"], common.BAD_REQUEST)
        self.assertEqual(node.assets, [])

    def test_changes_copied_to_the_backup_are_not_recorded_twice(self):
        node.accepts_writes = False

        with mock.patch.object(node, "audit") as audit:
            self.assertEqual(server.apply_change({"seq": 1, "asset": make_record()})["status"], "OK")

        audit.assert_not_called()

    def test_a_typed_line_break_stays_inside_its_record(self):
        with self.assertLogs(common.audit_logger, "INFO") as logs:
            common.audit("LOGIN_FAILED", user="evil\n{\"action\": \"FAKE\"}")
        self.assertEqual(len(logs.records[0].getMessage().splitlines()), 1)


class TimestampTest(NodeTestCase):
    """The server records when each asset was created, last changed and deleted, in UTC."""

    FORMAT = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"

    def at(self, timestamp: str):
        """Make the server's clock say `timestamp`."""
        return mock.patch.object(node, "utc_timestamp", return_value=timestamp)

    def test_a_new_asset_gets_both_times_from_the_server(self):
        sent = make_asset(created_at="1999-01-01T00:00:00Z", updated_at="1999-01-01T00:00:00Z", deleted_at="x")

        stored = node.register_asset(node.validate_asset(sent), None)["stored"]

        self.assertRegex(stored["created_at"], self.FORMAT)
        self.assertEqual(stored["created_at"], stored["updated_at"])
        self.assertNotEqual(stored["created_at"], "1999-01-01T00:00:00Z")
        self.assertNotIn("deleted_at", stored)

    def test_the_real_clock_is_utc(self):
        stamp = node.utc_timestamp()
        self.assertRegex(stamp, self.FORMAT)
        now = datetime.datetime.now(datetime.timezone.utc)
        parsed = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        self.assertLess(abs((now - parsed).total_seconds()), 5)

    def test_a_change_moves_only_the_updated_time(self):
        with self.at("2026-01-01T09:00:00Z"):
            asset_id = node.register_asset(node.validate_asset(make_asset()), None)["stored"]["asset_id"]
        with self.at("2026-01-02T10:30:00Z"):
            changed = node.set_asset_status(asset_id, common.NOT_IN_USE)["stored"]

        self.assertEqual(changed["created_at"], "2026-01-01T09:00:00Z")
        self.assertEqual(changed["updated_at"], "2026-01-02T10:30:00Z")

    def test_a_deletion_time_is_saved_and_copied_to_the_backup(self):
        copies = []
        node.on_change = copies.append
        asset_id = node.register_asset(node.validate_asset(make_asset()), None)["stored"]["asset_id"]

        with self.at("2026-03-04T05:06:07Z"):
            node.delete_asset(asset_id)

        self.assertEqual(node.load_data()["deleted_at"], {asset_id: "2026-03-04T05:06:07Z"})
        self.assertEqual(copies[-1]["deleted_at"], "2026-03-04T05:06:07Z")

    def test_the_backup_keeps_the_primarys_deletion_time(self):
        node.accepts_writes = False
        server.apply_change({"seq": 1, "asset": make_record("AST-P000001")})

        with self.at("2030-12-31T23:59:59Z"):
            server.apply_change({"seq": 2, "deleted": "AST-P000001", "request_id": None,
                                 "deleted_at": "2026-03-04T05:06:07Z"})

        self.assertEqual(node.deleted_at, {"AST-P000001": "2026-03-04T05:06:07Z"})

    def test_deletion_times_survive_a_handover_and_merge(self):
        node.deleted_at["AST-B000001"] = "2026-03-04T05:06:07Z"
        node.deleted_ids["AST-B000001"] = None
        handed = server.hand_over({})
        node.deleted_ids.clear()
        node.deleted_at.clear()

        server.merge_state([], handed["deleted"], handed["deleted_at"])

        self.assertEqual(node.deleted_at, {"AST-B000001": "2026-03-04T05:06:07Z"})

    def test_installed_state_includes_deletion_times(self):
        server.install_state({"assets": [], "deleted": {"AST-P000009": None},
                              "deleted_at": {"AST-P000009": "2026-03-04T05:06:07Z"}, "seq": 3})

        self.assertEqual(node.load_data()["deleted_at"], {"AST-P000009": "2026-03-04T05:06:07Z"})

    def test_files_from_before_timestamps_still_load(self):
        with open(node.ASSETS_FILE, "w", encoding="utf-8") as file:
            json.dump({"seq": 2, "assets": [make_record()], "deleted": {"AST-P000002": None}}, file)

        data = node.load_data()

        self.assertEqual(data["deleted_at"], {})
        self.assertEqual(node.load_assets(), ([make_record()], {"AST-P000002": None}, 2))


class LoginTest(NodeTestCase):
    """Clients without the shared secret (gui.py) log in with an account and send a token."""

    @classmethod
    def setUpClass(cls):
        cls.listener = node.create_listening_socket("127.0.0.1", 0)
        cls.address = cls.listener.getsockname()
        threading.Thread(target=node.run_server, args=(cls.listener,), daemon=True).start()

    def setUp(self):
        super().setUp()
        # Accounts live in a temporary file. A low cost keeps the tests quick.
        users = {}
        for name, role in (("alex", "admin"), ("sam", "user")):
            salt = "ab" * 16
            users[name] = {"salt": salt, "iterations": 1000, "role": role,
                           "hash": accounts.hash_password(f"{name} password", salt, 1000)}
        path = os.path.join(self.temp_dir.name, "users.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(users, file)
        patcher = mock.patch.object(accounts, "USERS_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        accounts.failed_logins, accounts.locked_until = 0, 0.0
        node.assets.append(make_record())

    def ask(self, request: dict, secret: bool = False) -> dict:
        """Send a request as a client would. Without `secret` it carries no shared secret."""
        request = {**request, "protocol_version": common.PROTOCOL_VERSION}
        if secret:
            request["auth"] = common.shared_secret()
        with common.connect(self.address, timeout=5) as sock:
            common.send_message(sock, request)
            return common.receive_message(sock)

    def login(self, name: str = "alex", password: Optional[str] = None) -> dict:
        return self.ask({"action": "LOGIN", "username": name, "password": password or f"{name} password"})

    def test_ping_needs_no_login_and_says_where_the_servers_are(self):
        with mock.patch.object(common, "SERVERS", [("10.0.0.1", 65432), ("10.0.0.2", 65433)]):
            reply = self.ask({"action": "PING"})
        self.assertEqual(reply["servers"], [["10.0.0.1", 65432], ["10.0.0.2", 65433]])
        self.assertNotIn("assets", reply)

    def test_everything_else_still_needs_the_secret_or_a_token(self):
        self.assertEqual(self.ask({"action": "LIST"})["code"], common.UNAUTHORIZED)
        self.assertEqual(self.ask({"action": "LIST", "token": "nonsense"})["code"], common.UNAUTHORIZED)
        self.assertEqual(self.ask({"action": "LIST"}, secret=True)["status"], "OK")

    def test_a_correct_login_gives_a_token_that_works_without_the_secret(self):
        reply = self.login()
        self.assertEqual((reply["status"], reply["username"], reply["role"]), ("OK", "alex", "admin"))
        listing = self.ask({"action": "LIST", "token": reply["token"]})
        self.assertEqual(len(listing["assets"]), 1)

    def test_wrong_password_and_unknown_user_get_the_same_refusal(self):
        wrong = self.login("alex", "not it")
        unknown = self.login("nobody", "not it")
        self.assertEqual(wrong["code"], common.UNAUTHORIZED)
        self.assertEqual(wrong["detail"], unknown["detail"])

    def test_too_many_failed_logins_lock_everyone_out_for_a_while(self):
        for _ in range(accounts.FAILED_LOGIN_LIMIT):
            self.login("alex", "not it")
        self.assertIn("Too many", self.login()["detail"])

    def test_a_changed_token_is_refused(self):
        token = self.login("sam")["token"]
        name, expires, signature = token.split(":")
        for bad in (f"alex:{expires}:{signature}", f"{name}:{int(expires) + 99999}:{signature}",
                    f"{name}:{expires}:{'0' * 64}", f"{name}:{expires}"):
            with self.subTest(bad):
                self.assertEqual(self.ask({"action": "LIST", "token": bad})["code"], common.UNAUTHORIZED)

    def test_an_expired_token_is_refused(self):
        token = self.login()["token"]
        with mock.patch("accounts.time.time", return_value=time.time() + accounts.TOKEN_SECONDS + 1):
            self.assertEqual(self.ask({"action": "LIST", "token": token})["code"], common.UNAUTHORIZED)

    def test_a_token_stops_working_when_its_account_is_removed(self):
        token = self.login("sam")["token"]
        with open(accounts.USERS_FILE, "w", encoding="utf-8") as file:
            json.dump({}, file)
        self.assertEqual(self.ask({"action": "LIST", "token": token})["code"], common.UNAUTHORIZED)

    def test_only_an_admin_can_delete_or_approve(self):
        user, admin = self.login("sam")["token"], self.login("alex")["token"]
        request = {"action": "DELETE", "asset_id": "AST-P000001"}
        self.assertEqual(self.ask({**request, "token": user})["code"], common.FORBIDDEN)
        self.assertEqual(self.ask({"action": "APPROVE", "asset_id": "AST-P000001", "token": user})["code"],
                         common.FORBIDDEN)
        self.assertEqual(len(node.assets), 1)
        self.assertEqual(self.ask({**request, "token": admin})["status"], "OK")
        self.assertEqual(node.assets, [])

    def test_a_user_can_change_assets_and_the_audit_user_is_the_account_not_the_claim(self):
        token = self.login("sam")["token"]
        request = {"action": "SET_STATUS", "asset_id": "AST-P000001", "status": "Not In Use",
                   "token": token, "user": "someone else"}
        seen = []
        with mock.patch.object(node, "set_asset_status",
                               side_effect=lambda *args: seen.append(node.current.request["user"]) or {"status": "OK"}):
            self.assertEqual(self.ask(request)["status"], "OK")
        self.assertEqual(seen, ["sam"])

    def test_a_token_cannot_run_server_to_server_actions(self):
        token = self.login()["token"]
        for action in ("STOP", "HANDOVER", "INSTALL_STATE"):
            with self.subTest(action):
                self.assertEqual(self.ask({"action": action, "token": token})["code"], common.FORBIDDEN)

    def test_a_server_answers_discovery_broadcasts(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        threading.Thread(target=node.run_discovery, args=(sock, 65432), daemon=True).start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(3)
            client.sendto(b"something else", sock.getsockname())  # ignored
            client.sendto(common.DISCOVERY_REQUEST, sock.getsockname())
            reply = json.loads(client.recvfrom(1024)[0])
        self.assertEqual(reply, {"service": common.SERVICE_NAME, "role": "primary", "port": 65432})


class StartSyncTest(unittest.TestCase):
    """The primary keeps trying to sync with the later servers for a short time as it starts."""

    def setUp(self):
        self.addCleanup(setattr, server, "START_SYNC_RETRY_SECONDS", server.START_SYNC_RETRY_SECONDS)
        server.START_SYNC_RETRY_SECONDS = 0
        self.addCleanup(server.followers_in_sync.clear)
        server.followers_in_sync.update({1: False, 2: False})

    def fake_catch_up(self, *results):
        """A stand-in for server.catch_up() that gives these results in turn, recording
        each one as the real one does."""
        results = iter(results)

        def catch_up(index):
            server.followers_in_sync[index] = next(results)
            return server.followers_in_sync[index]
        return catch_up

    def test_it_retries_until_every_later_server_answers(self):
        # Server 1 answers at once. Server 2 answers on its third try.
        with mock.patch.object(server, "catch_up", side_effect=self.fake_catch_up(True, False, False, True)) as catch_up:
            self.assertTrue(server.sync_at_start())
        self.assertEqual([call.args[0] for call in catch_up.call_args_list], [1, 2, 2, 2])

    def test_it_gives_up_and_starts_without_them_if_one_never_answers(self):
        self.addCleanup(setattr, server, "START_SYNC_SECONDS", server.START_SYNC_SECONDS)
        server.START_SYNC_SECONDS = 0.2

        with mock.patch.object(server, "catch_up", side_effect=lambda index: False), \
                self.assertLogs(node.logger, "INFO") as logs:
            started = time.monotonic()
            self.assertFalse(server.sync_at_start())

        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("starting without them", logs.output[-1])


class ServerOptionTest(unittest.TestCase):
    """server.py --backup N must name an entry in the server list."""

    def test_a_position_the_list_does_not_have_is_refused(self):
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [unused_address(), unused_address()]
        for options in (["--backup", "2"], ["--backup", "-1"], ["--backup", "one"]):
            with self.subTest(options), self.assertRaises(SystemExit),                     contextlib.redirect_stderr(io.StringIO()):
                server.main(options)

    def test_each_server_has_its_own_name_data_file_and_id_letter(self):
        names = [server.name_of(index) for index in range(4)]
        self.assertEqual(names, ["primary", "backup", "backup2", "backup3"])
        files = [os.path.basename(server.data_file(index)) for index in range(3)]
        self.assertEqual(files, ["assets.json", "assets_backup.json", "assets_backup2.json"])
        self.assertEqual(server.ID_LETTERS[:3], "PBC")
        self.assertEqual(len(set(server.ID_LETTERS)), len(server.ID_LETTERS))  # no letter twice
        self.assertEqual(len(server.ID_LETTERS), common.MAX_SERVERS)  # one for every server allowed

    def test_there_is_no_backup_without_a_second_entry(self):
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [unused_address()]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            server.main(["--backup", "1"])


class DistributedTestCase(unittest.TestCase):
    """Helpers for running servers as real, separate programs, and stopping them."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_path = temp_dir.name

        self.primary = unused_address()
        self.backup = unused_address()
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [self.primary, self.backup]

    def start_program(self, index: int, settings: str = "") -> subprocess.Popen:
        """Start the server at this position in the list (0 for the primary, 1 for the
        backup) as its own process, using the test's addresses and its own data file.
        `settings` is extra code run just before it starts."""
        code = (
            "import common\n"
            f"common.SERVERS = {common.SERVERS!r}\n"
            f"common.LOG_FOLDER = {self.temp_path!r}\n"
            "import server as program\n"
            f"program.ASSETS_FILE = {os.path.join(self.temp_path, f'server{index}.json')!r}\n"
            f"{settings}\n"
            f"program.main(['--backup', '{index}'])\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def start_primary(self) -> subprocess.Popen:
        return self.start_program(0)

    def start_backup(self) -> subprocess.Popen:
        return self.start_program(1)

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

class DistributedTest(DistributedTestCase):
    """Runs the primary and backup as real, separate programs, and stops them."""

    def test_a_backup_that_starts_just_after_the_primary_gets_every_change(self):
        # The primary's first attempt to sync finds nothing listening. It keeps trying,
        # and doesn't serve clients until it has finished, so the first change is already
        # copied. Its retry time is lengthened here, so the test doesn't depend on timing.
        # A failed connection can take 2 seconds on Windows, so the backup starts later.
        self.start_program(0, "program.START_SYNC_SECONDS = 10")
        time.sleep(2.5)
        self.start_backup()
        self.wait_until(lambda: common.request_from(self.backup, {"action": "PING"}), "saw the backup start")

        self.assertEqual(register(make_asset("First"))["status"], "OK")
        self.assertIn("First", self.descriptions_on(self.backup))

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

    def test_an_edit_reaches_the_backup_immediately(self):
        self.start_backup()
        self.start_primary()
        self.wait_until_in_sync()
        asset_id = register(make_asset("Before"))["stored"]["asset_id"]

        reply = common.send_request({"action": "EDIT", "asset_id": asset_id, "expected_version": 1,
                                     "changes": {"description": "After", "status": common.NOT_IN_USE}})

        self.assertEqual(reply["status"], "OK")
        on_backup = self.assets_on(self.backup)
        self.assertEqual(on_backup, [reply["stored"]])

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


class ThreeServerTest(DistributedTestCase):
    """The same rules with three servers: the order of the list is the order of succession."""

    def setUp(self):
        super().setUp()
        self.backup2 = unused_address()
        common.SERVERS = [self.primary, self.backup, self.backup2]

    def accepts_writes(self, address: tuple) -> bool:
        return common.request_from(address, {"action": "PING"})["accepts_writes"]

    def wait_until_all_hold(self, addresses: list, descriptions: list) -> None:
        self.wait_until(lambda: all(self.descriptions_on(address) == descriptions for address in addresses),
                        f"saw {descriptions} on every server")

    def test_each_server_takes_over_in_turn_and_all_catch_up_when_they_return(self):
        primary = self.start_program(0)
        backup = self.start_program(1)
        self.start_program(2)
        servers = [self.primary, self.backup, self.backup2]
        self.wait_until(lambda: all(not self.accepts_writes(address) for address in servers[1:]),
                        "saw both backups on standby")
        self.assertEqual(register(make_asset("First"))["served_by"], "primary")
        self.wait_until_all_hold(servers, ["First"])

        # The primary fails. The first backup takes over, and the second stays on standby,
        # because a server before it (the first backup) is still answering.
        self.stop(primary)
        self.wait_until(lambda: self.accepts_writes(self.backup), "saw the first backup take over")
        self.assertFalse(self.accepts_writes(self.backup2))
        self.assertEqual(register(make_asset("Second"))["served_by"], "backup")
        self.wait_until_all_hold(servers[1:], ["First", "Second"])

        # The first backup fails too. Now no server before the second backup answers.
        self.stop(backup)
        self.wait_until(lambda: self.accepts_writes(self.backup2), "saw the second backup take over")
        self.assertEqual(register(make_asset("Third"))["served_by"], "backup2")

        # Both come back. The primary takes control again, and every server ends up with
        # every change, with only the primary accepting changes.
        self.start_program(0)
        self.start_program(1)
        self.wait_until_all_hold(servers, ["First", "Second", "Third"])
        self.wait_until(lambda: [self.accepts_writes(address) for address in servers] == [True, False, False],
                        "saw the primary back in charge")


if __name__ == "__main__":
    unittest.main()

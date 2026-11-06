"""
Tests for fill.py, which adds made-up assets.

    python -m unittest -v tests.test_fill

These run real servers as separate programs on spare ports, with temporary data files,
so your data is never touched.
"""

import argparse
import contextlib
import io
import time
import unittest
from unittest import mock

import common
from tools import fill
from tests.test_demo import ServerTestCase  # starts real servers on spare ports


class MadeUpAssetTest(unittest.TestCase):
    def test_each_asset_has_valid_details_that_match_its_category(self):
        for _ in range(200):
            asset = fill.made_up_asset()
            self.assertIn(asset["status"], common.STATUSES)
            item, serial = asset["description"].split(", SN ")
            self.assertIn(item, fill.ITEMS_BY_CATEGORY[asset["category"]])
            self.assertRegex(serial, r"^[A-Z0-9]{8}$")
            self.assertRegex(asset["location"], r"^.+ - .+$")

    def test_the_assets_are_all_different_items(self):
        # The server refuses a second asset with the same description, category and
        # location, so a run of hundreds must never repeat one.
        identities = {(a["description"], a["category"], a["location"])
                      for a in (fill.made_up_asset() for _ in range(5000))}
        self.assertEqual(len(identities), 5000)


class CountOptionTest(unittest.TestCase):
    def test_a_positive_whole_number_is_accepted(self):
        self.assertEqual(fill.positive_number("200"), 200)

    def test_zero_negative_and_non_numbers_are_refused(self):
        for text in ("0", "-5", "abc", "2.5"):
            with self.assertRaises(argparse.ArgumentTypeError):
                fill.positive_number(text)


class FillTest(ServerTestCase):
    def held_by(self, address: tuple) -> list:
        return common.request_from(address, {"action": "LIST"})["assets"]

    def test_it_adds_the_number_asked_for_and_the_backup_gets_them_too(self):
        self.start()
        self.start(1)
        self.wait_until_answering(self.servers[0])
        self.wait_until_answering(self.servers[1])

        with contextlib.redirect_stdout(io.StringIO()):
            added = fill.fill(200)

        self.assertEqual(added, 200)
        self.assertEqual(len(self.held_by(self.servers[0])), 200)
        # The primary copies each change to the backup, so give it a moment to catch up.
        deadline = time.monotonic() + 10
        while len(self.held_by(self.servers[1])) < 200 and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(len(self.held_by(self.servers[1])), 200)

    def test_it_explains_what_to_do_if_the_servers_are_not_running(self):
        output = io.StringIO()
        with mock.patch("sys.argv", ["fill.py", "--count", "3"]), contextlib.redirect_stdout(output):
            fill.main()

        self.assertIn("Could not add the assets", output.getvalue())
        self.assertIn("python start.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()

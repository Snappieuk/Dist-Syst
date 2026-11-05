"""
Tests for what the client programs offer the user.

    python -m unittest -v test_clients

These don't need a server. The GUI tests are skipped if no window can be opened.
"""

import contextlib
import io
import os
import re
import unittest
from unittest import mock

import common
import send

FOLDER = os.path.dirname(os.path.abspath(__file__))


def make_gui():
    """Create the GUI without showing it or contacting a server, or skip the test."""
    try:
        import tkinter as tk
        import gui
        root = tk.Tk()
    except Exception as exc:  # no display, or Tk not installed
        raise unittest.SkipTest(f"No window can be opened here: {exc}")
    root.withdraw()
    with mock.patch.object(gui.AssetManagementApp, "refresh_assets"):
        app = gui.AssetManagementApp(root)
    return root, app


class SendStatusTest(unittest.TestCase):
    def choose(self, *answers) -> tuple:
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(output):
            return send.choose_status(), output.getvalue()

    def test_each_number_gives_its_status(self):
        for number, status in enumerate(common.STATUSES, start=1):
            with self.subTest(status):
                self.assertEqual(self.choose(str(number))[0], status)

    def test_only_the_two_statuses_are_offered(self):
        _, output = self.choose("1")
        self.assertIn("1. In Use", output)
        self.assertIn("2. Not In Use", output)
        self.assertNotIn("3.", output)

    def test_anything_else_is_asked_again(self):
        status, output = self.choose("", "Broken", "0", "3", "2")
        self.assertEqual(status, common.NOT_IN_USE)
        self.assertEqual(output.count("Please type a number"), 4)


class GuiStatusTest(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_gui()
        self.addCleanup(self.root.destroy)

    def test_the_status_box_is_a_read_only_list_of_the_two_statuses(self):
        box = self.app.fields["status"]
        self.assertEqual(str(box.cget("state")), "readonly")
        self.assertEqual(tuple(box.cget("values")), common.STATUSES)
        self.assertEqual(box.get(), common.IN_USE)

    def test_clearing_the_form_resets_the_status(self):
        self.app.fields["status"].set(common.NOT_IN_USE)
        self.app._finish_registration()
        self.assertEqual(self.app.fields["status"].get(), common.IN_USE)


class WebPageStatusTest(unittest.TestCase):
    def test_the_add_form_offers_only_the_two_statuses(self):
        with open(os.path.join(FOLDER, "static", "index.html"), encoding="utf-8") as file:
            page = file.read()
        add_form = re.search(r'<form id="add-form".*?</form>', page, re.S).group(0)
        select = re.search(r'<select name="status">(.*?)</select>', add_form, re.S).group(1)

        self.assertEqual(tuple(re.findall(r"<option>(.*?)</option>", select)), common.STATUSES)


if __name__ == "__main__":
    unittest.main()

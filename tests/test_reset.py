"""
Tests for reset.py.

    python -m unittest discover -v tests            (every test, from the project folder)
    python -m unittest discover -v tests -p test_reset.py  (just these)
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import sys

# The code under test is in the project folder and its tools and clients folders.
TESTS = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(TESTS)
for folder in (TESTS, PROJECT, os.path.join(PROJECT, "tools"), os.path.join(PROJECT, "clients")):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import reset
import server
import users
from test_system import ServerTestCase, make_asset, make_config


class ResetTest(unittest.TestCase):
    """Resetting with no servers running."""

    def setUp(self) -> None:
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config = make_config(self.folder)  # makes the certificate and key
        users.add_user(self.config, "admin", "admin-password", "admin", iterations=1000)
        # Data files and audit logs, as a server would leave them.
        node = server.AssetServer(self.config, "primary")
        with node.lock:
            node.save()
        node.audit("server_started")
        self.keep = os.path.join(self.folder, "notes.txt")  # something that isn't the system's
        with open(self.keep, "w") as file:
            file.write("mine")

    def run_reset(self, typed: str = "RESET", confirm: bool = True) -> bool:
        output = io.StringIO()
        with mock.patch("builtins.input", return_value=typed), contextlib.redirect_stdout(output):
            result = reset.reset(self.config, confirm)
        self.output = output.getvalue()
        return result

    def remaining(self) -> list:
        return sorted(os.listdir(self.folder))

    def test_confirming_deletes_every_generated_file_and_nothing_else(self):
        self.assertTrue(self.run_reset())
        self.assertEqual(self.remaining(), ["config.json", "notes.txt"])

    def test_it_lists_what_will_be_lost_before_asking(self):
        self.run_reset(typed="no")
        for name in ("assets_primary.json", "audit_primary.log", "users.json", "secret.key", "server.crt", "server.key"):
            self.assertIn(name, self.output)

    def test_anything_but_the_exact_word_deletes_nothing(self):
        before = self.remaining()
        for typed in ("", "reset", "yes", "RESET!"):
            self.assertFalse(self.run_reset(typed=typed))
        self.assertEqual(self.remaining(), before)

    def test_yes_skips_the_confirmation(self):
        self.assertTrue(self.run_reset(typed="ignored", confirm=False))
        self.assertEqual(self.remaining(), ["config.json", "notes.txt"])

    def test_a_fresh_install_has_nothing_to_reset(self):
        self.run_reset()
        self.assertTrue(self.run_reset())
        self.assertIn("Nothing to reset", self.output)

    def test_setting_up_again_makes_a_working_system(self):
        self.run_reset()
        with mock.patch("getpass.getpass", side_effect=["short", "short", "new-admin-password", "new-admin-password"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(reset.set_up_again(self.config))
        self.assertTrue(os.path.exists(os.path.join(self.folder, "server.crt")))
        self.assertEqual(users.load_users(self.config)["admin"]["role"], "admin")
        server.AssetServer(self.config, "primary")  # starts without complaint


class ResetWhileRunningTest(ServerTestCase):
    """A running server would just save its data again, so the reset must refuse."""

    def test_it_refuses_while_a_server_is_running(self):
        self.release_port("backup")  # reset.py would take the tests' stand-in for a server
        self.register(make_asset())
        output = io.StringIO()
        with mock.patch("builtins.input", return_value="RESET"), contextlib.redirect_stdout(output):
            self.assertFalse(reset.reset(self.config))
        self.assertIn("Stop the primary server", output.getvalue())
        self.assertTrue(os.path.exists(self.server.data_file))


if __name__ == "__main__":
    unittest.main()

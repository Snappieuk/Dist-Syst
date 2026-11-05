"""
Tests for first-time setup.

    python -m unittest -v test_setup

Every file these use is in a temporary folder, so your real data and secrets are never
touched, and a test that went wrong could not delete them.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import common
import setup
import web


class SetupTestCase(unittest.TestCase):
    """Points every generated file at a temporary folder."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.folder = temp_dir.name

        self.patch_value(common, "SECRET_FILE", self.path("secret.key"))
        self.patch_value(common, "_secret", None)
        self.patch_value(web, "PASSWORD_FILE", self.path("web_password.json"))
        self.patch_value(web, "HASH_ITERATIONS", 1000)  # the real number is slow on purpose

    def path(self, name: str) -> str:
        return os.path.join(self.folder, name)

    def patch_value(self, module, name: str, value) -> None:
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    def create(self, *names) -> None:
        for name in names:
            with open(self.path(name), "w", encoding="utf-8") as file:
                file.write("[]")

    def run_quietly(self, function, passwords=(), answers=()) -> tuple:
        """Run something with these typed answers, returning its result and its output."""
        output = io.StringIO()
        with mock.patch("getpass.getpass", side_effect=list(passwords)), \
                mock.patch("builtins.input", side_effect=list(answers)), \
                contextlib.redirect_stdout(output):
            result = function()
        return result, output.getvalue()


class FirstTimeSetupTest(SetupTestCase):
    def test_a_new_install_is_not_set_up(self):
        self.assertFalse(setup.is_set_up())

    def test_setup_creates_the_secret_and_the_chosen_password(self):
        result, _ = self.run_quietly(setup.run_setup, ["my password", "my password"])

        self.assertTrue(result)
        self.assertTrue(setup.is_set_up())
        web.password_record = web.load_or_create_password()
        self.addCleanup(setattr, web, "password_record", None)
        self.assertTrue(web.password_matches("my password"))

    def test_a_bad_password_can_be_tried_again(self):
        result, output = self.run_quietly(setup.run_setup, ["short", "my password", "different", "my password", "my password"])

        self.assertTrue(result)
        self.assertIn("at least", output)
        self.assertIn("did not match", output)

    def test_setup_gives_up_after_repeated_bad_passwords(self):
        result, _ = self.run_quietly(setup.run_setup, ["short"] * setup.PASSWORD_ATTEMPTS)

        self.assertFalse(result)
        self.assertFalse(setup.is_set_up())

    def test_setup_does_not_replace_an_existing_password(self):
        web.save_password("original password")
        before = web.load_or_create_password()

        result, _ = self.run_quietly(setup.run_setup)

        self.assertTrue(result)
        self.assertEqual(web.load_or_create_password(), before)

    def test_running_it_when_already_set_up_only_says_so(self):
        self.run_quietly(setup.run_setup, ["my password", "my password"])

        _, output = self.run_quietly(setup.main)

        self.assertIn("Already set up", output)

    def test_cancelling_is_reported_clearly(self):
        with mock.patch("getpass.getpass", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()) as output:
            setup.main()

        self.assertIn("not finished", output.getvalue())

    def test_no_keyboard_means_no_questions(self):
        with mock.patch("sys.stdin", None):
            self.assertFalse(setup.can_ask_questions())
        with mock.patch("sys.stdin", io.StringIO()):
            self.assertFalse(setup.can_ask_questions())


if __name__ == "__main__":
    unittest.main()

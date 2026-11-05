"""
Tests for first-time setup and the full reset.

    python -m unittest -v test_setup

Every file these use is in a temporary folder, so your real data, accounts and secrets
are never touched, and a test that went wrong could not delete them.
"""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import backup
import common
import node
import server
import setup
import web


def unused_address() -> tuple:
    """Find an address with nothing listening on it."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()


class SetupTestCase(unittest.TestCase):
    """Points every generated file at a temporary folder."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.folder = temp_dir.name

        self.patch_value(common, "SECRET_FILE", self.path("secret.key"))
        self.patch_value(common, "LOG_FOLDER", self.path("logs"))
        self.patch_value(common, "_secret", None)
        self.patch_value(web, "PASSWORD_FILE", self.path("web_password.json"))
        self.patch_value(web, "USERS_FILE", self.path("web_users.json"))
        self.addCleanup(web.users.clear)
        self.patch_value(web, "HASH_ITERATIONS", 1000)  # the real number is slow on purpose
        self.patch_value(server, "ASSETS_FILE", self.path("assets.json"))
        self.patch_value(backup, "ASSETS_FILE", self.path("assets_backup.json"))
        self.patch_value(common, "SERVERS", [unused_address(), unused_address()])
        self.patch_value(web, "WEB_ADDRESS", unused_address())
        self.patch_value(setup, "PORT_CHECK_SECONDS", 0.2)

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

    def test_setup_creates_the_secret_and_the_chosen_account(self):
        result, _ = self.run_quietly(setup.run_setup, ["my password", "my password"], ["alex"])

        self.assertTrue(result)
        self.assertTrue(setup.is_set_up())
        web.users.clear()
        self.run_quietly(web.load_accounts)
        self.assertTrue(web.password_matches(web.users["alex"], "my password"))

    def test_bad_details_can_be_tried_again(self):
        passwords = ["short", "short", "my password", "different", "my password", "my password"]

        result, output = self.run_quietly(setup.run_setup, passwords, ["alex"] * setup.ACCOUNT_ATTEMPTS)

        self.assertTrue(result)
        self.assertIn("at least", output)
        self.assertIn("did not match", output)

    def test_setup_gives_up_after_repeated_bad_details(self):
        result, _ = self.run_quietly(
            setup.run_setup, ["short"] * 2 * setup.ACCOUNT_ATTEMPTS, ["alex"] * setup.ACCOUNT_ATTEMPTS
        )

        self.assertFalse(result)
        self.assertFalse(setup.is_set_up())

    def test_setup_does_not_replace_an_existing_account(self):
        web.add_account("alex", "original password")
        before = dict(web.users)

        result, _ = self.run_quietly(setup.run_setup)

        self.assertTrue(result)
        self.assertEqual(web.read_users_file(), before)

    def test_an_older_password_counts_as_being_set_up(self):
        self.create("web_password.json")

        result, _ = self.run_quietly(setup.run_setup)

        self.assertTrue(result)
        self.assertTrue(setup.is_set_up())

    def test_running_it_when_already_set_up_only_says_so(self):
        self.run_quietly(setup.run_setup, ["my password", "my password"], ["alex"])

        _, output = self.run_quietly(setup.main)

        self.assertIn("Already set up", output)

    def test_cancelling_is_reported_clearly(self):
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()) as output:
            setup.main()

        self.assertIn("not finished", output.getvalue())

    def test_no_keyboard_means_no_questions(self):
        with mock.patch("sys.stdin", None):
            self.assertFalse(setup.can_ask_questions())
        with mock.patch("sys.stdin", io.StringIO()):
            self.assertFalse(setup.can_ask_questions())


class NoKeyboardTest(unittest.TestCase):
    """Uses real processes, because the operating system's answer is what matters here."""

    CHECK = "import setup; print(setup.can_ask_questions())"

    def ask_with_input(self, stdin) -> str:
        folder = os.path.dirname(os.path.abspath(__file__))
        result = subprocess.run(
            [sys.executable, "-c", self.CHECK], cwd=folder, stdin=stdin,
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()

    def test_the_null_device_is_not_a_keyboard(self):
        # Windows reports this as a terminal, which once made start.py wait forever.
        self.assertEqual(self.ask_with_input(subprocess.DEVNULL), "False")

    def test_a_pipe_is_not_a_keyboard(self):
        self.assertEqual(self.ask_with_input(subprocess.PIPE), "False")


class ResetTest(SetupTestCase):
    ALL_FILES = (
        "assets.json",
        "assets_backup.json",
        "secret.key",
        "web_password.json",
        "web_users.json",
        "assets.json.tmp",
        "assets_backup.json.tmp",
        "web_password.json.tmp",
        "web_users.json.tmp",
    )

    def setUp(self):
        super().setUp()
        self.create(*self.ALL_FILES)
        self.create("my_notes.txt", "server.py")
        os.mkdir(self.path("static"))

    def remaining(self) -> set:
        return set(os.listdir(self.folder))

    def test_confirming_deletes_only_what_the_system_generated(self):
        result, output = self.run_quietly(setup.reset, answers=[setup.RESET_WORD])

        self.assertTrue(result)
        self.assertIn("Reset complete", output)
        self.assertEqual(self.remaining(), {"my_notes.txt", "server.py", "static"})

    def test_the_logs_are_deleted_too(self):
        os.mkdir(self.path("logs"))
        self.create("logs/primary.log", "logs/primary.log.1", "logs/web.log", "logs/start.log",
                    "logs/primary-audit.log", "logs/web-audit.log.2")

        _, output = self.run_quietly(setup.reset, answers=[setup.RESET_WORD])

        self.assertIn("primary.log.1", output)
        self.assertIn("web-audit.log.2", output)
        self.assertNotIn("logs", self.remaining())

    def test_other_files_in_the_log_folder_are_kept(self):
        os.mkdir(self.path("logs"))
        self.create("logs/backup.log", "logs/my_notes.txt")

        self.run_quietly(setup.reset, answers=[setup.RESET_WORD])

        self.assertEqual(os.listdir(self.path("logs")), ["my_notes.txt"])

    def test_anything_but_the_exact_word_deletes_nothing(self):
        for answer in ("", "y", "yes", "reset", "RESET NOW"):
            with self.subTest(answer):
                result, output = self.run_quietly(setup.reset, answers=[answer])
                self.assertFalse(result)
                self.assertIn("Nothing was deleted", output)
                self.assertTrue(set(self.ALL_FILES) <= self.remaining())

    def test_cancelling_deletes_nothing(self):
        for interruption in (KeyboardInterrupt, EOFError):
            with self.subTest(interruption.__name__):
                with mock.patch("sys.argv", ["setup.py", "--reset"]):
                    _, output = self.run_quietly(setup.main, answers=[interruption])
                self.assertIn("Nothing was deleted", output)
                self.assertTrue(set(self.ALL_FILES) <= self.remaining())

    def test_it_says_what_will_be_lost_before_asking(self):
        with open(self.path("assets_backup.json"), "w", encoding="utf-8") as file:
            json.dump({"assets": [{}, {}, {}], "deleted": {}, "seq": 0}, file)

        _, output = self.run_quietly(setup.reset, answers=["no"])

        self.assertIn("3 asset(s)", output)
        self.assertIn("secret.key", output)

    def test_it_refuses_while_any_part_of_the_system_is_running(self):
        for setting, name in (
            ((common, "SERVERS", 0), "server.py"),
            ((common, "SERVERS", 1), "backup.py"),
            ((web, "WEB_ADDRESS", None), "web.py"),
        ):
            with self.subTest(name):
                listener = node.create_listening_socket("127.0.0.1", 0)
                self.addCleanup(listener.close)
                module, attribute, index = setting
                value = getattr(module, attribute)
                self.addCleanup(setattr, module, attribute, value)
                if index is None:
                    setattr(module, attribute, listener.getsockname())
                else:
                    changed = list(value)
                    changed[index] = listener.getsockname()
                    setattr(module, attribute, changed)

                # No answer is given, so asking for confirmation would raise an error.
                result, output = self.run_quietly(setup.reset)

                self.assertFalse(result)
                self.assertIn(name, output)
                self.assertTrue(set(self.ALL_FILES) <= self.remaining())
                setattr(module, attribute, value)
                listener.close()

    def test_a_damaged_data_file_does_not_stop_a_reset(self):
        with open(self.path("assets.json"), "w", encoding="utf-8") as file:
            file.write("{damaged")

        result, _ = self.run_quietly(setup.reset, answers=[setup.RESET_WORD])

        self.assertTrue(result)
        self.assertNotIn("assets.json", self.remaining())

    def test_a_bare_system_has_nothing_to_reset(self):
        self.run_quietly(setup.reset, answers=[setup.RESET_WORD])

        result, output = self.run_quietly(setup.reset)

        self.assertFalse(result)
        self.assertIn("already bare", output)

    def test_a_file_that_cannot_be_deleted_is_reported(self):
        real_remove = os.remove

        def remove_except_secret(path):
            if path.endswith("secret.key"):
                raise PermissionError("in use")
            real_remove(path)

        with mock.patch("os.remove", remove_except_secret):
            _, output = self.run_quietly(setup.reset, answers=[setup.RESET_WORD])

        self.assertIn("not complete", output)
        self.assertIn("secret.key", output)
        self.assertNotIn("Reset complete", output)
        self.assertEqual(self.remaining(), {"my_notes.txt", "server.py", "static", "secret.key"})

    def test_setup_works_again_after_a_reset(self):
        self.run_quietly(setup.reset, answers=[setup.RESET_WORD])
        self.assertFalse(setup.is_set_up())

        result, _ = self.run_quietly(setup.run_setup, ["new password", "new password"], ["alex"])

        self.assertTrue(result)
        self.assertTrue(setup.is_set_up())


if __name__ == "__main__":
    unittest.main()

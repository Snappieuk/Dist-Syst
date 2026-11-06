"""
Tests for what the client programs offer the user.

    python -m unittest -v tests.test_clients

These don't need a server. The GUI tests are skipped if no window can be opened.
"""

import contextlib
import io
import os
import re
import time
import unittest
from unittest import mock

import cli
import common

# The project folder, one above this tests folder.
FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class CommandTest(unittest.TestCase):
    """cli.py runs the command it is given, or asks for one."""

    def run_cli(self, argv: list, answers: list = ()) -> list:
        """Run cli.main() with these arguments and typed answers, returning which
        command functions it called."""
        called = []
        commands = {name: (label, lambda name=name: called.append(name))
                    for name, (label, _) in cli.COMMANDS.items()}
        with mock.patch.object(cli, "COMMANDS", commands), mock.patch("sys.argv", ["cli.py", *argv]),                 mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        return called

    def test_each_command_runs_its_own_function(self):
        for name in ("add", "list", "change"):
            self.assertEqual(self.run_cli([name]), [name])

    def test_without_a_command_it_asks_until_given_a_valid_number(self):
        self.assertEqual(self.run_cli([], answers=["9", "x", "2"]), ["list"])

    def test_an_unknown_command_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(["remove"])


class SendStatusTest(unittest.TestCase):
    def choose(self, *answers) -> tuple:
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(output):
            return cli.choose_status(), output.getvalue()

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


class GuiEditTest(unittest.TestCase):
    ASSET = {"asset_id": "AST-P000001", "description": "Dell laptop", "category": "IT Equipment",
             "location": "Site A", "status": common.IN_USE, "version": 3, "tags": []}

    def setUp(self):
        self.root, self.app = make_gui()
        self.addCleanup(self.root.destroy)
        self.app._show_assets([self.ASSET])
        sender = mock.patch.object(self.app, "_start_request")
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def test_the_window_starts_with_the_assets_details(self):
        _, boxes, _ = self.app.open_edit_window("AST-P000001")

        self.assertEqual({key: box.get() for key, box in boxes.items()},
                         {"description": "Dell laptop", "category": "IT Equipment",
                          "location": "Site A", "status": common.IN_USE})
        self.assertEqual(str(boxes["status"].cget("state")), "readonly")

    def test_saving_sends_only_the_changes_with_the_version_shown(self):
        _, boxes, save = self.app.open_edit_window("AST-P000001")
        boxes["location"].delete(0, "end")
        boxes["location"].insert(0, "Site B")
        boxes["status"].set(common.NOT_IN_USE)

        save()

        self.sent.assert_called_once_with("edit", {
            "action": "EDIT", "asset_id": "AST-P000001", "expected_version": 3,
            "changes": {"location": "Site B", "status": common.NOT_IN_USE},
        })

    def test_saving_with_no_changes_sends_nothing(self):
        _, _, save = self.app.open_edit_window("AST-P000001")
        save()
        self.sent.assert_not_called()

    def test_an_emptied_field_is_not_sent(self):
        _, boxes, save = self.app.open_edit_window("AST-P000001")
        boxes["description"].delete(0, "end")

        with mock.patch("tkinter.messagebox.showwarning") as warning:
            save()

        warning.assert_called_once()
        self.sent.assert_not_called()


class GuiSearchTest(unittest.TestCase):
    MATCH = {"asset_id": "AST-P000002", "description": "Dell monitor", "category": "IT", "location": "B",
             "status": common.NOT_IN_USE, "version": 1, "tags": []}

    def setUp(self):
        self.root, self.app = make_gui()
        self.addCleanup(self.root.destroy)

    def wait_for_result(self) -> None:
        """Wait for the GUI's background request to finish."""
        deadline = time.monotonic() + 5
        while self.app.results.empty():
            self.assertLess(time.monotonic(), deadline, "the request never finished")
            time.sleep(0.05)

    def search_for(self, text: str = "", status: str = None) -> None:
        self.app.search_box.delete(0, "end")
        self.app.search_box.insert(0, text)
        if status is not None:
            self.app.status_filter.set(status)

    def test_with_nothing_chosen_everything_is_listed(self):
        self.assertEqual(self.app.list_request(), {"action": "LIST"})

    def test_the_search_and_status_are_sent_to_the_server(self):
        self.search_for("  dell ", common.NOT_IN_USE)
        self.assertEqual(self.app.list_request(), {"action": "LIST", "query": "dell", "status": common.NOT_IN_USE})

    def test_the_status_filter_is_a_read_only_list(self):
        box = self.app.status_filter
        self.assertEqual(str(box.cget("state")), "readonly")
        self.assertEqual(tuple(box.cget("values")), ("All statuses",) + common.STATUSES)

    def test_clearing_shows_everything_again(self):
        self.search_for("dell", common.IN_USE)
        with mock.patch.object(self.app, "_start_request") as sent:
            self.app.clear_search()
        sent.assert_called_once_with("list", {"action": "LIST"})

    def test_the_table_shows_only_what_the_server_matched(self):
        reply = {"status": "OK", "assets": [self.MATCH], "total": 1, "served_by": "primary"}
        self.search_for("dell")

        with mock.patch("gui.send_request", return_value=reply) as send:
            self.app.refresh_assets()
            self.wait_for_result()
            self.app._process_results()

        send.assert_called_once_with({"action": "LIST", "query": "dell"})
        self.assertEqual(self.app.tree.get_children(), ("AST-P000002",))
        self.assertIn("Showing 1 matching asset(s)", self.app.status_label.cget("text"))

    def test_a_change_refreshes_the_table_with_the_same_search(self):
        changed = {"status": "OK", "stored": dict(self.MATCH, version=2), "served_by": "primary"}
        listing = {"status": "OK", "assets": [], "total": 0, "served_by": "primary"}
        self.search_for("nothing matches")

        with mock.patch("gui.send_request", side_effect=[changed, listing]) as send:
            self.app._start_request("edit", {"action": "EDIT"})
            self.wait_for_result()

        self.assertEqual(send.call_args_list[1].args[0], {"action": "LIST", "query": "nothing matches"})
        _, _, assets, _ = self.app.results.get_nowait()
        self.assertEqual(assets, [])  # the changed asset isn't added when a search hides it


class WebPageStatusTest(unittest.TestCase):
    def test_the_add_form_offers_only_the_two_statuses(self):
        with open(os.path.join(FOLDER, "static", "index.html"), encoding="utf-8") as file:
            page = file.read()
        add_form = re.search(r'<form id="add-form".*?</form>', page, re.S).group(0)
        select = re.search(r'<select name="status">(.*?)</select>', add_form, re.S).group(1)

        self.assertEqual(tuple(re.findall(r"<option>(.*?)</option>", select)), common.STATUSES)


if __name__ == "__main__":
    unittest.main()

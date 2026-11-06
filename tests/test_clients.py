"""
Tests for what the client programs offer the user.

    python -m unittest -v tests.test_clients

These don't need a server. The GUI tests are skipped if no window can be opened.
"""

import ast
import contextlib
import datetime
import io
import json
import os
import re
import socket
import tempfile
import threading
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


class GuiReviewTest(unittest.TestCase):
    """The GUI's "Flagged for review" area and history window."""

    ASSETS = [
        {"asset_id": "AST-B000001", "description": "iPad", "category": "IT", "location": "Site B",
         "status": common.IN_USE, "version": 3, "flagged": True,
         "history": [{"at": "2026-11-06T10:00:00Z", "detail": "Registered", "server": "backup", "user": None}]},
        {"asset_id": "AST-P000001", "description": "Laptop", "category": "IT", "location": "Site A",
         "status": common.IN_USE, "version": 1},
    ]

    def setUp(self):
        self.root, self.app = make_gui()
        self.addCleanup(self.root.destroy)
        self.app._show_assets(self.ASSETS)

    def test_only_flagged_assets_are_listed_and_the_area_hides_when_there_are_none(self):
        self.assertEqual(self.app.review_tree.get_children(), ("AST-B000001",))
        self.assertEqual(self.app.review_frame.winfo_manager(), "pack")
        self.app._show_assets(self.ASSETS[1:])
        self.assertEqual(self.app.review_frame.winfo_manager(), "")

    def test_approving_the_ticked_assets_sends_one_request_each(self):
        self.app.review_all.set(True)
        self.app._tick_all_reviews()
        with mock.patch.object(self.app, "_start_request") as start:
            self.app.review("approve", everything=False)
        start.assert_called_once_with(
            "review", [{"action": "APPROVE", "asset_id": "AST-B000001", "expected_version": 3}])

    def test_denying_asks_first(self):
        with mock.patch("gui.messagebox.askyesno", return_value=False), \
                mock.patch.object(self.app, "_start_request") as start:
            self.app.review("deny", everything=True)
        start.assert_not_called()


class GuiLoanTest(unittest.TestCase):
    """The GUI's check-out form, check-in and loans list."""

    LOANED = {"asset_id": "AST-P000001", "description": "Laptop", "category": "IT", "location": "Site A",
              "status": common.IN_USE, "version": 4,
              "loans": [{"borrower": "Ann", "start": "2026-01-01", "due": "2026-01-08", "returned": "2026-01-07"},
                        {"borrower": "Bob", "start": "2026-03-01", "due": "2026-03-08", "returned": None}]}
    FREE = {"asset_id": "AST-P000002", "description": "Phone", "category": "IT", "location": "Site A",
            "status": common.IN_USE, "version": 2,
            "loans": [{"borrower": "Cy", "start": "2026-02-01", "due": "2026-02-08", "returned": "2026-02-05"}]}

    def setUp(self):
        self.root, self.app = make_gui()
        self.addCleanup(self.root.destroy)
        self.app._show_assets([self.LOANED, self.FREE])

    def fill_form(self, asset_id, **values):
        """Open the check-out form, overwrite the given boxes and return its save function."""
        window, boxes, save = self.app.open_check_out_window(asset_id)
        self.addCleanup(lambda: window.winfo_exists() and window.destroy())
        for key, value in values.items():
            boxes[key].delete(0, "end")
            boxes[key].insert(0, value)
        return boxes, save

    def test_form_defaults_to_today_and_a_week_later(self):
        boxes, _ = self.fill_form("AST-P000002")
        today = datetime.date.today()
        self.assertEqual(boxes["start"].get(), today.isoformat())
        self.assertEqual(boxes["due"].get(), (today + datetime.timedelta(days=7)).isoformat())

    def test_check_out_sends_the_request(self):
        _, save = self.fill_form("AST-P000002", borrower=" Dee ", start="2026-11-06", due="2026-11-10")
        with mock.patch.object(self.app, "_start_request") as start:
            save()
        start.assert_called_once_with("check_out", {
            "action": "CHECK_OUT", "asset_id": "AST-P000002", "borrower": "Dee",
            "start": "2026-11-06", "due": "2026-11-10", "expected_version": 2})

    def test_bad_input_is_not_sent(self):
        for values in ({"borrower": ""}, {"start": "6/11/2026"}, {"due": "2026-1-5"},
                       {"start": "2026-11-10", "due": "2026-11-06"}):
            _, save = self.fill_form("AST-P000002", **{"borrower": "Dee", **values})
            with mock.patch("gui.messagebox.showwarning") as warn, \
                    mock.patch.object(self.app, "_start_request") as start:
                save()
            warn.assert_called_once()
            start.assert_not_called()

    def test_asset_on_loan_is_checked_in_after_confirming(self):
        self.app.tree.selection_set("AST-P000001")
        with mock.patch("gui.messagebox.askyesno", return_value=True), \
                mock.patch.object(self.app, "_start_request") as start:
            self.app.check_out_or_in_selected()
        start.assert_called_once_with(
            "check_in", {"action": "CHECK_IN", "asset_id": "AST-P000001", "expected_version": 4})

    def test_declining_the_check_in_sends_nothing(self):
        self.app.tree.selection_set("AST-P000001")
        with mock.patch("gui.messagebox.askyesno", return_value=False), \
                mock.patch.object(self.app, "_start_request") as start:
            self.app.check_out_or_in_selected()
        start.assert_not_called()

    def test_asset_not_on_loan_opens_the_form(self):
        self.app.tree.selection_set("AST-P000002")
        with mock.patch.object(self.app, "open_check_out_window") as form:
            self.app.check_out_or_in_selected()
        form.assert_called_once_with("AST-P000002")

    def test_loans_are_sorted_by_start_date_and_show_overdue(self):
        rows = self.app.loan_rows()
        self.assertEqual([row[1] for row in rows], ["Ann", "Cy", "Bob"])
        self.assertEqual([row[4] for row in rows], ["2026-01-07", "2026-02-05", "Overdue"])


class GuiStandaloneTest(unittest.TestCase):
    """gui.py is one file that needs nothing from the project, finds servers from one
    address, and logs in with an account."""

    def test_it_imports_nothing_from_the_project(self):
        with open(os.path.join(FOLDER, "gui.py"), encoding="utf-8") as file:
            tree = ast.parse(file.read())
        modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        project = {name[:-3] for name in os.listdir(FOLDER) if name.endswith(".py")}
        self.assertEqual(modules & project, set())

    def test_addresses_are_checked(self):
        import gui
        self.assertEqual(gui.parse_address("10.0.0.5"), ("10.0.0.5", 65432))
        self.assertEqual(gui.parse_address(" assets.example.com:7000 "), ("assets.example.com", 7000))
        for bad in ("", "999.1.1", "bad_name", "host:0", "host:99999", "host:abc", "a:1:2"):
            with self.subTest(bad), self.assertRaises(ValueError):
                gui.parse_address(bad)

    def test_the_servers_come_from_the_reply_primary_first(self):
        import gui
        reply = {"servers": [["127.0.0.1", 65432], ["10.0.0.9", 65433], ["junk"]]}
        # The user typed the backup's address, but the primary is still tried first.
        self.assertEqual(gui.servers_from_ping(("10.0.0.9", 65433), reply),
                         [("10.0.0.9", 65432), ("10.0.0.9", 65433)])
        self.assertEqual(gui.servers_from_ping(("10.0.0.9", 1), {}), [("10.0.0.9", 1)])


class GuiLoginTest(unittest.TestCase):
    """The GUI's connection code against a real server."""

    @classmethod
    def setUpClass(cls):
        import gui
        import node
        cls.gui = gui
        cls.listener = node.create_listening_socket("127.0.0.1", 0)
        cls.address = cls.listener.getsockname()
        threading.Thread(target=node.run_server, args=(cls.listener,), daemon=True).start()

    def setUp(self):
        import accounts
        gui = self.gui
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        salt = "cd" * 16
        record = {"salt": salt, "iterations": 1000, "role": "user",
                  "hash": accounts.hash_password("pass word", salt, 1000)}
        path = os.path.join(temp.name, "users.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump({"sam": record}, file)
        patcher = mock.patch.object(accounts, "USERS_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Each test starts with nobody logged in and nothing trusted.
        self.addCleanup(lambda: vars(gui.session).clear())
        gui.session.servers, gui.session.trusted, gui.session.token = [self.address], None, None

    def test_a_server_whose_certificate_was_not_trusted_is_not_sent_anything(self):
        gui = self.gui
        gui.session.trusted = "AA:BB"
        with self.assertRaisesRegex(ConnectionError, "different certificate"):
            gui.send_request({"action": "LOGIN", "username": "sam", "password": "pass word"})

    def test_logging_in_then_using_the_token(self):
        gui = self.gui
        gui.session.trusted = gui.probe(self.address)[0]
        wrong = gui.send_request({"action": "LOGIN", "username": "sam", "password": "nope"})
        self.assertEqual(wrong["code"], common.UNAUTHORIZED)
        self.assertEqual(gui.send_request({"action": "LIST"})["code"], common.UNAUTHORIZED)  # no token yet

        reply = gui.send_request({"action": "LOGIN", "username": "sam", "password": "pass word"})
        self.assertEqual((reply["status"], reply["role"]), ("OK", "user"))
        gui.session.token = reply["token"]
        self.assertEqual(gui.send_request({"action": "LIST"})["status"], "OK")
        # A user may not delete, and the GUI sees the server's refusal.
        self.assertEqual(gui.send_request({"action": "DELETE", "asset_id": "AST-P000001"})["code"], common.FORBIDDEN)

    def test_a_dead_server_is_skipped_for_the_next(self):
        gui = self.gui
        gui.session.trusted = gui.probe(self.address)[0]
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            dead = spare.getsockname()
        gui.session.servers = [dead, self.address]
        self.assertEqual(gui.send_request({"action": "PING"})["status"], "OK")

    def test_declining_to_trust_a_server_sends_nothing(self):
        import tkinter as tk
        gui = self.gui
        try:
            root = tk.Tk()
        except Exception as exc:
            raise unittest.SkipTest(f"No window can be opened here: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        screen = gui.LoginWindow(root, lambda: None)
        then = mock.Mock()
        with mock.patch("gui.messagebox.askyesno", return_value=False) as ask, \
                mock.patch.object(screen, "_run") as run:
            screen._trust(self.address, ("AA:BB", {"status": "OK"}), None, then)
        ask.assert_called_once()
        run.assert_not_called()  # no login
        self.assertIsNone(gui.session.trusted)
        then.assert_not_called()

"""
Tests for the Tkinter GUI (FR08). They drive the real window against real servers.

    python -m unittest discover -v tests            (every test, from the project folder)
    python -m unittest discover -v tests -p test_gui.py  (just these)

The window tests are skipped if Tkinter is not installed or there is no display. On a
Linux machine without a screen, run them with: xvfb-run python -m unittest discover tests
"""

import ast
import gc
import os
import time
import unittest
from unittest import mock

import sys

# The code under test is in the project folder and its tools and clients folders.
TESTS = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(TESTS)
for folder in (TESTS, PROJECT, os.path.join(PROJECT, "tools"), os.path.join(PROJECT, "clients")):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import common
import server
from test_system import ADMIN_PASSWORD, USER_PASSWORD, ServerTestCase, make_asset

try:
    import tkinter as tk
except ImportError:
    raise unittest.SkipTest("Tkinter is not installed")

import gui

try:
    tk.Tk().destroy()
    GUI_AVAILABLE = True
except Exception:  # no display
    GUI_AVAILABLE = False


class StandaloneTest(unittest.TestCase):
    """gui.py must work as a single file handed out on its own."""

    def test_it_imports_only_the_standard_library(self):
        tree = ast.parse(open(os.path.join(PROJECT, "clients", "gui.py"), encoding="utf-8").read())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        standard = {"hashlib", "ipaddress", "itertools", "json", "os", "queue", "re", "socket", "ssl",
                    "threading", "time", "tkinter", "uuid"}
        self.assertLessEqual(imported, standard)

    @unittest.skipUnless(GUI_AVAILABLE, "Tkinter is not available")
    def test_its_copies_of_the_protocol_constants_match_the_server(self):
        config = common.load_config()
        self.assertEqual(gui.PROTOCOL_VERSION, common.PROTOCOL_VERSION)
        self.assertEqual(gui.MAX_MESSAGE_BYTES, config["max_message_bytes"])
        self.assertEqual(gui.ASSET_FIELDS, common.ASSET_FIELDS)
        self.assertEqual(gui.EDITABLE_FIELDS, common.EDITABLE_FIELDS)
        self.assertEqual(gui.STATUSES, common.STATUSES)
        self.assertEqual(gui.DEFAULT_PORTS, {role: s["port"] for role, s in config["servers"].items()})
        self.assertEqual(gui.DISCOVERY_REQUEST, server.DISCOVERY_REQUEST)
        self.assertEqual(gui.SERVICE_NAME, server.SERVICE_NAME)

    @unittest.skipUnless(GUI_AVAILABLE, "Tkinter is not available")
    def test_addresses_are_validated(self):
        self.assertEqual(gui.parse_address("192.168.1.20", 65432), ("192.168.1.20", 65432))
        self.assertEqual(gui.parse_address(" server-1.local:7000 ", 65432), ("server-1.local", 7000))
        self.assertEqual(gui.parse_address("localhost", 65433), ("localhost", 65433))
        for bad in ("", "  ", "bad host", "host:", "host:0", "host:99999", "host:abc", "999.1.1.1", "-x.com", "a:b:c"):
            with self.assertRaises(ValueError, msg=bad):
                gui.parse_address(bad, 65432)

    @unittest.skipUnless(GUI_AVAILABLE, "Tkinter is not available")
    def test_servers_set_up_as_this_machine_are_reached_at_the_address_used(self):
        servers = {"primary": {"host": "127.0.0.1", "port": 1}, "backup": {"host": "10.0.0.9", "port": 2}}
        self.assertEqual(gui.server_addresses(servers, ("192.168.1.20", 1)), [("192.168.1.20", 1), ("10.0.0.9", 2)])
        self.assertEqual(gui.server_addresses(None, ("192.168.1.20", 1)), [("192.168.1.20", 1)])


@unittest.skipUnless(GUI_AVAILABLE, "Tkinter or a display is not available")
class GuiTest(ServerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.settings_file = os.path.join(self.folder, "gui-settings.json")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.close_windows)
        self.app = self.open_app()
        self.yes = mock.patch("gui.messagebox.askyesno", return_value=True)

    def close_windows(self) -> None:
        """Destroy the windows and clean them up here, on the main thread. Left to
        Python's garbage collector, they could be cleaned up from a background thread,
        which Tk does not allow."""
        self.root.destroy()
        del self.app, self.apps, self.root
        gc.collect()

    def open_app(self) -> "gui.AssetApp":
        app = gui.AssetApp(self.root, self.settings_file)
        # Keep every window object alive until the test ends: if a background thread
        # held the last reference, Tk would be cleaned up from the wrong thread.
        self.apps = getattr(self, "apps", []) + [app]
        return app

    def address(self, role: str) -> str:
        server_ = self.config["servers"][role]
        return f"{server_['host']}:{server_['port']}"

    def wait_until(self, condition, explanation: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"Never {explanation}; status: {self.app.status_text.get()}")

    def type_login(self, username: str = "admin", password: str = ADMIN_PASSWORD, role: str = "primary") -> None:
        self.app.server_entry.delete(0, tk.END)
        self.app.server_entry.insert(0, self.address(role))
        self.app.username_entry.insert(0, username)
        self.app.password_entry.insert(0, password)

    def log_in_to_window(self, username: str = "admin", password: str = ADMIN_PASSWORD, role: str = "primary") -> None:
        self.type_login(username, password, role)
        with self.yes:
            self.app.log_in()
            self.wait_until(lambda: self.app.token is not None and (
                "Showing" in self.app.status_text.get() or "No assets" in self.app.status_text.get()), "logged in")

    def table_ids(self) -> list:
        return list(self.app.tree.get_children())

    def fill_form(self, **values) -> None:
        for field, value in values.items():
            entry = self.app.fields[field]
            if field == "status":
                entry.set(value)
            else:
                entry.delete(0, tk.END)
                entry.insert(0, value)

    # ---- Connecting ------------------------------------------------------------

    def test_the_first_connection_asks_to_trust_the_certificate_and_remembers_it(self):
        self.type_login()
        with mock.patch("gui.messagebox.askyesno", return_value=True) as ask:
            self.app.log_in()
            self.wait_until(lambda: self.app.token is not None, "logged in")
        self.assertEqual(ask.call_count, 1)
        self.assertIn(self.app.settings["trusted"][self.address("primary")], ask.call_args[0][1])

        self.app = self.open_app()  # the next run remembers the server and its certificate
        self.assertEqual(self.app.server_entry.get(), self.address("primary"))
        with mock.patch("gui.messagebox.askyesno") as ask:
            self.app.username_entry.insert(0, "admin")
            self.app.password_entry.insert(0, ADMIN_PASSWORD)
            self.app.log_in()
            self.wait_until(lambda: self.app.token is not None, "logged in again")
        ask.assert_not_called()

    def test_declining_to_trust_sends_no_password(self):
        self.type_login()
        with mock.patch("gui.messagebox.askyesno", return_value=False):
            self.app.log_in()
            self.wait_until(lambda: "not trusted" in self.app.status_text.get(), "refused to connect")
        self.assertIsNone(self.app.token)
        self.assertEqual(self.app.settings["trusted"], {})

    def test_a_different_certificate_at_a_trusted_address_is_refused(self):
        self.app.settings["trusted"][self.address("primary")] = "AB:CD"  # as if trusted earlier
        self.type_login()
        with mock.patch("gui.messagebox.showerror") as warn, mock.patch("gui.messagebox.askyesno") as ask:
            self.app.log_in()
            self.wait_until(lambda: "not trusted" in self.app.status_text.get(), "refused to connect")
        warn.assert_called_once()
        ask.assert_not_called()  # no chance to wave it through
        self.assertIsNone(self.app.token)

    def test_an_invalid_address_is_explained_before_connecting(self):
        self.app.server_entry.insert(0, "not a host")
        self.app.log_in()
        self.assertIn("not a valid IP address or host name", self.app.status_text.get())

    def test_check_reports_whether_an_address_is_an_asset_server(self):
        self.app.server_entry.insert(0, self.address("primary"))
        self.app.check_server()
        self.wait_until(lambda: "primary server found" in self.app.status_text.get(), "found the server")
        self.assertIn("not yet trusted", self.app.status_text.get())
        self.assertIn(f"other server at {self.address('backup')}", self.app.status_text.get())

        self.app.server_entry.delete(0, tk.END)
        self.app.server_entry.insert(0, self.address("backup"))  # nothing is running there
        self.app.check_server()
        self.wait_until(lambda: "nothing answered" in self.app.status_text.get(), "reported nothing there")

    def test_find_servers_fills_in_the_primary(self):
        self.start_server("backup")
        ports = {role: s["port"] for role, s in self.config["servers"].items()}
        with mock.patch.dict(gui.DEFAULT_PORTS, ports):
            self.app.find_servers()
            self.wait_until(lambda: self.app.status_text.get().startswith("Found"), "found the servers")
        self.assertTrue(self.app.server_entry.get().endswith(f":{ports['primary']} (primary)"))
        self.assertIn("backup at", self.app.status_text.get())
        # Both are offered in the box's list, and a picked one is read without its label.
        choices = list(self.app.server_entry["values"])
        self.assertEqual([c.rsplit(" ", 1)[1] for c in choices], ["(primary)", "(backup)"])
        self.app.server_entry.set(choices[1])
        self.assertEqual(self.app.read_address()[1], ports["backup"])

    def test_find_servers_says_when_there_are_none(self):
        with mock.patch.dict(gui.DEFAULT_PORTS, {"primary": self.config["servers"]["backup"]["port"]}, clear=True):
            self.app.find_servers()
            self.wait_until(lambda: "No servers found" in self.app.status_text.get(), "reported none found")

    def server_pair(self) -> list:
        return [(s["host"], s["port"]) for s in (self.config["servers"]["primary"], self.config["servers"]["backup"])]

    def test_one_address_is_enough_the_server_says_where_the_backup_is(self):
        self.start_server("backup")
        self.type_login()
        with mock.patch("gui.messagebox.askyesno", return_value=True) as ask:
            self.app.log_in()
            self.wait_until(lambda: self.app.token is not None, "logged in")
        self.assertEqual(ask.call_count, 1)  # the backup shares the certificate
        self.assertEqual(self.app.addresses, self.server_pair())
        self.assertEqual(self.app.settings["servers"], [self.address("primary"), self.address("backup")])

    def test_typing_the_backup_address_still_puts_the_primary_first(self):
        self.start_server("backup")
        self.log_in_to_window(role="backup")
        self.assertEqual(self.app.addresses, self.server_pair())
        self.assertNotIn("(backup)", self.app.server_label.cget("text"))

    def test_it_logs_in_through_the_backup_when_the_primary_is_already_down(self):
        self.start_server("backup")
        self.log_in_to_window()           # learns where both servers are
        self.stop_server("primary")
        self.app = self.open_app()        # next time, with the primary down
        self.app.username_entry.insert(0, "admin")
        self.app.password_entry.insert(0, ADMIN_PASSWORD)
        with mock.patch("gui.messagebox.askyesno") as ask:
            self.app.log_in()
            self.wait_until(lambda: self.app.token is not None, "logged in through the backup")
        ask.assert_not_called()
        self.assertIn("(backup)", self.app.server_label.cget("text"))

    def test_the_fingerprint_shown_matches_the_one_certificate_py_prints(self):
        import certificate
        self.type_login()
        with mock.patch("gui.messagebox.askyesno", return_value=True) as ask:
            self.app.log_in()
            self.wait_until(lambda: self.app.token is not None, "logged in")
        self.assertIn(certificate.fingerprint(self.config), ask.call_args[0][1])

    def test_it_moves_to_the_backup_when_the_primary_fails(self):
        backup = self.start_server("backup")
        self.register(make_asset("AST-0001"))
        self.wait_until(lambda: backup.seq == 1, "copied the asset")
        self.log_in_to_window()  # only the primary's address is typed
        self.stop_server("primary")
        self.app.refresh()
        self.wait_until(lambda: "(backup)" in self.app.server_label.cget("text"), "used the backup")
        self.assertEqual(self.table_ids(), ["AST-0001"])

    # ---- Working with assets -------------------------------------------------

    def test_a_wrong_password_keeps_the_login_screen(self):
        self.type_login(password="wrong-password")
        with self.yes:
            self.app.log_in()
            self.wait_until(lambda: "UNAUTHENTICATED" in self.app.status_text.get(), "showed the login error")
        self.assertIsNone(self.app.token)

    def test_assets_can_be_registered_and_viewed(self):
        self.log_in_to_window()
        self.fill_form(**make_asset("AST-0100"))
        self.app.register()
        self.wait_until(lambda: self.table_ids() == ["AST-0100"], "showed the new asset")
        self.assertIn("Registered AST-0100", self.app.status_text.get())

    def test_status_can_only_be_chosen_from_the_allowed_values(self):
        self.assertEqual(tuple(self.app.fields["status"].cget("values")), ("In Use", "Not In Use"))
        self.assertEqual(str(self.app.fields["status"].cget("state")), "readonly")

    def test_assets_can_be_searched_by_any_field_or_one_field(self):
        self.register(make_asset("AST-0001", description="Dell laptop"))
        self.register(make_asset("AST-0002", description="HP printer", location="Site B"))
        self.log_in_to_window()
        self.app.search_entry.insert(0, "printer")
        self.app.run_search()
        self.wait_until(lambda: self.table_ids() == ["AST-0002"], "searched every field")

        self.app.search_field.set("Location")
        self.app.search_entry.delete(0, tk.END)
        self.app.search_entry.insert(0, "site a")
        self.app.run_search()
        self.wait_until(lambda: self.table_ids() == ["AST-0001"], "searched one field")

        self.app.clear_search()
        self.wait_until(lambda: len(self.table_ids()) == 2, "showed everything again")

    def test_long_lists_are_shown_a_page_at_a_time(self):
        for n in range(gui.PAGE_SIZE + 3):
            self.register(make_asset(f"AST-{n:04d}"))
        self.log_in_to_window()
        self.wait_until(lambda: len(self.table_ids()) == gui.PAGE_SIZE, "showed the first page")
        self.app.change_page(1)
        self.wait_until(lambda: len(self.table_ids()) == 3, "showed the second page")
        self.assertIn("Page 2 of 2", self.app.page_label.cget("text"))

    def test_a_selected_asset_can_be_edited(self):
        self.register(make_asset("AST-0001"))
        self.log_in_to_window()
        self.app.tree.selection_set("AST-0001")
        self.root.update()
        self.assertEqual(self.app.fields["description"].get(), "Laptop AST-0001")
        self.fill_form(location="Site C", status="Not In Use")
        self.app.save_changes()
        self.wait_until(lambda: "Saved changes" in self.app.status_text.get(), "saved the edit")
        stored = self.listed()[0]
        self.assertEqual((stored["location"], stored["status"], stored["version"]), ("Site C", "Not In Use", 2))

    def test_an_edit_of_out_of_date_data_reports_a_conflict(self):
        self.register(make_asset("AST-0001"))
        self.log_in_to_window()
        self.edit("AST-0001", 1, location="Changed elsewhere")
        self.app.tree.selection_set("AST-0001")
        self.root.update()
        self.fill_form(location="Site C")
        self.app.save_changes()
        self.wait_until(lambda: "changed by someone else" in self.app.status_text.get(), "reported the conflict")

    def test_an_administrator_can_delete_the_selected_asset(self):
        self.register(make_asset("AST-0001"))
        self.log_in_to_window()
        self.app.tree.selection_set("AST-0001")
        self.root.update()
        with self.yes:
            self.app.delete_selected()
        self.wait_until(lambda: self.table_ids() == [], "removed the asset")
        self.assertEqual(self.listed(), [])

    def test_delete_is_disabled_for_ordinary_users(self):
        self.log_in_to_window("alice", USER_PASSWORD)
        self.assertIn("disabled", self.app.delete_button.state())


if __name__ == "__main__":
    unittest.main()

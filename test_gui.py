"""
Tests for the Tkinter GUI (FR08). They drive the real window against a real server.

    python -m unittest -v test_gui

They are skipped if Tkinter is not installed or there is no display to open a window
on. On a Linux machine without a screen, run them with: xvfb-run python -m unittest test_gui
"""

import time
import unittest
from unittest import mock

from test_system import ADMIN_PASSWORD, USER_PASSWORD, ServerTestCase, make_asset

try:
    import tkinter as tk

    import gui

    tk.Tk().destroy()
    GUI_AVAILABLE = True
except Exception:  # no Tkinter, or no display
    GUI_AVAILABLE = False


@unittest.skipUnless(GUI_AVAILABLE, "Tkinter or a display is not available")
class GuiTest(ServerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = gui.AssetApp(self.root, self.config)

    def wait_until(self, condition, explanation: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"Never {explanation}; status: {self.app.status_text.get()}")

    def log_in_to_window(self, username: str = "admin", password: str = ADMIN_PASSWORD) -> None:
        self.app.username_entry.insert(0, username)
        self.app.password_entry.insert(0, password)
        self.app.log_in()
        self.wait_until(lambda: self.app.token is not None and "Showing" in self.app.status_text.get()
                        or "No assets" in self.app.status_text.get(), "logged in")

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

    def test_a_wrong_password_keeps_the_login_screen(self):
        self.app.username_entry.insert(0, "admin")
        self.app.password_entry.insert(0, "wrong-password")
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
        with mock.patch("gui.messagebox.askyesno", return_value=True):
            self.app.delete_selected()
        self.wait_until(lambda: self.table_ids() == [], "removed the asset")
        self.assertEqual(self.listed(), [])

    def test_delete_is_disabled_for_ordinary_users(self):
        self.log_in_to_window("alice", USER_PASSWORD)
        self.assertIn("disabled", self.app.delete_button.state())


if __name__ == "__main__":
    unittest.main()

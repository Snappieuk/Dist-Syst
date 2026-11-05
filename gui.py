"""
Tkinter graphical client for the Asset Management System.

    python gui.py

Log in, then register, view, search, edit and delete assets without typing any
commands. Select a row to load it into the form, change it and press "Save changes".
Only administrators can delete; the server enforces this, and the button is
disabled for other users.

Each request runs on a background thread, so the window never freezes while it
waits for the server.
"""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from common import ASSET_FIELDS, EDITABLE_FIELDS, STATUSES, new_request_id, send_request

PAGE_SIZE = 20
ANY_FIELD = "Any field"
LABELS = {
    "asset_id": "Asset ID",
    "description": "Description",
    "category": "Category",
    "location": "Location",
    "status": "Status",
}
COLUMNS = ASSET_FIELDS + ("updated_at",)


class AssetApp:
    """The application window: a login screen, then the asset screen."""

    def __init__(self, root: tk.Tk, config: dict = None) -> None:
        self.root = root
        self.config = config
        self.token = None
        self.role = None
        self.page = 1
        self.search = {}      # the LIST parameters of the search being shown
        self.shown = {}       # the assets in the table, by asset_id
        self.notice = None    # a message to show once the table has refreshed
        self.replies = queue.Queue()

        root.title("Asset Management System")
        root.minsize(900, 500)
        self.status_text = tk.StringVar(value="Please log in.")
        self.build_login()
        self.build_main()
        ttk.Label(root, textvariable=self.status_text, anchor=tk.W, relief=tk.SUNKEN).pack(side=tk.BOTTOM, fill=tk.X)
        self.login_frame.pack(padx=20, pady=20)
        self.check_replies()

    # ---- Layout ---------------------------------------------------------------

    def build_login(self) -> None:
        frame = self.login_frame = ttk.LabelFrame(self.root, text="Log in", padding=12)
        ttk.Label(frame, text="Username").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.username_entry = ttk.Entry(frame, width=30)
        self.username_entry.grid(row=0, column=1, pady=3)
        ttk.Label(frame, text="Password").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.password_entry = ttk.Entry(frame, width=30, show="*")
        self.password_entry.grid(row=1, column=1, pady=3)
        self.login_button = ttk.Button(frame, text="Log in", command=self.log_in)
        self.login_button.grid(row=2, column=1, sticky=tk.E, pady=(8, 0))
        self.password_entry.bind("<Return>", lambda _event: self.log_in())

    def build_main(self) -> None:
        frame = self.main_frame = ttk.Frame(self.root, padding=8)

        # Search bar
        search = ttk.Frame(frame)
        search.pack(fill=tk.X)
        ttk.Label(search, text="Search").pack(side=tk.LEFT)
        self.search_field = ttk.Combobox(
            search, state="readonly", width=14, values=[ANY_FIELD] + [LABELS[f] for f in ASSET_FIELDS]
        )
        self.search_field.set(ANY_FIELD)
        self.search_field.pack(side=tk.LEFT, padx=4)
        self.search_entry = ttk.Entry(search, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=4)
        self.search_entry.bind("<Return>", lambda _event: self.run_search())
        ttk.Button(search, text="Search", command=self.run_search).pack(side=tk.LEFT, padx=2)
        ttk.Button(search, text="Show all", command=self.clear_search).pack(side=tk.LEFT, padx=2)
        self.user_label = ttk.Label(search, text="")
        self.user_label.pack(side=tk.RIGHT)

        # Asset table
        table = ttk.Frame(frame)
        table.pack(fill=tk.BOTH, expand=True, pady=6)
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", height=12, selectmode="browse")
        for column in COLUMNS:
            self.tree.heading(column, text=LABELS.get(column, "Last changed"))
            self.tree.column(column, width=150 if column == "description" else 110)
        scrollbar = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.load_selected())

        # Paging
        paging = ttk.Frame(frame)
        paging.pack(fill=tk.X)
        self.previous_button = ttk.Button(paging, text="< Previous", command=lambda: self.change_page(-1))
        self.previous_button.pack(side=tk.LEFT)
        self.page_label = ttk.Label(paging, text="")
        self.page_label.pack(side=tk.LEFT, padx=8)
        self.next_button = ttk.Button(paging, text="Next >", command=lambda: self.change_page(1))
        self.next_button.pack(side=tk.LEFT)

        # Form
        form = ttk.LabelFrame(frame, text="Asset details (every field is required)", padding=8)
        form.pack(fill=tk.X, pady=6)
        self.fields = {}
        for column, field in enumerate(ASSET_FIELDS):
            ttk.Label(form, text=LABELS[field]).grid(row=0, column=column, sticky=tk.W, padx=4)
            if field == "status":
                entry = ttk.Combobox(form, state="readonly", values=STATUSES, width=14)
                entry.set(STATUSES[0])
            else:
                entry = ttk.Entry(form, width=22)
            entry.grid(row=1, column=column, padx=4, sticky=tk.EW)
            self.fields[field] = entry

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Register new asset", command=self.register).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="Save changes", command=self.save_changes).pack(side=tk.LEFT, padx=2)
        self.delete_button = ttk.Button(buttons, text="Delete selected", command=self.delete_selected)
        self.delete_button.pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="Clear form", command=self.clear_form).pack(side=tk.LEFT, padx=2)

    # ---- Talking to the server ------------------------------------------------

    def send(self, request: dict, on_reply) -> None:
        """Send a request on a background thread; `on_reply` runs on the window's thread."""
        if self.token is not None:
            request = {**request, "token": self.token}

        def work() -> None:
            try:
                reply = send_request(request, self.config)
            except (OSError, ValueError) as exc:
                reply = {"status": "ERROR", "code": "NO_SERVER", "message": f"Could not reach the server: {exc}"}
            self.replies.put((on_reply, reply))

        self.status_text.set("Working...")
        threading.Thread(target=work, daemon=True).start()

    def check_replies(self) -> None:
        """Hand finished replies to their callbacks. Tk may only be used from its own thread."""
        while not self.replies.empty():
            on_reply, reply = self.replies.get()
            if reply.get("code") == "UNAUTHENTICATED" and self.token is not None:
                self.status_text.set(reply["message"])
                self.show_login()
                continue
            on_reply(reply)
        self.root.after(50, self.check_replies)

    def report_error(self, reply: dict) -> None:
        self.status_text.set(f"{reply.get('code')}: {reply.get('message')}")

    # ---- Logging in -----------------------------------------------------------

    def log_in(self) -> None:
        self.token = None
        request = {
            "action": "LOGIN",
            "username": self.username_entry.get().strip(),
            "password": self.password_entry.get(),
        }
        self.send(request, self.logged_in)

    def logged_in(self, reply: dict) -> None:
        if reply["status"] != "OK":
            self.report_error(reply)
            return
        self.token, self.role = reply["token"], reply["role"]
        self.password_entry.delete(0, tk.END)
        self.user_label.configure(text=f"Logged in as {reply['username']} ({self.role})")
        self.delete_button.configure(state=tk.NORMAL if self.role == "admin" else tk.DISABLED)
        self.login_frame.pack_forget()
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh()

    def show_login(self) -> None:
        self.token = None
        self.main_frame.pack_forget()
        self.login_frame.pack(padx=20, pady=20)

    # ---- Viewing and searching ------------------------------------------------

    def refresh(self) -> None:
        request = {"action": "LIST", "page": self.page, "page_size": PAGE_SIZE, **self.search}
        self.send(request, self.show_assets)

    def show_assets(self, reply: dict) -> None:
        if reply["status"] != "OK":
            self.report_error(reply)
            return
        if not reply["assets"] and self.page > 1:  # the last page emptied: step back
            self.page = reply["pages"]
            self.refresh()
            return
        self.tree.delete(*self.tree.get_children())
        self.shown = {asset["asset_id"]: asset for asset in reply["assets"]}
        for asset in reply["assets"]:
            self.tree.insert("", tk.END, iid=asset["asset_id"], values=[asset.get(c, "") for c in COLUMNS])
        self.page_label.configure(text=f"Page {reply['page']} of {reply['pages']} ({reply['total']} assets)")
        self.previous_button.configure(state=tk.NORMAL if reply["page"] > 1 else tk.DISABLED)
        self.next_button.configure(state=tk.NORMAL if reply["page"] < reply["pages"] else tk.DISABLED)
        if self.notice:
            self.status_text.set(self.notice)
            self.notice = None
        elif reply["total"] == 0:
            self.status_text.set("No assets match this search." if self.search else "No assets registered yet.")
        else:
            self.status_text.set(f"Showing {len(reply['assets'])} of {reply['total']} assets.")

    def run_search(self) -> None:
        text = self.search_entry.get().strip()
        label = self.search_field.get()
        if not text:
            self.search = {}
        elif label == ANY_FIELD:
            self.search = {"query": text}
        else:
            field = next(f for f, l in LABELS.items() if l == label)
            self.search = {"filters": {field: text}}
        self.page = 1
        self.refresh()

    def clear_search(self) -> None:
        self.search_entry.delete(0, tk.END)
        self.search_field.set(ANY_FIELD)
        self.run_search()

    def change_page(self, step: int) -> None:
        self.page = max(1, self.page + step)
        self.refresh()

    # ---- The form -------------------------------------------------------------

    def form_values(self) -> dict:
        return {field: entry.get().strip() for field, entry in self.fields.items()}

    def selected(self) -> object:
        selection = self.tree.selection()
        return self.shown.get(selection[0]) if selection else None

    def load_selected(self) -> None:
        asset = self.selected()
        if asset is None:
            return
        self.fields["asset_id"].configure(state=tk.NORMAL)
        for field, entry in self.fields.items():
            if field == "status":
                entry.set(asset[field])
            else:
                entry.delete(0, tk.END)
                entry.insert(0, asset[field])
        self.fields["asset_id"].configure(state="readonly")  # IDs can't be edited

    def clear_form(self) -> None:
        self.tree.selection_remove(*self.tree.selection())
        self.fields["asset_id"].configure(state=tk.NORMAL)
        for field, entry in self.fields.items():
            if field == "status":
                entry.set(STATUSES[0])
            else:
                entry.delete(0, tk.END)

    # ---- Changes --------------------------------------------------------------

    def after_change(self, success: str):
        def handle(reply: dict) -> None:
            if reply["status"] != "OK":
                self.report_error(reply)
                if reply.get("code") == "CONFLICT":
                    # Show the latest data, keeping the explanation on screen.
                    self.notice = f"{reply['message']} The list now shows the latest data."
                    self.refresh()
                return
            self.clear_form()
            self.notice = success
            self.refresh()
        return handle

    def register(self) -> None:
        values = self.form_values()
        request = {"action": "REGISTER", "request_id": new_request_id(), "asset": values}
        self.send(request, self.after_change(f"Registered {values['asset_id']}."))

    def save_changes(self) -> None:
        asset = self.selected()
        if asset is None:
            self.status_text.set("Select an asset to edit first.")
            return
        values = self.form_values()
        changes = {field: values[field] for field in EDITABLE_FIELDS if values[field] != asset[field]}
        if not changes:
            self.status_text.set("Nothing has changed.")
            return
        request = {
            "action": "EDIT",
            "request_id": new_request_id(),
            "asset_id": asset["asset_id"],
            "expected_version": asset["version"],
            "changes": changes,
        }
        self.send(request, self.after_change(f"Saved changes to {asset['asset_id']}."))

    def delete_selected(self, confirm: bool = True) -> None:
        asset = self.selected()
        if asset is None:
            self.status_text.set("Select an asset to delete first.")
            return
        if confirm and not messagebox.askyesno("Delete asset", f"Delete {asset['asset_id']}?"):
            return
        request = {
            "action": "DELETE",
            "request_id": new_request_id(),
            "asset_id": asset["asset_id"],
            "expected_version": asset["version"],
        }
        self.send(request, self.after_change(f"Deleted {asset['asset_id']}."))


def main() -> None:
    root = tk.Tk()
    AssetApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

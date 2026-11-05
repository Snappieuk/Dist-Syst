"""
Tkinter interface for the Asset Management System.

WHY THIS FILE USES THREADS AND A QUEUE: Tkinter can only redraw the window and
respond to clicks while its main thread is free. A network request can take
seconds, and running it on the main thread would freeze the window. So each
request runs on a background thread. Tkinter is also not safe to call from other
threads, so that thread puts its result on a queue, and the main thread checks
the queue regularly and updates the window itself.
"""

import queue      # carries results from background threads to the main thread
import threading  # runs network requests without freezing the window
import tkinter as tk
from tkinter import messagebox, ttk

from common import (
    IN_USE,
    NOT_IN_USE,
    format_asset_tags,
    generate_asset_tags,
    include_asset,
    new_register_request,
    send_request,
)

# What the status line says when each kind of change succeeds.
SUCCESS_MESSAGES = {
    "register": "Asset registered successfully",
    "set_status": "Asset status updated",
    "delete": "Asset deleted",
}


class AssetManagementApp:
    """The main window: a form to register assets, a table listing them, and buttons
    to change or delete the selected one."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Asset Management")
        self.root.minsize(760, 480)
        self.results = queue.Queue()  # finished requests, waiting to be displayed

        self.fields = {}  # the form's text boxes, by asset field name
        self.unconfirmed_registration = None  # see register_asset()
        self._build_ui()

        # Start checking for finished requests, then load the table.
        self.root.after(100, self._process_results)
        self.refresh_assets()

    def _build_ui(self) -> None:
        """Create the form, the table and the status line."""
        form = ttk.LabelFrame(self.root, text="Register an asset", padding=10)
        form.pack(fill=tk.X, padx=12, pady=(12, 8))

        # The server creates the asset ID, so there is no box for it.
        labels = (
            ("description", "Description"),
            ("category", "Category"),
            ("location", "Location"),
            ("status", "Status"),
        )
        for row, (key, label) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            # Status offers the standard choices, but other text can be typed too.
            entry = ttk.Combobox(form, values=(IN_USE, NOT_IN_USE)) if key == "status" else ttk.Entry(form)
            entry.grid(row=row, column=1, sticky=tk.EW, pady=3)
            self.fields[key] = entry

        form.columnconfigure(1, weight=1)
        self.register_button = ttk.Button(form, text="Register Asset", command=self.register_asset)
        self.register_button.grid(row=0, column=2, rowspan=2, padx=(12, 0), sticky=tk.N)
        self.refresh_button = ttk.Button(form, text="Refresh List", command=self.refresh_assets)
        self.refresh_button.grid(row=2, column=2, rowspan=2, padx=(12, 0), sticky=tk.N)

        listing = ttk.LabelFrame(self.root, text="Registered assets", padding=8)
        listing.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # Packed first, so the table cannot squeeze the buttons out of a small window.
        actions = ttk.Frame(listing)
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.not_in_use_button = ttk.Button(
            actions, text="Mark Not In Use", command=lambda: self.set_selected_status(NOT_IN_USE)
        )
        self.not_in_use_button.pack(side=tk.LEFT)
        self.in_use_button = ttk.Button(
            actions, text="Mark In Use", command=lambda: self.set_selected_status(IN_USE)
        )
        self.in_use_button.pack(side=tk.LEFT, padx=(8, 0))
        self.delete_button = ttk.Button(actions, text="Delete Asset", command=self.delete_selected)
        self.delete_button.pack(side=tk.RIGHT)

        columns = (
            "asset_id",
            "description",
            "category",
            "location",
            "status",
            "tags",
            "date_registered",
        )
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode=tk.BROWSE)
        headings = {
            "asset_id": "Asset ID",
            "description": "Description",
            "category": "Category",
            "location": "Location",
            "status": "Status",
            "tags": "Tags",
            "date_registered": "Date Registered",
        }
        widths = {
            "asset_id": 170,
            "description": 190,
            "category": 120,
            "location": 150,
            "status": 100,
            "tags": 160,
            "date_registered": 115,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=70, anchor=tk.W)

        scrollbar = ttk.Scrollbar(listing, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.status_label = ttk.Label(self.root, text="Ready", anchor=tk.W)
        self.status_label.pack(fill=tk.X, padx=14, pady=(0, 10))

    def _set_busy(self, busy: bool) -> None:
        """Disable the buttons during a request, so it cannot be sent twice."""
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (
            self.register_button,
            self.refresh_button,
            self.not_in_use_button,
            self.in_use_button,
            self.delete_button,
        ):
            button.configure(state=state)

    def _start_request(self, operation: str, request: dict) -> None:
        """Send a request on a background thread, keeping the window responsive."""
        self._set_busy(True)
        self.status_label.configure(text="Connecting to asset server...")

        def run() -> None:
            # This runs on the background thread, so it must not touch any
            # widgets. It only puts its result on the queue.
            try:
                response = send_request(request)
                assets = None
                error = None

                # After a successful change, fetch the list again so the table shows
                # it. A new asset is included even if the list came from a server
                # that has not synced it yet.
                if operation != "list" and response.get("status") == "OK":
                    try:
                        list_response = send_request({"action": "LIST"})
                        if list_response.get("status") != "OK":
                            error = list_response.get("detail", "Could not refresh the asset list")
                        else:
                            assets = include_asset(list_response.get("assets", []), response.get("stored"))
                    except (OSError, ValueError) as exc:
                        error = str(exc)
                self.results.put((operation, response, assets, error))
            except (OSError, ValueError) as exc:
                # OSError covers network failures; ValueError covers invalid replies.
                self.results.put((operation, None, None, str(exc)))

        # daemon=True means an unfinished request does not stop the program closing.
        threading.Thread(target=run, daemon=True).start()

    def register_asset(self) -> None:
        """Check the form is filled in, then send it to the server."""
        asset = {key: entry.get().strip() for key, entry in self.fields.items()}
        missing = [key.replace("_", " ").title() for key, value in asset.items() if not value]
        if missing:
            messagebox.showwarning(
                "Missing information",
                "Please fill in: " + ", ".join(missing),
                parent=self.root,
            )
            return

        # If the last attempt failed, the server may still have saved it, with only
        # the reply being lost. Pressing the button again with the same details sends
        # the same request, which cannot create a second copy of the asset.
        pending = self.unconfirmed_registration
        if pending is None or pending["asset"] != asset:
            pending = new_register_request(asset)
            self.unconfirmed_registration = pending

        self._start_request("register", pending)

    def refresh_assets(self) -> None:
        """Ask the server for the current list of assets."""
        self._start_request("list", {"action": "LIST"})

    def _selected_asset_id(self) -> object:
        """Return the ID of the asset selected in the table, or None after asking
        the user to select one."""
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("No asset selected", "Please select an asset in the table.", parent=self.root)
            return None
        return selection[0]  # each row is identified by its asset's ID

    def set_selected_status(self, status: str) -> None:
        """Change the status of the asset selected in the table."""
        asset_id = self._selected_asset_id()
        if asset_id is not None:
            self._start_request("set_status", {"action": "SET_STATUS", "asset_id": asset_id, "status": status})

    def delete_selected(self) -> None:
        """Delete the asset selected in the table, once the user has confirmed it."""
        asset_id = self._selected_asset_id()
        if asset_id is None:
            return

        if messagebox.askyesno(
            "Delete asset",
            f"Delete {asset_id} for good? This cannot be undone.",
            icon=messagebox.WARNING,
            parent=self.root,
        ):
            self._start_request("delete", {"action": "DELETE", "asset_id": asset_id})

    def _finish_registration(self) -> None:
        """Clear the form after a registration the server has confirmed."""
        self.unconfirmed_registration = None
        for entry in self.fields.values():
            entry.delete(0, tk.END)

    def _process_results(self) -> None:
        """Display any finished requests. Runs on the main thread, every 100 ms."""
        try:
            while True:
                operation, response, assets, error = self.results.get_nowait()
                self._set_busy(False)

                if error:
                    # The change worked and only the list refresh failed, so finish
                    # as usual but report the problem.
                    if operation != "list" and response and response.get("status") == "OK":
                        if operation == "register":
                            self._finish_registration()
                        self.status_label.configure(text=SUCCESS_MESSAGES[operation] + "; list refresh failed")
                        messagebox.showerror(
                            "List not refreshed",
                            SUCCESS_MESSAGES[operation] + ", but the list could not be refreshed:\n" + error,
                            parent=self.root,
                        )
                        continue
                    self.status_label.configure(text="Request failed")
                    messagebox.showerror("Asset server error", error, parent=self.root)
                    continue

                if response.get("status") != "OK":
                    detail = response.get("detail", "The server returned an error.")
                    self.status_label.configure(text="Request failed")
                    messagebox.showerror("Asset server error", detail, parent=self.root)
                    continue

                if operation != "list":
                    if operation == "register":
                        self._finish_registration()
                    self.status_label.configure(text=SUCCESS_MESSAGES[operation])
                    self._show_assets(assets)
                else:
                    self._show_assets(response.get("assets", []))
                    self.status_label.configure(text="Asset list refreshed")
        except queue.Empty:
            pass  # nothing left to display for now

        # Check again shortly. after() does not block, so the window stays responsive.
        self.root.after(100, self._process_results)

    def _show_assets(self, assets: list) -> None:
        """Replace the table's contents with `assets`."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        # Assets saved before tagging was added have no tags, so those are
        # worked out here for display.
        for asset in assets:
            self.tree.insert(
                "",
                tk.END,
                iid=asset.get("asset_id"),  # lets a selected row give its asset's ID
                values=(
                    asset.get("asset_id", ""),
                    asset.get("description", ""),
                    asset.get("category", ""),
                    asset.get("location", ""),
                    asset.get("status", ""),
                    format_asset_tags(asset.get("tags") or generate_asset_tags(asset)),
                    asset.get("date_registered", ""),
                ),
            )


def main() -> None:
    root = tk.Tk()
    AssetManagementApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

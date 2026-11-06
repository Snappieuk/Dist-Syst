"""
Tkinter client for the Asset Management System. A single file: copy it to any computer
with Python 3.8+ and run it. It needs nothing else from the project, and saves nothing.

    python gui.py

On the login screen, enter the server's IP address or host name (add :port if it isn't
the usual one), or press "Find servers" to look for one on the local network. Only one
address is needed: the server tells the GUI where every server is, and if the primary
fails, the GUI moves to the backup by itself. Log in with the account the server's
administrator gave you (the same accounts as the web page). The server checks every
request against your account, and only admins can delete assets or approve flagged ones.

Certificates: you never have to deal with them. The first certificate the GUI sees is
trusted automatically, and after that only that exact certificate is accepted until the
window is closed (so the connection is encrypted, and a different server appearing
mid-session is refused). The weak spot: someone already intercepting the very first
connection would not be noticed.

WHY THIS FILE USES THREADS AND A QUEUE: Tkinter can only redraw the window and
respond to clicks while its main thread is free. A network request can take
seconds, and running it on the main thread would freeze the window. So each
request runs on a background thread. Tkinter is also not safe to call from other
threads, so that thread puts its result on a queue, and the main thread checks
the queue regularly and updates the window itself.
"""

import datetime   # today's date, and checking the dates typed into the check-out form
import hashlib    # fingerprints a server's certificate
import ipaddress  # checks a typed server address
import json       # requests and replies are JSON
import queue      # carries results from background threads to the main thread
import re         # checks a typed host name
import socket     # talks to the servers, and finds them by UDP broadcast
import ssl        # encrypts every connection
import threading  # runs network requests without freezing the window
import time       # waits for a backup to take over
import tkinter as tk
import uuid       # gives each registration its own ID, so a retry can't register twice
from tkinter import messagebox, ttk

# ---- Must match the server (common.py and node.py) ----------------------------
# This file imports nothing from the rest of the project, so it can be copied on its
# own to any computer with Python 3.8+. These few values are the ones it shares.
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
IN_USE, NOT_IN_USE = "In Use", "Not In Use"
STATUSES = (IN_USE, NOT_IN_USE)
DEFAULT_PORTS = (65432, 65433)  # the primary's and the backup's, searched for by "Find servers"
DISCOVERY_REQUEST = b"ASSET_MANAGEMENT_DISCOVER"
SERVICE_NAME = "asset-management"
# --------------------------------------------------------------------------------

TIMEOUT_SECONDS = 5           # how long to wait on one server
DISCOVERY_SECONDS = 1.5       # how long "Find servers" listens for answers
FAILOVER_WAIT_SECONDS = 15    # how long to wait for a backup that is taking over
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
HOST_NAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


class UntrustedServer(ConnectionError):
    """A server's certificate is not the one the user chose to trust."""


class Session:
    """Who is logged in, and which servers to talk to. Nothing is saved to disk: it all
    lasts until the window is closed, so this file is all a computer needs."""

    servers = []     # addresses to try in order, the primary first
    trusted = None   # the certificate fingerprint the user accepted
    token = None     # proves the login to the servers
    username = None
    role = None


session = Session()


# ---- Addresses --------------------------------------------------------------------

def parse_address(text: str, default_port: int = DEFAULT_PORTS[0]) -> tuple:
    """Turn "host" or "host:port" into (host, port), or raise ValueError saying what's wrong."""
    text = text.strip()
    if not text:
        raise ValueError("Enter the server's IP address or host name.")
    has_port = text.count(":") == 1
    host, port_text = text.split(":") if has_port else (text, "")
    if has_port and not (port_text.isdigit() and 1 <= int(port_text) <= 65535):
        raise ValueError(f"'{port_text}' is not a port. Use a number from 1 to 65535.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # Something like 999.1.1 looks like an IP address but isn't one, and isn't a name either.
        if re.fullmatch(r"[\d.]+", host) or not HOST_NAME.fullmatch(host):
            raise ValueError(f"'{host}' is not a valid IP address or host name.") from None
    return host, int(port_text) if has_port else default_port


def servers_from_ping(typed: tuple, reply: dict) -> list:
    """The addresses to use, from the one the user typed and the list in a PING reply.

    The servers list their own addresses as their config.json gives them, and "127.0.0.1"
    means nothing to a GUI on another computer, so those are replaced by the typed host.
    The order is the servers' own, primary first, so the GUI starts with the primary even
    if the user typed the backup's address.
    """
    servers = []
    for entry in reply.get("servers") or []:
        try:
            host, port = str(entry[0]), int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue  # skip an entry that isn't an address
        address = (typed[0] if host in LOOPBACK_NAMES else host, port)
        if address not in servers:
            servers.append(address)
    if typed not in servers:
        servers.insert(0, typed)
    return servers


# ---- Talking to a server ------------------------------------------------------------

def fingerprint(certificate: bytes) -> str:
    """The SHA-256 fingerprint of a certificate, as people compare it: AB:CD:..."""
    digest = hashlib.sha256(certificate).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def receive_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("The server closed the connection")
        data += chunk
    return data


def ask(address: tuple, request: dict, trusted: object = None) -> tuple:
    """Send one request to one server and return (reply, certificate fingerprint).

    The server uses its own certificate, which Python can't check against a known
    authority, so the usual check is switched off. Instead the fingerprint is compared
    with the one the user trusted, BEFORE anything is sent, so a password is never sent
    to a server the user hasn't accepted. With no `trusted` fingerprint the request is
    sent anyway, so it must hold nothing private: only probe()'s PING does that.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(address, timeout=TIMEOUT_SECONDS)
    try:
        sock = context.wrap_socket(raw)
    except Exception:
        raw.close()
        raise
    with sock:
        found = fingerprint(sock.getpeercert(binary_form=True))
        if trusted is not None and found != trusted:
            raise UntrustedServer(
                f"The server at {address[0]}:{address[1]} has a different certificate from the "
                "one you trusted, so it was not used."
            )
        # Messages are length-prefixed JSON, the same as the servers use.
        payload = json.dumps({**request, "protocol_version": PROTOCOL_VERSION}).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("The request is too large")
        sock.sendall(len(payload).to_bytes(4, "big") + payload)
        length = int.from_bytes(receive_exact(sock, 4), "big")
        if length > MAX_MESSAGE_BYTES:
            raise ValueError("The reply is too large")
        reply = json.loads(receive_exact(sock, length).decode("utf-8"))
        if not isinstance(reply, dict):
            raise ValueError("The reply is not valid")
        return reply, found


def probe(address: tuple) -> tuple:
    """PING a server we know nothing about, returning (certificate fingerprint, reply). A
    PING needs no login and carries nothing private, so it is safe to send before the user
    has decided whether to trust the server. Its reply is not used until they have."""
    reply, found = ask(address, {"action": "PING"}, trusted=None)
    return found, reply


def discover() -> list:
    """Look for servers on the local network, returning their (host, port) addresses.

    A broadcast is sent to each usual port. Every server answers, naming the port it
    really uses. This machine is asked directly too, as a broadcast doesn't always reach
    a server on the same computer.
    """
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        for target in ("255.255.255.255", "127.0.0.1"):
            for port in DEFAULT_PORTS:
                try:
                    sock.sendto(DISCOVERY_REQUEST, (target, port))
                except OSError:
                    pass  # no network to broadcast on; the other target may still work
        end = time.monotonic() + DISCOVERY_SECONDS
        while time.monotonic() < end:
            try:
                data, (host, _) = sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
                address = (host, int(info["port"]))
            except socket.timeout:
                continue
            except (OSError, ValueError, KeyError, TypeError):
                continue  # not an answer from one of our servers
            if info.get("service") == SERVICE_NAME and address not in found:
                found.append(address)
    return found


def send_request(request: dict) -> dict:
    """Send a request to the first server that answers, adding the login token.

    This is failover: if the primary is down, the next server is tried without the user
    doing anything. Retrying on another server is safe, because a repeated registration
    is recognised by its request ID. The next server is also tried after an INTERNAL or
    NOT_FOUND reply (this one may not have the asset yet), and if every server says
    NOT_PRIMARY, a backup is taking over, so it waits up to FAILOVER_WAIT_SECONDS.

    Raises ConnectionError if no server can be reached, and ValueError for an invalid reply.
    """
    if session.token:
        request = {**request, "token": session.token}
    deadline = time.monotonic() + FAILOVER_WAIT_SECONDS

    while True:
        last_error = retryable = not_primary = None
        for address in session.servers:
            try:
                reply, _ = ask(address, request, trusted=session.trusted)
            except OSError as exc:  # refused, timed out, dropped, or an untrusted certificate
                last_error = exc
                continue
            if reply.get("code") in ("INTERNAL", "NOT_FOUND"):
                retryable = reply
            elif reply.get("code") == "NOT_PRIMARY":
                not_primary = reply
            else:
                return reply

        if retryable is not None:
            return retryable
        if not_primary is None:
            break  # nothing could be reached, so there is nothing to wait for
        if time.monotonic() >= deadline:
            return not_primary
        time.sleep(1)

    raise ConnectionError(f"Could not reach any asset server ({last_error}).\nCheck that the server is running, then try again.")


# ---- Small helpers the main window uses ------------------------------------------------

def format_asset_tags(tags: list) -> str:
    """Join tags into one comma-separated string for the table."""
    return ", ".join(str(tag) for tag in tags or [])


def new_register_request(asset: dict) -> dict:
    """Build a REGISTER request with a new random request ID. The server stores the ID
    with the asset, so sending the same request again can't register a second copy."""
    return {"action": "REGISTER", "request_id": uuid.uuid4().hex, "asset": asset}


def include_asset(assets: list, stored: object) -> list:
    """Return `assets`, adding the just-registered `stored` asset if the list lacks it. A
    server that has not synced yet may answer the list without an asset another one stored."""
    if not isinstance(stored, dict):
        return assets
    if any(asset.get("asset_id") == stored.get("asset_id") for asset in assets):
        return assets
    return assets + [stored]

# What the status line says when each kind of change succeeds.
SUCCESS_MESSAGES = {
    "register": "Asset registered successfully",
    "set_status": "Asset status updated",
    "edit": "Asset details updated",
    "delete": "Asset deleted",
    "review": "Review saved",
    "check_out": "Asset checked out",
    "check_in": "Asset checked in",
}

# How a ticked and an unticked row look in the review list.
TICKED, UNTICKED = "\u2611", "\u2610"

# The status filter's choice that shows every status.
ALL_STATUSES = "All statuses"

# The fields shown in the form and in the edit window, with their labels.
FIELD_LABELS = (
    ("description", "Description"),
    ("category", "Category"),
    ("location", "Location"),
    ("status", "Status"),
)


class AssetManagementApp:
    """The main window: a form to register assets, a table listing them, and buttons
    to change, edit or delete the selected one."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Asset Management")
        self.root.minsize(760, 480)
        self.results = queue.Queue()  # finished requests, waiting to be displayed

        self.fields = {}  # the form's text boxes, by asset field name
        self.unconfirmed_registration = None  # see register_asset()
        self.versions = {}  # the version of each asset the table shows, by asset ID
        self.assets_by_id = {}  # each asset the table shows, by asset ID
        self._build_ui()

        # Start checking for finished requests, then load the table.
        self.root.after(100, self._process_results)
        self.refresh_assets()

    def _build_ui(self) -> None:
        """Create the form, the table and the status line."""
        form = ttk.LabelFrame(self.root, text="Register an asset", padding=10)
        form.pack(fill=tk.X, padx=12, pady=(12, 8))

        # The server creates the asset ID, so there is no box for it.
        for row, (key, label) in enumerate(FIELD_LABELS):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            # Status is a read-only list of the only two statuses the server accepts.
            if key == "status":
                entry = ttk.Combobox(form, values=STATUSES, state="readonly")
                entry.set(IN_USE)
            else:
                entry = ttk.Entry(form)
            entry.grid(row=row, column=1, sticky=tk.EW, pady=3)
            self.fields[key] = entry

        form.columnconfigure(1, weight=1)
        self.register_button = ttk.Button(form, text="Register Asset", command=self.register_asset)
        self.register_button.grid(row=0, column=2, rowspan=2, padx=(12, 0), sticky=tk.N)
        self.refresh_button = ttk.Button(form, text="Refresh List", command=self.refresh_assets)
        self.refresh_button.grid(row=2, column=2, rowspan=2, padx=(12, 0), sticky=tk.N)

        listing = ttk.LabelFrame(self.root, text="Registered assets", padding=8)
        self._build_review_area()
        self.listing = listing
        listing.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # The search happens on the server, which only sends back the matching assets.
        finder = ttk.Frame(listing)
        finder.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))
        ttk.Label(finder, text="Search").pack(side=tk.LEFT)
        self.search_box = ttk.Entry(finder, width=30)
        self.search_box.pack(side=tk.LEFT, padx=(6, 12))
        self.search_box.bind("<Return>", lambda event: self.refresh_assets())
        ttk.Label(finder, text="Status").pack(side=tk.LEFT)
        self.status_filter = ttk.Combobox(finder, values=(ALL_STATUSES,) + STATUSES, state="readonly", width=14)
        self.status_filter.set(ALL_STATUSES)
        self.status_filter.pack(side=tk.LEFT, padx=(6, 12))
        self.status_filter.bind("<<ComboboxSelected>>", lambda event: self.refresh_assets())
        self.search_button = ttk.Button(finder, text="Search", command=self.refresh_assets)
        self.search_button.pack(side=tk.LEFT)
        self.clear_button = ttk.Button(finder, text="Clear", command=self.clear_search)
        self.clear_button.pack(side=tk.LEFT, padx=(8, 0))

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
        self.edit_button = ttk.Button(actions, text="Edit Details", command=self.edit_selected)
        self.edit_button.pack(side=tk.LEFT, padx=(8, 0))
        self.history_button = ttk.Button(actions, text="History", command=self.show_selected_history)
        self.history_button.pack(side=tk.LEFT, padx=(8, 0))
        self.check_button = ttk.Button(actions, text="Check Out / In", command=self.check_out_or_in_selected)
        self.check_button.pack(side=tk.LEFT, padx=(8, 0))
        self.loans_button = ttk.Button(actions, text="Loans", command=self.show_loans)
        self.loans_button.pack(side=tk.LEFT, padx=(8, 0))
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

        # Double-clicking an asset shows its history.
        self.tree.bind("<Double-1>", lambda event: self.show_selected_history())

        self.status_label = ttk.Label(self.root, text="Ready", anchor=tk.W)
        self.status_label.pack(fill=tk.X, padx=14, pady=(0, 10))

    def _build_review_area(self) -> None:
        """The "Flagged for review" area: assets added while the primary server was offline,
        each with a tick box, and buttons to approve or deny the ticked ones or all of them.
        It is only shown while there is something to review."""
        self.review_frame = ttk.LabelFrame(self.root, text="Flagged for review", padding=8)
        self.review_label = ttk.Label(self.review_frame, anchor=tk.W)
        self.review_label.pack(fill=tk.X)

        self.review_tree = ttk.Treeview(self.review_frame, columns=("tick", "asset_id", "description", "location"),
                                        show="headings", height=4, selectmode="none")
        for column, heading, width in (("tick", "", 40), ("asset_id", "Asset ID", 150),
                                       ("description", "Description", 260), ("location", "Location", 200)):
            self.review_tree.heading(column, text=heading)
            self.review_tree.column(column, width=width, minwidth=40, anchor=tk.W, stretch=column != "tick")
        self.review_tree.pack(fill=tk.X, pady=6)
        # Clicking a row ticks or unticks it, like a tick box.
        self.review_tree.bind("<Button-1>", self._toggle_review_row)
        self.review_ticked = set()

        buttons = ttk.Frame(self.review_frame)
        buttons.pack(fill=tk.X)
        self.review_all = tk.BooleanVar()
        ttk.Checkbutton(buttons, text="Select all", variable=self.review_all,
                        command=self._tick_all_reviews).pack(side=tk.LEFT)
        for text, decision, everything in (("Approve selected", "approve", False), ("Approve all", "approve", True),
                                           ("Deny selected", "deny", False), ("Deny all", "deny", True)):
            ttk.Button(buttons, text=text,
                       command=lambda d=decision, e=everything: self.review(d, e)).pack(side=tk.LEFT, padx=(8, 0))

    def _flagged_assets(self) -> list:
        return sorted((asset for asset in self.assets_by_id.values() if asset.get("flagged")),
                      key=lambda asset: asset["asset_id"])

    def _show_review(self) -> None:
        """Fill the review area from the assets the table shows, and hide it if none are flagged."""
        flagged = self._flagged_assets()
        self.review_ticked &= {asset["asset_id"] for asset in flagged}  # forget ones no longer flagged
        self.review_tree.delete(*self.review_tree.get_children())
        for asset in flagged:
            tick = TICKED if asset["asset_id"] in self.review_ticked else UNTICKED
            self.review_tree.insert("", tk.END, iid=asset["asset_id"],
                                    values=(tick, asset["asset_id"], asset.get("description", ""),
                                            asset.get("location", "")))
        self.review_label.configure(text=(
            "This asset was added while the primary server was offline." if len(flagged) == 1 else
            f"These {len(flagged)} assets were added while the primary server was offline."
        ) + " Check them, then approve or deny them.")
        self.review_all.set(bool(flagged) and len(self.review_ticked) == len(flagged))
        if flagged:
            self.review_frame.pack(fill=tk.X, padx=12, pady=(0, 4), before=self.listing)
        else:
            self.review_frame.pack_forget()

    def _toggle_review_row(self, event) -> str:
        asset_id = self.review_tree.identify_row(event.y)
        if asset_id:
            self.review_ticked ^= {asset_id}  # add it if missing, remove it if there
            self._show_review()
        return "break"  # stop the tree treating the click as anything else

    def _tick_all_reviews(self) -> None:
        self.review_ticked = {asset["asset_id"] for asset in self._flagged_assets()} if self.review_all.get() else set()
        self._show_review()

    def review(self, decision: str, everything: bool) -> None:
        """Approve or deny the ticked flagged assets, or all of them. Denying deletes, so it
        asks first."""
        flagged = [asset["asset_id"] for asset in self._flagged_assets()]
        chosen = flagged if everything else [asset_id for asset_id in flagged if asset_id in self.review_ticked]
        if not chosen:
            messagebox.showinfo("Nothing selected", "Tick the assets to review first.", parent=self.root)
            return
        if decision == "deny" and not messagebox.askyesno(
                "Deny assets", f"Deny {len(chosen)} asset(s)? Denied assets are deleted for good.",
                icon=messagebox.WARNING, parent=self.root):
            return
        action = "APPROVE" if decision == "approve" else "DELETE"
        self.review_ticked -= set(chosen)
        self._start_request("review", [{"action": action, "asset_id": asset_id,
                                        "expected_version": self.versions.get(asset_id)} for asset_id in chosen])

    def show_selected_history(self) -> None:
        """Show everything that has happened to the selected asset, newest first."""
        asset_id = self._selected_asset_id()
        if asset_id is None:
            return
        asset = self.assets_by_id.get(asset_id, {})
        window = tk.Toplevel(self.root)
        window.title(f"History of {asset_id}")
        window.transient(self.root)
        text = tk.Text(window, width=80, height=20, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        lines = [f"{asset.get('description', '')}, {asset.get('location', '')}", ""]
        for entry in reversed(asset.get("history") or []):
            who = f"by {entry['user']}, " if entry.get("user") else ""
            lines.append(f"{entry.get('at', '')}  {entry.get('detail', '')} ({who}on the {entry.get('server')} server)")
        if not asset.get("history"):
            lines.append("Nothing has been recorded for this asset yet.")
        text.insert(tk.END, "\n".join(lines))
        text.configure(state=tk.DISABLED)  # read-only
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 10))

    @staticmethod
    def current_loan(asset: dict) -> object:
        """The asset's current loan, or None. The last loan is current only if it has
        not been returned."""
        loans = asset.get("loans") or []
        if loans and loans[-1].get("returned") is None:
            return loans[-1]
        return None

    @staticmethod
    def parse_date(text: str) -> object:
        """Turn 'YYYY-MM-DD' into a date, or return None if it is not exactly that format."""
        try:
            date = datetime.datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None
        # strptime also accepts things like "2026-1-5", so insist on the exact format.
        return date if date.isoformat() == text else None

    def check_out_or_in_selected(self) -> None:
        """Check the selected asset in if it is on loan, otherwise open the check-out form."""
        asset_id = self._selected_asset_id()
        if asset_id is None:
            return
        asset = self.assets_by_id.get(asset_id, {})
        loan = self.current_loan(asset)
        if loan is None:
            self.open_check_out_window(asset_id)
        elif messagebox.askyesno(
            "Check in",
            f"Check {asset_id} in? It is on loan to {loan.get('borrower')}.",
            parent=self.root,
        ):
            self._start_request("check_in", {
                "action": "CHECK_IN",
                "asset_id": asset_id,
                "expected_version": self.versions.get(asset_id),
            })

    def open_check_out_window(self, asset_id: str) -> tuple:
        """Show the check-out form, returning the window, its boxes and the function that
        sends it. The dates start as today and a week from today."""
        window = tk.Toplevel(self.root)
        window.title(f"Check out {asset_id}")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        today = datetime.date.today()
        defaults = {
            "borrower": "",
            "start": today.isoformat(),
            "due": (today + datetime.timedelta(days=7)).isoformat(),
        }
        boxes = {}
        for row, (key, label) in enumerate((("borrower", "Borrower"), ("start", "Start (YYYY-MM-DD)"),
                                            ("due", "Due (YYYY-MM-DD)"))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            box = ttk.Entry(frame, width=30)
            box.insert(0, defaults[key])
            box.grid(row=row, column=1, sticky=tk.EW, pady=3)
            boxes[key] = box

        def save() -> None:
            values = {key: box.get().strip() for key, box in boxes.items()}
            # Check everything before sending, so the user sees the problem straight away.
            if not values["borrower"]:
                problem = "Please enter who is borrowing the asset."
            elif self.parse_date(values["start"]) is None or self.parse_date(values["due"]) is None:
                problem = "Dates must be written as YYYY-MM-DD, for example 2026-11-06."
            elif values["due"] < values["start"]:  # same format, so text order is date order
                problem = "The due date cannot be before the start date."
            else:
                problem = None
            if problem:
                messagebox.showwarning("Check out", problem, parent=window)
                return
            window.destroy()
            self._start_request("check_out", {
                "action": "CHECK_OUT",
                "asset_id": asset_id,
                "borrower": values["borrower"],
                "start": values["start"],
                "due": values["due"],
                "expected_version": self.versions.get(asset_id),
            })

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky=tk.E, pady=(10, 0))
        ttk.Button(buttons, text="Check Out", command=save).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side=tk.LEFT, padx=(8, 0))
        frame.columnconfigure(1, weight=1)
        return window, boxes, save

    def loan_rows(self) -> list:
        """One (asset ID, borrower, start, due, returned) row per loan of the assets the
        table shows, oldest start date first. A loan not yet returned shows "Overdue" once
        its due date has passed, and "On loan" before that."""
        today = datetime.date.today().isoformat()
        rows = []
        for asset in self.assets_by_id.values():
            for loan in asset.get("loans") or []:
                returned = loan.get("returned")
                if returned is None:
                    returned = "Overdue" if loan.get("due", "") < today else "On loan"
                rows.append((asset.get("asset_id", ""), loan.get("borrower", ""),
                             loan.get("start", ""), loan.get("due", ""), returned))
        # Dates are YYYY-MM-DD text, so sorting the text sorts by date.
        return sorted(rows, key=lambda row: (row[2], row[0]))

    def show_loans(self) -> object:
        """Open a window listing every loan of the assets the table shows."""
        window = tk.Toplevel(self.root)
        window.title("Loans")
        window.transient(self.root)
        columns = ("asset_id", "borrower", "start", "due", "returned")
        tree = ttk.Treeview(window, columns=columns, show="headings", height=15)
        for column, heading, width in (("asset_id", "Asset ID", 160), ("borrower", "Borrower", 160),
                                       ("start", "Start", 100), ("due", "Due", 100),
                                       ("returned", "Returned", 100)):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=60, anchor=tk.W)
        for row in self.loan_rows():
            tree.insert("", tk.END, values=row)
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 10))
        return tree

    def _set_busy(self, busy: bool) -> None:
        """Disable the buttons during a request, so it cannot be sent twice."""
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (
            self.register_button,
            self.refresh_button,
            self.search_button,
            self.clear_button,
            self.not_in_use_button,
            self.in_use_button,
            self.edit_button,
            self.check_button,
            self.delete_button,
        ):
            button.configure(state=state)

    def list_request(self) -> dict:
        """A LIST request for the assets matching the search box and status filter."""
        request = {"action": "LIST"}
        query = self.search_box.get().strip()
        if query:
            request["query"] = query
        if self.status_filter.get() != ALL_STATUSES:
            request["status"] = self.status_filter.get()
        return request

    def clear_search(self) -> None:
        self.search_box.delete(0, tk.END)
        self.status_filter.set(ALL_STATUSES)
        self.refresh_assets()

    def _start_request(self, operation: str, request: dict) -> None:
        """Send a request on a background thread, keeping the window responsive."""
        self._set_busy(True)
        self.status_label.configure(text="Connecting to asset server...")
        list_request = self.list_request()  # read on this thread, because it reads widgets
        filtered = len(list_request) > 1

        def run() -> None:
            # This runs on the background thread, so it must not touch any
            # widgets. It only puts its result on the queue.
            try:
                # A review sends one request per asset. Any that fail are reported together,
                # and the rest still go ahead.
                if operation == "review":
                    failures = []
                    for one in request:
                        reply = send_request(one)
                        if reply.get("status") != "OK":
                            failures.append(f"{one['asset_id']}: {reply.get('detail')}")
                    response = dict(reply, status="ERROR" if failures else "OK", detail="\n".join(failures))
                else:
                    response = send_request(request)
                assets = None
                error = None

                # After a successful change, fetch the list again so the table shows
                # it. A new asset is included even if the list came from a server
                # that has not synced it yet, unless a search is hiding other assets.
                if operation != "list" and response.get("status") == "OK":
                    try:
                        list_response = send_request(list_request)
                        if list_response.get("status") != "OK":
                            error = list_response.get("detail", "Could not refresh the asset list")
                        else:
                            assets = list_response.get("assets", [])
                            if not filtered:
                                assets = include_asset(assets, response.get("stored"))
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
        """Ask the server for the assets matching the search, or all of them."""
        self._start_request("list", self.list_request())

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
            self._start_request("set_status", {
                "action": "SET_STATUS",
                "asset_id": asset_id,
                "status": status,
                "expected_version": self.versions.get(asset_id),
            })

    def edit_selected(self) -> None:
        """Open a window to edit the details of the asset selected in the table."""
        asset_id = self._selected_asset_id()
        if asset_id is not None:
            self.open_edit_window(asset_id)

    def open_edit_window(self, asset_id: str) -> tuple:
        """Show the asset's details for editing, returning the window, its boxes and the
        function that saves it. Only the fields the user changes are sent, along with the
        version shown, so a change someone else has made meanwhile isn't overwritten."""
        asset = self.assets_by_id.get(asset_id, {})
        window = tk.Toplevel(self.root)
        window.title(f"Edit {asset_id}")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        boxes = {}
        for row, (key, label) in enumerate(FIELD_LABELS):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            if key == "status":
                box = ttk.Combobox(frame, values=STATUSES, state="readonly")
                box.set(asset.get("status", IN_USE))
            else:
                box = ttk.Entry(frame, width=45)
                box.insert(0, asset.get(key, ""))
            box.grid(row=row, column=1, sticky=tk.EW, pady=3)
            boxes[key] = box

        def save() -> None:
            values = {key: box.get().strip() for key, box in boxes.items()}
            missing = [label for key, label in FIELD_LABELS if not values[key]]
            if missing:
                messagebox.showwarning("Missing information", "Please fill in: " + ", ".join(missing), parent=window)
                return
            changes = {key: value for key, value in values.items() if value != asset.get(key)}
            window.destroy()
            if not changes:
                self.status_label.configure(text="Nothing was changed")
                return
            self._start_request("edit", {
                "action": "EDIT",
                "asset_id": asset_id,
                "changes": changes,
                "expected_version": self.versions.get(asset_id),
            })

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(FIELD_LABELS), column=0, columnspan=2, sticky=tk.E, pady=(10, 0))
        ttk.Button(buttons, text="Save Changes", command=save).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side=tk.LEFT, padx=(8, 0))
        frame.columnconfigure(1, weight=1)
        return window, boxes, save

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
            self._start_request("delete", {
                "action": "DELETE",
                "asset_id": asset_id,
                "expected_version": self.versions.get(asset_id),
            })

    def _finish_registration(self) -> None:
        """Clear the form after a registration the server has confirmed."""
        self.unconfirmed_registration = None
        for key, entry in self.fields.items():
            if key == "status":
                entry.set(IN_USE)  # a read-only list can't be emptied
            else:
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

                    # The table may be out of date, for example if someone else
                    # changed the asset, so show the user what is there now.
                    if operation in ("set_status", "edit", "delete", "review", "check_out", "check_in"):
                        self.refresh_assets()
                    continue

                # Showing which server answered makes a failover visible.
                served_by = f" (served by the {response.get('served_by', 'unknown')} server)"

                if operation != "list":
                    if operation == "register":
                        self._finish_registration()
                    self.status_label.configure(text=SUCCESS_MESSAGES[operation] + served_by)
                    self._show_assets(assets)
                else:
                    shown = response.get("assets", [])
                    self._show_assets(shown)
                    if self.list_request() == {"action": "LIST"}:
                        self.status_label.configure(text=f"Showing all {len(shown)} asset(s)" + served_by)
                    else:
                        self.status_label.configure(
                            text=f"Showing {len(shown)} matching asset(s)" + served_by
                        )
        except queue.Empty:
            pass  # nothing left to display for now

        # Check again shortly. after() does not block, so the window stays responsive.
        self.root.after(100, self._process_results)

    def _show_assets(self, assets: list) -> None:
        """Replace the table's contents with `assets`."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.versions = {asset.get("asset_id"): asset.get("version", 0) for asset in assets}
        self.assets_by_id = {asset.get("asset_id"): asset for asset in assets}

        self._show_review()

        # The server works out each asset's tags, so they are shown as stored.
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
                    format_asset_tags(asset.get("tags")),
                    asset.get("date_registered", ""),
                ),
            )


class LoginWindow(ttk.Frame):
    """The first screen: which server, and who is logging in.

    "Find servers" looks for servers on the local network, "Check" tests an address, and
    "Log in" does both and then logs in. The first certificate a server shows is trusted
    automatically, and after that only that exact certificate is accepted, so a different
    server at the same address is refused.
    Network work runs on a background thread, with results handed back through a queue,
    for the reason given at the top of this file.
    """

    def __init__(self, root: tk.Tk, on_login) -> None:
        super().__init__(root, padding=16)
        self.root = root
        self.on_login = on_login  # called, with no arguments, once logged in
        self.results = queue.Queue()  # (function to call, result, error) from background threads
        root.title("Asset Management - Log in")
        self.pack(fill=tk.BOTH, expand=True)

        ttk.Label(self, text="Server").grid(row=0, column=0, sticky=tk.W, pady=3)
        # Editable, with the addresses "Find servers" found as choices.
        self.server_box = ttk.Combobox(self, width=34)
        self.server_box.grid(row=0, column=1, sticky=tk.EW, pady=3, padx=(8, 0))
        self.find_button = ttk.Button(self, text="Find servers", command=self.find)
        self.find_button.grid(row=0, column=2, padx=(8, 0))
        self.check_button = ttk.Button(self, text="Check", command=self.check)
        self.check_button.grid(row=0, column=3, padx=(8, 0))

        ttk.Label(self, text="Username").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.username_box = ttk.Entry(self)
        self.username_box.grid(row=1, column=1, sticky=tk.EW, pady=3, padx=(8, 0))
        ttk.Label(self, text="Password").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.password_box = ttk.Entry(self, show="*")
        self.password_box.grid(row=2, column=1, sticky=tk.EW, pady=3, padx=(8, 0))
        self.password_box.bind("<Return>", lambda event: self.log_in())

        self.login_button = ttk.Button(self, text="Log in", command=self.log_in)
        self.login_button.grid(row=3, column=1, sticky=tk.E, pady=(10, 0))
        self.message = ttk.Label(self, text="Enter the server's address, or press Find servers.", wraplength=520)
        self.message.grid(row=4, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        self.columnconfigure(1, weight=1)

        self.root.after(100, self._process_results)

    # ---- Background work ----

    def _run(self, work, done) -> None:
        """Run work() on a thread, then call done(result, error) on the main thread."""
        self._set_busy(True)

        def target() -> None:
            try:
                self.results.put((done, work(), None))
            except (OSError, ValueError) as exc:
                self.results.put((done, None, exc))

        threading.Thread(target=target, daemon=True).start()

    def _process_results(self) -> None:
        try:
            while True:
                done, result, error = self.results.get_nowait()
                self._set_busy(False)
                done(result, error)
        except queue.Empty:
            pass
        if self.winfo_exists():  # stop checking once logged in and this screen is gone
            self.root.after(100, self._process_results)

    def _set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (self.find_button, self.check_button, self.login_button):
            button.configure(state=state)

    def _say(self, text: str) -> None:
        self.message.configure(text=text)

    # ---- The buttons ----

    def find(self) -> None:
        self._say("Looking for servers...")
        self._run(discover, self._found)

    def _found(self, addresses: list, error: object) -> None:
        if error or not addresses:
            self._say("No servers were found. Type the address of the server instead.")
            return
        shown = [f"{host}:{port}" for host, port in addresses]
        self.server_box.configure(values=shown)
        self.server_box.set(shown[0])
        self._say(f"Found {len(shown)} server(s). Check the address, then log in.")

    def check(self) -> None:
        """Test the address, without logging in."""
        self._connect(lambda: self._say(
            f"The server answered. It knows of {len(session.servers)} server(s); you can log in."))

    def log_in(self) -> None:
        username, password = self.username_box.get().strip(), self.password_box.get()
        if not username or not password:
            self._say("Enter your username and password.")
            return
        self._connect(lambda: self._send_login(username, password))

    def _connect(self, then) -> None:
        """Check the typed address, get the user's trust in its certificate, and learn where
        every server is, then call then()."""
        try:
            address = parse_address(self.server_box.get())
        except ValueError as exc:
            self._say(str(exc))
            return
        self._say("Connecting...")
        self._run(lambda: probe(address), lambda result, error: self._trust(address, result, error, then))

    def _trust(self, address: tuple, result: object, error: object, then) -> None:
        if error:
            self._say(f"Could not connect to {address[0]}:{address[1]}: {error}")
            return
        found, reply = result
        if session.trusted != found:
            if session.trusted is not None:
                self._say("This server's certificate is different from the one you trusted, so it was not used.")
                return
            # First connection: trust the certificate silently (no question for the user).
            # From now on only this exact certificate is accepted.
            session.trusted = found
        if reply.get("status") != "OK":
            self._say(f"The server did not answer properly: {reply.get('detail')}")
            return
        # It is trusted, so use the reply: it says where every server is, so one address is enough.
        session.servers = servers_from_ping(address, reply)
        then()

    def _send_login(self, username: str, password: str) -> None:
        self._say("Logging in...")
        self._run(lambda: send_request({"action": "LOGIN", "username": username, "password": password}),
                  self._logged_in)

    def _logged_in(self, reply: object, error: object) -> None:
        self.password_box.delete(0, tk.END)  # don't leave the password in the window
        if error:
            self._say(str(error))
        elif reply.get("status") != "OK":
            self._say(reply.get("detail", "The login failed."))
        else:
            session.token = reply["token"]
            session.username, session.role = reply["username"], reply["role"]
            self.destroy()
            self.on_login()


def main() -> None:
    root = tk.Tk()

    def start() -> None:
        root.title(f"Asset Management - {session.username} ({session.role})")
        AssetManagementApp(root)

    LoginWindow(root, start)
    root.mainloop()


if __name__ == "__main__":
    main()

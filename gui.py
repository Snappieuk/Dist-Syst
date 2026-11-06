"""
Tkinter client for the Asset Management System. A single file: copy it to any
computer with Python 3.8+ and run it.

    python gui.py

On the login screen, enter the server's IP address or host name (add :port if it
isn't the usual one), or press "Find servers" to look for them on the local
network. "Check" tests an address before you log in. The backup address is
optional; if it's given, the GUI moves to the backup when the primary can't be
reached.

Certificates: the first time you connect to a server, the GUI shows the
fingerprint of its certificate and asks whether to trust it (trust on first use).
After that it only accepts that exact certificate, so a different server at the
same address is refused. Server addresses and trusted certificates are remembered
in .asset_management_gui.json in your home folder.

What you can do depends on your account; the server checks every request.
"""

import hashlib
import ipaddress
import itertools
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

# ---- Must match the server (common.py and server.py) ----------------------------
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1048576
ASSET_FIELDS = ("asset_id", "description", "category", "location", "status")
EDITABLE_FIELDS = ("description", "category", "location", "status")
STATUSES = ("In Use", "Not In Use")
DEFAULT_PORTS = {"primary": 65432, "backup": 65433}
DISCOVERY_REQUEST = b"ASSET_MANAGEMENT_DISCOVER"
SERVICE_NAME = "asset-management"
# ---------------------------------------------------------------------------------

TIMEOUT_SECONDS = 5
DISCOVERY_SECONDS = 1.5
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".asset_management_gui.json")
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
HOST_NAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


class UntrustedServer(Exception):
    """The server's certificate is not one this GUI trusts."""


# ---- Addresses ------------------------------------------------------------------

def parse_address(text: str, default_port: int) -> tuple:
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
        if re.fullmatch(r"[\d.]+", host) or not HOST_NAME.fullmatch(host):
            raise ValueError(f"'{host}' is not a valid IP address or host name.") from None
    return host, int(port_text) if has_port else default_port


def show_address(address: tuple) -> str:
    return f"{address[0]}:{address[1]}"


# ---- Talking to a server ----------------------------------------------------------

def fingerprint(certificate: bytes) -> str:
    """The SHA-256 fingerprint of a certificate, as people compare it: AB:CD:..."""
    digest = hashlib.sha256(certificate).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def connect(address: tuple) -> tuple:
    """Open an encrypted connection and return it with the server's certificate fingerprint.

    Python's usual certificate check is switched off because these servers use their own
    certificate; the caller checks the fingerprint against the trusted ones instead,
    before anything is sent.
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
    return sock, fingerprint(sock.getpeercert(binary_form=True))


def receive_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("The server closed the connection")
        data += chunk
    return data


def exchange(sock: socket.socket, request: dict) -> dict:
    """Send one request as length-prefixed JSON and return the reply."""
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
    return reply


def probe(address: tuple) -> tuple:
    """Check that an address is an Asset Management server. Returns (role, fingerprint).
    Only PING is sent, which needs no login, so nothing secret goes to an untrusted server."""
    sock, print_ = connect(address)
    with sock:
        reply = exchange(sock, {"action": "PING"})
    if reply.get("service") != SERVICE_NAME:
        raise ValueError(f"{show_address(address)} answered, but it isn't an Asset Management server.")
    return reply.get("role", "server"), print_


def explain(exc: Exception) -> str:
    """A short, plain reason a connection failed."""
    if isinstance(exc, socket.gaierror):
        return "no such host name"
    if isinstance(exc, (ConnectionRefusedError, socket.timeout, TimeoutError)):
        return "nothing answered there"
    if isinstance(exc, ssl.SSLError):
        return "not an Asset Management server (no TLS)"
    return str(exc)


def send_to_servers(addresses: list, trusted: dict, request: dict) -> tuple:
    """Send a request to the primary, or the backup if the primary can't be reached.
    Returns (reply, address).

    The certificate is checked before anything is sent. A server not checked at login
    is accepted only if it has the same certificate as one already trusted (the
    primary and backup share one).
    """
    last_error = OSError("No server address")
    for address in addresses:
        try:
            sock, print_ = connect(address)
        except OSError as exc:
            last_error = exc
            continue
        with sock:
            key = show_address(address)
            if trusted.get(key, print_) != print_ or print_ not in trusted.values():
                raise UntrustedServer(f"The server at {key} is not trusted. Log in again to check it.")
            return exchange(sock, request), address
    raise last_error


def check_addresses(addresses: list, trusted: dict) -> list:
    """What "Check" reports for each address."""
    results = []
    for address in addresses:
        try:
            role, print_ = probe(address)
            known = trusted.get(show_address(address)) == print_
            results.append(f"{show_address(address)}: {role} server found" + ("" if known else " (certificate not yet trusted)"))
        except (OSError, ValueError) as exc:
            results.append(f"{show_address(address)}: {explain(exc)}")
    return results


def probe_addresses(addresses: list) -> list:
    """[(address, fingerprint)] for each address that is an Asset Management server now."""
    found = []
    for address in addresses:
        try:
            found.append((address, probe(address)[1]))
        except (OSError, ValueError):
            pass  # a backup that's down now is checked again when it's needed
    if not found:
        raise ConnectionError("no server answered at that address")
    return found


def run_in_thread(replies: queue.Queue, key: int, work) -> None:
    """Do `work` and queue its result (or exception) for the window. It is given no
    window objects, so Tk is only ever touched from the window's own thread."""
    try:
        result = work()
    except Exception as exc:  # handed back to the window, which reports it
        result = exc
    replies.put((key, result))


def discover(ports: list) -> list:
    """Broadcast on the local network and return the servers that answer:
    [{"role", "host", "port"}], primary first."""
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        for port in ports:
            for target in ("<broadcast>", "127.0.0.1"):  # this machine too
                try:
                    sock.sendto(DISCOVERY_REQUEST, (target, port))
                except OSError:
                    pass
        deadline = time.monotonic() + DISCOVERY_SECONDS
        while time.monotonic() < deadline:
            try:
                data, (host, _port) = sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                break
            if not isinstance(info, dict) or info.get("service") != SERVICE_NAME:
                continue
            key = (info.get("role"), info.get("port"))
            # The same server can answer on 127.0.0.1 and its network address; keep the
            # network one, which works from other machines too.
            if key not in found or found[key]["host"].startswith("127."):
                found[key] = {"role": info.get("role"), "host": host, "port": info.get("port")}
    return sorted(found.values(), key=lambda server: server["role"] != "primary")


# ---- Remembered settings ----------------------------------------------------------

def load_settings(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as file:
            settings = json.load(file)
    except (OSError, ValueError):
        return {"server": "", "backup": "", "trusted": {}}
    if not isinstance(settings, dict) or not isinstance(settings.get("trusted"), dict):
        return {"server": "", "backup": "", "trusted": {}}
    return settings


def save_settings(path: str, settings: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(settings, file, indent=2)
    except OSError:
        pass  # not being able to remember is no reason to stop working


class AssetApp:
    """The application window: a login screen, then the asset screen."""

    def __init__(self, root: tk.Tk, settings_file: str = SETTINGS_FILE) -> None:
        self.root = root
        self.settings_file = settings_file
        self.settings = load_settings(settings_file)
        self.addresses = []   # [primary, backup?] as (host, port)
        self.token = None
        self.role = None
        self.page = 1
        self.search = {}      # the LIST parameters of the search being shown
        self.shown = {}       # the assets in the table, by asset_id
        self.notice = None    # a message to show once the table has refreshed
        self.replies = queue.Queue()  # finished background work: (key, result)
        self.pending = {}             # what to do with each result, by key
        self.keys = itertools.count()

        root.title("Asset Management System")
        root.minsize(900, 500)
        self.status_text = tk.StringVar(value="Enter a server, or press Find servers, then log in.")
        self.build_login()
        self.build_main()
        ttk.Label(root, textvariable=self.status_text, anchor=tk.W, relief=tk.SUNKEN).pack(side=tk.BOTTOM, fill=tk.X)
        self.login_frame.pack(padx=20, pady=20)
        self.check_replies()

    # ---- Layout ---------------------------------------------------------------

    def build_login(self) -> None:
        frame = self.login_frame = ttk.LabelFrame(self.root, text="Connect and log in", padding=12)
        ttk.Label(frame, text="Server").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.server_entry = ttk.Entry(frame, width=30)
        self.server_entry.insert(0, self.settings.get("server", ""))
        self.server_entry.grid(row=0, column=1, pady=3)
        ttk.Button(frame, text="Check", command=self.check_server).grid(row=0, column=2, padx=4)
        ttk.Button(frame, text="Find servers", command=self.find_servers).grid(row=0, column=3)
        ttk.Label(frame, text="Backup (optional)").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.backup_entry = ttk.Entry(frame, width=30)
        self.backup_entry.insert(0, self.settings.get("backup", ""))
        self.backup_entry.grid(row=1, column=1, pady=3)
        ttk.Label(frame, text="Username").grid(row=2, column=0, sticky=tk.W, pady=(12, 3))
        self.username_entry = ttk.Entry(frame, width=30)
        self.username_entry.grid(row=2, column=1, pady=(12, 3))
        ttk.Label(frame, text="Password").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.password_entry = ttk.Entry(frame, width=30, show="*")
        self.password_entry.grid(row=3, column=1, pady=3)
        self.login_button = ttk.Button(frame, text="Log in", command=self.log_in)
        self.login_button.grid(row=4, column=1, sticky=tk.E, pady=(8, 0))
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
        self.server_label = ttk.Label(paging, text="")
        self.server_label.pack(side=tk.RIGHT)

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

    # ---- Background work ------------------------------------------------------

    def in_background(self, work, on_done) -> None:
        """Run `work` on a background thread so the window never freezes; `on_done`
        gets its result (or the exception) on the window's own thread. `work` must not
        use the window."""
        key = next(self.keys)
        self.pending[key] = on_done
        self.status_text.set("Working...")
        threading.Thread(target=run_in_thread, args=(self.replies, key, work), daemon=True).start()

    def check_replies(self) -> None:
        """Hand finished work to its callback. Tk may only be used from its own thread."""
        while not self.replies.empty():
            key, result = self.replies.get()
            self.pending.pop(key)(result)
        self.root.after(50, self.check_replies)

    # ---- Finding and checking servers -----------------------------------------

    def read_addresses(self) -> list:
        """The addresses typed in, checked for format. Raises ValueError."""
        addresses = [parse_address(self.server_entry.get(), DEFAULT_PORTS["primary"])]
        if self.backup_entry.get().strip():
            addresses.append(parse_address(self.backup_entry.get(), DEFAULT_PORTS["backup"]))
        return addresses

    def find_servers(self) -> None:
        def done(result) -> None:
            if isinstance(result, Exception) or not result:
                self.status_text.set("No servers found on this network. Enter the address by hand.")
                return
            for server in result:
                entry = self.server_entry if server["role"] == "primary" else self.backup_entry
                entry.delete(0, tk.END)
                entry.insert(0, f"{server['host']}:{server['port']}")
            found = ", ".join(f"{s['role']} at {s['host']}:{s['port']}" for s in result)
            self.status_text.set(f"Found {found}.")

        self.in_background(lambda: discover(list(DEFAULT_PORTS.values())), done)

    def check_server(self) -> None:
        try:
            addresses = self.read_addresses()
        except ValueError as exc:
            self.status_text.set(str(exc))
            return

        trusted = dict(self.settings["trusted"])
        self.in_background(lambda: check_addresses(addresses, trusted),
                           lambda results: self.status_text.set("; ".join(results)))

    def trust(self, address: tuple, print_: str) -> bool:
        """Decide whether to trust a server's certificate (trust on first use).

        A certificate already trusted, for this address or another, is accepted. A
        different one at a trusted address is refused outright: it may be someone
        pretending to be the server. Otherwise the user is asked, and shown the
        fingerprint to compare with the one the administrator gave out.
        """
        key = show_address(address)
        pinned = self.settings["trusted"].get(key)
        if pinned == print_:
            return True
        if pinned is None and print_ in self.settings["trusted"].values():
            # Same certificate as a server already trusted: the primary and backup share one.
            self.settings["trusted"][key] = print_
            save_settings(self.settings_file, self.settings)
            return True
        if pinned is not None:
            messagebox.showerror(
                "Certificate changed",
                f"The server at {key} has a different certificate from last time, so it was not "
                f"trusted. If your administrator replaced it, delete {self.settings_file} and connect again.",
            )
            return False
        if not messagebox.askyesno(
            "Trust this server?",
            f"First connection to {key}.\n\nCertificate fingerprint:\n{print_}\n\n"
            "Only trust it if this matches the fingerprint your administrator gave you.",
        ):
            return False
        self.settings["trusted"][key] = print_
        save_settings(self.settings_file, self.settings)
        return True

    # ---- Requests -------------------------------------------------------------

    def send(self, request: dict, on_reply) -> None:
        """Send a request in the background; `on_reply` gets the server's reply."""
        if self.token is not None:
            request = {**request, "token": self.token}

        def done(result) -> None:
            if isinstance(result, Exception):
                message = str(result) if isinstance(result, UntrustedServer) else (
                    f"Could not reach the server: {explain(result)}")
                on_reply({"status": "ERROR", "code": "NO_SERVER", "message": message})
                return
            reply, address = result
            backup = len(self.addresses) > 1 and address == self.addresses[1]
            self.server_label.configure(text=f"Server: {show_address(address)}" + (" (backup)" if backup else ""))
            if reply.get("code") == "UNAUTHENTICATED" and self.token is not None:
                self.status_text.set(reply["message"])
                self.show_login()
                return
            on_reply(reply)

        addresses, trusted = list(self.addresses), dict(self.settings["trusted"])
        self.in_background(lambda: send_to_servers(addresses, trusted, request), done)

    def report_error(self, reply: dict) -> None:
        self.status_text.set(f"{reply.get('code')}: {reply.get('message')}")

    # ---- Logging in -----------------------------------------------------------

    def log_in(self) -> None:
        """Check the servers and their certificates, then log in."""
        try:
            addresses = self.read_addresses()
        except ValueError as exc:
            self.status_text.set(str(exc))
            return
        self.token = None

        def checked(result) -> None:
            if isinstance(result, Exception):
                self.status_text.set(f"Could not connect: {explain(result)}")
                return
            for address, print_ in result:
                if not self.trust(address, print_):
                    self.status_text.set(f"Not connected: {show_address(address)} was not trusted.")
                    return
            self.addresses = addresses
            self.settings["server"] = self.server_entry.get().strip()
            self.settings["backup"] = self.backup_entry.get().strip()
            save_settings(self.settings_file, self.settings)
            self.send({
                "action": "LOGIN",
                "username": self.username_entry.get().strip(),
                "password": self.password_entry.get(),
            }, self.logged_in)

        self.in_background(lambda: probe_addresses(addresses), checked)

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
        request = {"action": "REGISTER", "request_id": uuid.uuid4().hex, "asset": values}
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
            "request_id": uuid.uuid4().hex,
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
            "request_id": uuid.uuid4().hex,
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

"""
Asset Management System server.

    python server.py            (the primary server)
    python server.py backup     (the backup server)

Understands these actions:
    {"action": "PING"}                                         -> this server's role, and both servers' addresses
    {"action": "LOGIN", "username": "...", "password": "..."}  -> a session token
    {"action": "REGISTER", "request_id": "...", "asset": {...}}
    {"action": "LIST", "query": "...", "filters": {"location": "..."}, "page": 1, "page_size": 20}
    {"action": "EDIT", "request_id": "...", "asset_id": "...", "expected_version": 1,
     "changes": {"description": "...", "category": "...", "location": "...", "status": "..."}}
    {"action": "DELETE", "request_id": "...", "asset_id": "...", "expected_version": 1}

Every request must include "protocol_version", every request except PING and LOGIN
must include the session "token", and every change must include a unique "request_id".
Only users with the admin role may DELETE. Every reply has "status": "OK" or
"status": "ERROR" with a stable "code", a "message" and a "context".

Replication: the primary copies its data to the backup after every change, and
every sync_interval_seconds. Clients try the primary first and the backup if the
primary can't be reached, so the backup keeps the service running. When the primary
comes back, it takes over any changes the backup made meanwhile. The servers use
these actions between themselves, authenticated with a key derived from secret.key:
    SYNC_STATUS -> the server's change number and a digest of its data
    STATE       -> all of the server's data
    REPLICATE   -> replace the server's data with the copy sent

Discovery: each server also answers UDP broadcasts of DISCOVERY_REQUEST on the same
port number as its TCP port, so gui.py can find servers on the local network without
being told their addresses. Set "discoverable" to False in the settings (common.py) to turn it off.
"""

import datetime     # creates the server's timestamps
import hashlib      # compares the two servers' data
import hmac         # checks the key the servers use with each other
import json         # the data file is JSON
import logging      # records errors and events with timestamps
import os           # saves the data file safely
import re           # checks the format of asset IDs
import socket       # provides the networking building blocks (sockets)
import ssl          # encrypts every connection with TLS
import sys          # reads which server to run from the command line
import threading    # one thread per client, and a lock for the shared data
from typing import Optional

# users.py (accounts and login tokens) is in the tools folder.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))

import common
import users
from common import (
    ASSET_FIELDS,
    BAD_REQUEST,
    CONFLICT,
    DUPLICATE_ASSET,
    EDITABLE_FIELDS,
    FORBIDDEN,
    INTERNAL,
    NOT_FOUND,
    PROTOCOL_VERSION,
    STATUSES,
    TOO_LARGE,
    UNAUTHENTICATED,
    UNKNOWN_ACTION,
    UNSUPPORTED_VERSION,
    MessageTooLargeError,
    error_reply,
    receive_message,
    send_message,
)

SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)

# An asset ID is 1 to 32 letters, digits, dashes or underscores, starting with a
# letter or digit, e.g. AST-0001.
ASSET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")

# The longest text accepted in any field.
MAX_FIELD_LENGTH = 200

# How many recent request IDs are remembered, so that retries are recognised. Old ones
# are forgotten so the data file can't grow forever; a retry comes within seconds.
MAX_REMEMBERED_REQUESTS = 1000

# The most assets one page of a LIST reply may hold.
MAX_PAGE_SIZE = 100

# What a client broadcasts to find servers, and what identifies this service.
DISCOVERY_REQUEST = b"ASSET_MANAGEMENT_DISCOVER"
SERVICE_NAME = "asset-management"

# Actions only the servers use with each other.
SERVER_ACTIONS = ("SYNC_STATUS", "STATE", "REPLICATE")

# Actions that change the data, and so must carry a request_id.
CHANGE_ACTIONS = ("REGISTER", "EDIT", "DELETE")

# The roles allowed to perform each action. Anything not listed is open to every
# logged-in user.
ALLOWED_ROLES = {"DELETE": ("admin",)}

logger = logging.getLogger("asset_server")


class InvalidRequest(ValueError):
    """A request breaks a rule. Carries the error code and the field concerned."""

    def __init__(self, message: str, code: str = BAD_REQUEST, **context) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


def utc_now() -> str:
    """The server's current time, in the one format used for every timestamp:
    UTC ISO 8601 to the second, e.g. 2026-11-05T21:14:03Z. Clients' clocks may be
    wrong or in other time zones, so timestamps are only ever made here."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_text(value: object, field: str) -> str:
    """Return a field's text without surrounding spaces, or raise InvalidRequest."""
    if value is None:
        raise InvalidRequest(f"{field} is required", field=field)
    if not isinstance(value, str):
        raise InvalidRequest(f"{field} must be text", field=field)
    value = value.strip()
    if not value:
        raise InvalidRequest(f"{field} must not be empty", field=field)
    if len(value) > MAX_FIELD_LENGTH:
        raise InvalidRequest(f"{field} must be at most {MAX_FIELD_LENGTH} characters", field=field)
    return value


def check_status(value: object) -> str:
    """Return the standard spelling of an allowed status, or raise InvalidRequest."""
    text = check_text(value, "status")
    for status in STATUSES:
        if text.lower() == status.lower():
            return status
    raise InvalidRequest(f"status must be one of: {', '.join(STATUSES)}", field="status", allowed=list(STATUSES))


def check_version(value: object) -> int:
    """Return the expected_version a client sent, or raise InvalidRequest."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest("expected_version must be a whole number of at least 1", field="expected_version")
    return value


def check_request_id(value: object) -> str:
    """Return a change's request_id, or raise InvalidRequest."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise InvalidRequest("Every change needs a request_id of 1-64 characters", field="request_id")
    return value


def fingerprint(request: dict) -> str:
    """What a request asks for, ignoring the parts that may differ between retries."""
    ignored = ("protocol_version", "request_id", "token")
    return json.dumps({key: value for key, value in request.items() if key not in ignored}, sort_keys=True)


def validate_asset(asset: object) -> dict:
    """Return a clean copy of a client's asset, or raise InvalidRequest.

    This happens on the server even if a client checks too, because anyone can write
    a program that speaks this protocol. Fields the client is not allowed to set,
    such as timestamps, are dropped.
    """
    if not isinstance(asset, dict):
        raise InvalidRequest("asset must be an object", field="asset")
    clean = {field: check_text(asset.get(field), field) for field in ASSET_FIELDS}
    if not ASSET_ID_PATTERN.fullmatch(clean["asset_id"]):
        raise InvalidRequest(
            "asset_id must be 1-32 letters, digits, dashes or underscores, starting with a letter or digit",
            field="asset_id",
        )
    clean["status"] = check_status(clean["status"])
    return clean


class AssetServer:
    """One asset server: its data and the rules for changing it."""

    def __init__(self, config: dict, role: str = "primary") -> None:
        self.config = config
        self.role = role
        self.address = (config["servers"][role]["host"], config["servers"][role]["port"])
        # Where to listen. 0.0.0.0 means every network connection, so clients on other
        # machines can connect; 127.0.0.1 would mean this machine only.
        self.listen_address = (config.get("listen_host", self.address[0]), self.address[1])
        self.max_bytes = config["max_message_bytes"]
        self.timeout = config["server_timeout_seconds"]
        self.data_file = common.config_path(config, config["data_files"][role])
        self.audit_file = common.config_path(config, config["audit_files"][role])
        self.audit_lock = threading.Lock()  # one line at a time from many threads
        self.session_seconds = config["session_seconds"]
        self.secret = users.read_secret(config)  # signs and checks session tokens

        # Every connection is encrypted with TLS 1.2 or later, using the system's
        # certificate and private key (made by certificate.py).
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls.load_cert_chain(
            common.config_path(config, config["certificate_file"]),
            common.config_path(config, config["private_key_file"]),
        )

        # Every client has its own thread, so two requests can arrive at the same
        # moment. The lock makes each read or change of the data one indivisible
        # step, so they cannot interfere with each other.
        self.lock = threading.Lock()

        self.assets = {}    # every asset, by asset_id, including soft-deleted ones
        self.requests = {}  # recent request IDs: {request_id: {"fingerprint", "reply"}}
        self.seq = 0        # counts every change, so the servers can tell whose data is newer
        self.load()

        # Replication (only the primary starts it; see sync_with_backup).
        self.server_key = users.server_key(self.secret)
        self.sync_interval = config["sync_interval_seconds"]
        self.changed = threading.Event()  # set after each change, to copy it at once
        self.backup_reachable = None      # unknown until the first attempt

    # ---- Audit trail ----------------------------------------------------------

    def audit(self, event: str, **details) -> None:
        """Append one line to the audit log: a JSON object with the server's UTC time,
        this server's role, the event and its details (user, request ID, asset ID...).

        JSON Lines keeps each record on one line, readable by people and by programs.
        A failure to write is logged but never fails the request it describes.
        """
        record = {"time": utc_now(), "server": self.role, "event": event, **details}
        try:
            with self.audit_lock, open(self.audit_file, "a", encoding="utf-8") as file:
                file.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.error("Could not write to the audit log: %s", exc)

    # ---- Storage --------------------------------------------------------------

    def load(self) -> None:
        """Read the data file. It starts empty if there is no file yet.

        A damaged file stops the server rather than starting empty, because the next
        save would overwrite whatever could still be recovered.
        """
        try:
            with open(self.data_file, encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return
        if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            raise ValueError(f"{self.data_file} does not contain valid asset data")
        self.assets = {asset["asset_id"]: asset for asset in data["assets"]}
        self.requests = data.get("requests", {})
        self.seq = data.get("seq", 0)

    def save(self) -> None:
        """Write all the data to disk. The caller must hold the lock.

        Writing straight over the real file would empty it first, so a crash part-way
        through would lose everything. Instead the data goes into a temporary file,
        which is forced onto the disk, then os.replace() swaps it into place in one
        atomic step. The file is always a complete old or a complete new version.
        """
        temp_path = self.data_file + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self.snapshot(), file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, self.data_file)

    def snapshot(self) -> dict:
        """All of this server's data, as saved to disk and copied between servers.
        The caller must hold the lock."""
        return {"seq": self.seq, "assets": list(self.assets.values()), "requests": self.requests}

    def commit(self, undo) -> None:
        """Save a change just made in memory. The caller must hold the lock.

        If the save fails, `undo` puts memory back as it was, so memory and disk still
        agree and the client, which will be told this failed, never sees the change.
        """
        try:
            self.save()
        except OSError:
            undo()
            raise

    # ---- Requests -------------------------------------------------------------

    def process(self, request: dict) -> dict:
        """Carry out one decoded request and return the reply. No networking happens
        here, so the rules can be tested without a socket."""
        version = request.get("protocol_version")
        if version not in SUPPORTED_VERSIONS:
            self.audit("rejected_version", action=request.get("action"), protocol_version=version)
            return error_reply(
                UNSUPPORTED_VERSION,
                f"Protocol version {version!r} is not supported",
                supported_versions=list(SUPPORTED_VERSIONS),
            )

        action = request.get("action")
        handlers = {
            "REGISTER": self.register,
            "EDIT": self.edit,
            "DELETE": self.delete,
            "LIST": self.list_assets,
        }
        if action not in handlers and action not in ("PING", "LOGIN") + SERVER_ACTIONS:
            return error_reply(UNKNOWN_ACTION, f"Unknown action: {action!r}", action=action)
        try:
            if action == "PING":
                # Lets a client check an address really is one of these servers, and
                # tells it where both servers are, so it can move to the backup by
                # itself if the primary fails. Needs no login; reveals no data.
                return {
                    "status": "OK",
                    "service": SERVICE_NAME,
                    "role": self.role,
                    "servers": self.config["servers"],
                }
            if action == "LOGIN":
                return self.login(request)
            if action in SERVER_ACTIONS:
                return self.server_action(request)
            user = self.authorise(request)
            handler = handlers[action]
            if action in CHANGE_ACTIONS:
                return self.change(request, handler, user)
            return handler(request, user)
        except InvalidRequest as exc:
            return error_reply(exc.code, str(exc), **exc.context)

    # ---- Accounts -------------------------------------------------------------

    def login(self, request: dict) -> dict:
        """Check a username and password, and issue a time-limited session token.

        An unknown username gets the same reply, after the same slow hashing, as a wrong
        password, so neither the reply nor its timing reveals which usernames exist.
        """
        username = request.get("username")
        password = request.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise InvalidRequest("username and password must be text", field="username")
        username = username.strip().lower()
        record = users.load_users(self.config).get(username)
        if record is None:
            users.hash_password(password)  # same work as a real check
            matched = False
        else:
            matched = users.password_matches(record, password)
        if not matched or record is None:
            logger.warning("Failed login for %r", username)
            self.audit("login_failed", user=username)
            raise InvalidRequest("Wrong username or password", code=UNAUTHENTICATED)
        token, expires = users.issue_token(username, self.secret, self.session_seconds)
        logger.info("%s logged in", username)
        self.audit("login", user=username, role=record["role"])
        return {
            "status": "OK",
            "token": token,
            "username": username,
            "role": record["role"],
            "expires_at": datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def authorise(self, request: dict) -> dict:
        """Return the user making a request, or raise InvalidRequest.

        The token proves who the user is; their role is looked up now rather than read
        from the token, so changing or removing an account takes effect at once.
        """
        try:
            username = users.read_token(request.get("token"), self.secret)
        except users.TokenError as exc:
            self.audit("unauthenticated", action=request.get("action"), request_id=request.get("request_id"), reason=str(exc))
            raise InvalidRequest(str(exc), code=UNAUTHENTICATED) from exc
        record = users.load_users(self.config).get(username)
        if record is None:
            self.audit("unauthenticated", action=request.get("action"), user=username, reason="account removed")
            raise InvalidRequest("This account no longer exists. Please log in again.", code=UNAUTHENTICATED)
        user = {"username": username, "role": record["role"]}

        action = request.get("action")
        allowed = ALLOWED_ROLES.get(action) if isinstance(action, str) else None
        if allowed and user["role"] not in allowed:
            logger.warning("%s (%s) was refused %s", username, user["role"], request.get("action"))
            self.audit(
                "forbidden",
                action=request.get("action"),
                request_id=request.get("request_id"),
                user=username,
                role=user["role"],
                asset_id=request.get("asset_id"),
            )
            raise InvalidRequest(
                f"Only {' or '.join(allowed)} users may {request.get('action')}",
                code=FORBIDDEN,
                role=user["role"],
                allowed_roles=list(allowed),
            )
        return user

    # ---- Changes --------------------------------------------------------------

    def change(self, request: dict, handler, user: dict) -> dict:
        """Apply a change exactly once, however many times the request is sent.

        The handler makes the change in memory and returns its reply and a way to undo
        it. The request ID and reply are remembered, then both are saved together, so
        a retry after a lost reply gets the original reply and the change isn't made
        twice. Everything happens under the lock, so two copies of one request
        arriving at once can't both be applied.
        """
        request_id = check_request_id(request.get("request_id"))
        with self.lock:
            previous = self.requests.get(request_id)
            if previous is not None:
                if previous["fingerprint"] != fingerprint(request):
                    raise InvalidRequest("This request_id was already used for a different request", field="request_id")
                return previous["reply"]

            reply, undo = handler(request, user)

            def undo_all() -> None:
                undo()
                del self.requests[request_id]
                self.seq -= 1

            self.requests[request_id] = {"fingerprint": fingerprint(request), "reply": reply}
            self.seq += 1
            self.commit(undo_all)
            self.changed.set()  # copy it to the backup straight away
            while len(self.requests) > MAX_REMEMBERED_REQUESTS:
                del self.requests[next(iter(self.requests))]  # forget the oldest
            stored = reply["stored"]
            self.audit(
                request["action"].lower(),
                user=user["username"],
                role=user["role"],
                request_id=request_id,
                asset_id=stored["asset_id"],
                version=stored["version"],
                fields=sorted(request.get("changes", {})) or None,
            )
        return reply

    def current_asset(self, asset_id: object, expected_version: object) -> dict:
        """Return the asset a change applies to. The caller must hold the lock.

        The change must name the version the client last saw. If someone else has
        changed the asset since, it is refused with CONFLICT rather than silently
        overwriting a change its author never saw (optimistic concurrency control).
        """
        asset_id = check_text(asset_id, "asset_id")
        expected_version = check_version(expected_version)
        asset = self.assets.get(asset_id)
        if asset is None or asset.get("deleted"):
            raise InvalidRequest(f"No asset has the ID {asset_id}", code=NOT_FOUND, asset_id=asset_id)
        if asset["version"] != expected_version:
            raise InvalidRequest(
                f"{asset_id} was changed by someone else. Refresh and try again.",
                code=CONFLICT,
                asset_id=asset_id,
                expected_version=expected_version,
                current_version=asset["version"],
            )
        return asset

    def register(self, request: dict, user: dict) -> tuple:
        """Store a new asset, stamped with the server's time. The caller holds the lock."""
        asset = validate_asset(request.get("asset"))
        # IDs are compared ignoring capitals, so AST-1 and ast-1 can't both exist. A
        # deleted asset keeps its ID, so the ID can't be reused for something else.
        taken = {asset_id.lower() for asset_id in self.assets}
        if asset["asset_id"].lower() in taken:
            raise InvalidRequest(
                f"An asset with the ID {asset['asset_id']} already exists",
                code=DUPLICATE_ASSET,
                field="asset_id",
                asset_id=asset["asset_id"],
            )
        now = utc_now()
        asset.update(version=1, created_at=now, updated_at=now)
        self.assets[asset["asset_id"]] = asset
        logger.info("%s registered %s", user["username"], asset["asset_id"])
        return {"status": "OK", "stored": asset}, lambda: self.assets.pop(asset["asset_id"])

    def edit(self, request: dict, user: dict) -> tuple:
        """Change some of an asset's description, category, location and status.
        The caller holds the lock."""
        changes = request.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise InvalidRequest("changes must be an object with at least one field", field="changes")
        for field in changes:
            if field not in EDITABLE_FIELDS:
                raise InvalidRequest(
                    f"{field} cannot be edited", field=field, editable=list(EDITABLE_FIELDS)
                )
        clean = {field: check_text(value, field) for field, value in changes.items()}
        if "status" in clean:
            clean["status"] = check_status(clean["status"])

        previous = self.current_asset(request.get("asset_id"), request.get("expected_version"))
        # A new record replaces the old one, rather than the old one being edited, so
        # undoing is just putting the old record back.
        updated = dict(previous, **clean, version=previous["version"] + 1, updated_at=utc_now())
        self.assets[updated["asset_id"]] = updated
        logger.info("%s edited %s: %s", user["username"], updated["asset_id"], ", ".join(clean))
        return {"status": "OK", "stored": updated}, lambda: self.assets.__setitem__(updated["asset_id"], previous)

    def delete(self, request: dict, user: dict) -> tuple:
        """Soft-delete an asset: the record is kept, marked deleted with the server's
        time, and left out of normal results. The caller holds the lock."""
        previous = self.current_asset(request.get("asset_id"), request.get("expected_version"))
        now = utc_now()
        deleted = dict(previous, deleted=True, deleted_at=now, updated_at=now, version=previous["version"] + 1)
        self.assets[deleted["asset_id"]] = deleted
        logger.info("%s deleted %s", user["username"], deleted["asset_id"])
        reply = {"status": "OK", "deleted": deleted["asset_id"], "stored": deleted}
        return reply, lambda: self.assets.__setitem__(deleted["asset_id"], previous)

    def list_assets(self, request: dict, user: dict) -> dict:
        """Return the assets that have not been deleted and match the search, one page
        at a time if a page_size is given.

        Optional parameters:
          query      text that must appear in any of the asset's fields
          filters    {field: text}: the text must appear in that field
          page       which page to return, from 1
          page_size  how many assets per page, at most MAX_PAGE_SIZE
        Matching ignores capitals. Without page_size, every match is returned.
        The reply always says how many assets matched in total, and which page this is.
        """
        query = request.get("query")
        if query is not None:
            query = check_text(query, "query").lower()
        filters = request.get("filters") or {}
        if not isinstance(filters, dict):
            raise InvalidRequest("filters must be an object", field="filters")
        for field in filters:
            if field not in ASSET_FIELDS:
                raise InvalidRequest(f"Cannot search by {field}", field=field, searchable=list(ASSET_FIELDS))
        wanted = {field: check_text(text, field).lower() for field, text in filters.items()}

        def matches(asset: dict) -> bool:
            if asset.get("deleted"):
                return False
            if query and not any(query in asset[field].lower() for field in ASSET_FIELDS):
                return False
            return all(text in asset[field].lower() for field, text in wanted.items())

        with self.lock:
            found = [asset for asset in self.assets.values() if matches(asset)]

        total = len(found)
        page_size = request.get("page_size")
        if page_size is None:
            page_size = max(total, 1)
        elif not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise InvalidRequest(f"page_size must be from 1 to {MAX_PAGE_SIZE}", field="page_size")
        page = request.get("page", 1)
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise InvalidRequest("page must be a whole number of at least 1", field="page")

        start = (page - 1) * page_size
        return {
            "status": "OK",
            "assets": found[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, -(-total // page_size)),  # total / page_size, rounded up
        }

    # ---- Replication ----------------------------------------------------------

    def server_action(self, request: dict) -> dict:
        """Answer another server. These actions need the servers' shared key, not a
        user's token, so no client can read or overwrite a server's data this way."""
        sent = request.get("server_key")
        if not isinstance(sent, str) or not hmac.compare_digest(sent, self.server_key):
            self.audit("unauthenticated", action=request.get("action"), reason="wrong server key")
            raise InvalidRequest("Only the other server may use this action", code=UNAUTHENTICATED)
        action = request["action"]
        with self.lock:
            if action == "SYNC_STATUS":
                return {"status": "OK", "seq": self.seq, "digest": self.digest()}
            if action == "STATE":
                return {"status": "OK", **self.snapshot()}
            self.install(request)
        return {"status": "OK", "seq": self.seq}

    def digest(self) -> str:
        """A fingerprint of the assets, to spot two servers whose data differs even
        though they have made the same number of changes. The caller holds the lock."""
        text = json.dumps(sorted(self.assets.values(), key=lambda a: a["asset_id"]), sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def install(self, data: dict) -> None:
        """Replace this server's data with another server's copy, and save it. The
        caller holds the lock."""
        assets, requests, seq = data.get("assets"), data.get("requests"), data.get("seq")
        if (
            not isinstance(assets, list)
            or not all(isinstance(a, dict) and isinstance(a.get("asset_id"), str) for a in assets)
            or not isinstance(requests, dict)
            or not isinstance(seq, int)
        ):
            raise InvalidRequest("The copy sent is not valid asset data", field="assets")
        previous = (self.assets, self.requests, self.seq)

        def undo() -> None:
            self.assets, self.requests, self.seq = previous

        self.assets = {asset["asset_id"]: asset for asset in assets}
        self.requests, self.seq = requests, seq
        self.commit(undo)

    def ask_backup(self, action: str, **fields) -> dict:
        reply = common.request_from(
            self.config["servers"]["backup"], {"action": action, "server_key": self.server_key, **fields}, self.config
        )
        if reply.get("status") != "OK":
            raise ValueError(f"The backup refused {action}: {reply.get('message')}")
        return reply

    def sync_with_backup(self) -> None:
        """Make the primary and backup hold the same data. Runs on the primary.

        Each server counts its changes in `seq`. If the backup has counted more, it
        must have taken changes while the primary was down, so the primary takes its
        data. Otherwise, if the backup is behind or its data differs, the primary sends
        its own copy. Raises OSError or ValueError if the backup can't be reached.
        """
        status = self.ask_backup("SYNC_STATUS")
        with self.lock:
            mine = (self.seq, self.digest())
        if status["seq"] > mine[0]:
            state = self.ask_backup("STATE")
            with self.lock:
                self.install(state)
            logger.warning("Took %s change(s) the backup made while this server was away", state["seq"] - mine[0])
            self.audit("took_backup_data", seq=state["seq"], previous_seq=mine[0])
        elif (status["seq"], status["digest"]) != mine:
            with self.lock:
                copy = self.snapshot()
            self.ask_backup("REPLICATE", **copy)
            logger.info("Copied the data to the backup, at change %s", copy["seq"])

    def keep_backup_in_sync(self, stop: threading.Event) -> None:
        """Copy every change to the backup as soon as it happens, and check every
        sync_interval_seconds in case the backup has restarted or fallen behind.
        Runs on its own thread on the primary."""
        while not stop.is_set():
            self.changed.wait(self.sync_interval)
            self.changed.clear()
            if stop.is_set():
                return
            try:
                self.sync_with_backup()
                if self.backup_reachable is not True:
                    logger.info("The backup is up to date")
                self.backup_reachable = True
            except (OSError, ValueError) as exc:
                if self.backup_reachable is not False:
                    logger.warning("Cannot reach the backup, carrying on alone: %s", exc)
                    self.audit("backup_unreachable", reason=str(exc))
                self.backup_reachable = False

    # ---- Networking -----------------------------------------------------------

    def read_and_process(self, conn: socket.socket) -> dict:
        """Read one request and work out the reply, turning bad input into an error
        reply instead of an exception."""
        try:
            request = receive_message(conn, self.max_bytes)
        except MessageTooLargeError as exc:
            self.audit("rejected_message", reason=str(exc))
            return error_reply(TOO_LARGE, str(exc), max_bytes=self.max_bytes)
        except ValueError as exc:
            self.audit("rejected_message", reason=str(exc))
            return error_reply(BAD_REQUEST, str(exc))
        try:
            return self.process(request)
        except Exception as exc:
            logger.exception("Could not complete a %s request", request.get("action"))
            self.audit(
                "error",
                action=request.get("action"),
                request_id=request.get("request_id"),
                detail=f"{type(exc).__name__}: {exc}",
            )
            return error_reply(INTERNAL, "The server could not complete the request")

    def handle_connection(self, conn: socket.socket, addr: Optional[tuple] = None) -> None:
        """Serve one connection: one request, one reply. Runs on the client's own thread.

        Nothing one client does may affect the server or other clients, so every
        exception stops here. The timeout means an idle or stalled client is
        disconnected instead of holding its thread forever.
        """
        try:
            conn.settimeout(self.timeout)
            # The TLS handshake happens here, on the client's own thread, so a slow or
            # failed handshake never holds up the accept loop. A client that doesn't
            # speak TLS, or rejects the certificate, fails here and is disconnected.
            try:
                tls_conn = self.tls.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                conn.close()
                self.audit("tls_failed", client=f"{addr[0]}:{addr[1]}" if addr else None, reason=str(exc))
                return
            with tls_conn:
                reply = self.read_and_process(tls_conn)
                send_message(tls_conn, reply, self.max_bytes)
        except OSError as exc:
            # The client went away or went quiet. There is nobody to reply to.
            logger.warning("Lost connection to %s: %s", addr, exc)
        except Exception:
            logger.exception("Unexpected error serving %s", addr)

    def open_socket(self) -> socket.socket:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(self.listen_address)
        server_sock.listen()
        return server_sock

    def serve(self, stop: Optional[threading.Event] = None) -> None:
        """Accept connections until `stop` is set (or forever)."""
        stop = stop or threading.Event()
        if self.role == "primary" and "backup" in self.config["servers"]:
            # Before taking any client changes, pick up whatever the backup did while
            # this server was away, so those changes aren't overwritten.
            try:
                self.sync_with_backup()
                self.backup_reachable = True
            except (OSError, ValueError) as exc:
                logger.warning("Cannot reach the backup at start-up: %s", exc)
                self.backup_reachable = False
            sync_thread = threading.Thread(target=self.keep_backup_in_sync, args=(stop,), daemon=True)
            sync_thread.start()
        else:
            sync_thread = None
        discovery_thread = None
        if self.config.get("discoverable", True):
            discovery_thread = threading.Thread(target=self.answer_discovery, args=(stop,), daemon=True)
            discovery_thread.start()
        try:
            self.accept_connections(stop)
        finally:
            stop.set()
            if sync_thread is not None:
                self.changed.set()  # wake it so it sees `stop` and finishes
                sync_thread.join()
            if discovery_thread is not None:
                discovery_thread.join()

    def answer_discovery(self, stop: threading.Event) -> None:
        """Reply to clients looking for servers on the local network.

        A client broadcasts DISCOVERY_REQUEST over UDP to the usual server ports. This
        replies with the service name, this server's role and its TCP port; the client
        takes the address from where the reply came from. The reply holds nothing
        secret, and the client still checks the server's certificate before trusting it.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(self.listen_address)
            except OSError as exc:
                logger.warning("Discovery is off: %s", exc)
                return
            sock.settimeout(0.2)  # wake up regularly to check `stop`
            reply = json.dumps({
                "service": SERVICE_NAME,
                "role": self.role,
                "port": self.address[1],
                "protocol_version": PROTOCOL_VERSION,
            }).encode("utf-8")
            while not stop.is_set():
                try:
                    data, sender = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                if data.strip() == DISCOVERY_REQUEST:
                    try:
                        sock.sendto(reply, sender)
                    except OSError:
                        pass

    def accept_connections(self, stop: threading.Event) -> None:
        with self.open_socket() as server_sock:
            server_sock.settimeout(0.2)  # wake up regularly to check `stop`
            logger.info("Asset Management %s server listening on %s:%s", self.role, *self.listen_address)
            self.audit("server_started", address=f"{self.address[0]}:{self.address[1]}")
            while not stop.is_set():
                try:
                    conn, addr = server_sock.accept()
                except socket.timeout:
                    continue
                # Each client gets its own thread, so a slow client doesn't hold up the others.
                threading.Thread(target=self.handle_connection, args=(conn, addr), daemon=True).start()


def wait_before_closing() -> None:
    """Keep the window open after a failure until Enter is pressed.

    Started by double-clicking, Windows closes the console the moment the program
    ends, so the reason it couldn't start would vanish before anyone could read it.
    """
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass  # no keyboard, e.g. started by another program


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    role = sys.argv[1] if len(sys.argv) > 1 else "primary"
    if role not in ("primary", "backup"):
        print("Usage: python server.py [primary|backup]")
        wait_before_closing()
        return
    try:
        server = AssetServer(common.load_config(), role)
    except FileNotFoundError as exc:
        logger.error("Could not start the server: %s", exc)
        print(
            "\nThe one-time setup hasn't been done yet. In this folder, run:\n"
            "    pip install -r requirements.txt\n"
            "    python tools/certificate.py\n"
            "    python tools/users.py add admin admin\n"
            "then start the server again."
        )
        wait_before_closing()
        return
    except (OSError, ValueError) as exc:
        logger.error("Could not start the server: %s", exc)
        wait_before_closing()
        return
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Server stopped.")
    except OSError as exc:
        # Most often the port is already in use, e.g. this server is already running.
        logger.error("The %s server stopped: %s", role, exc)
        wait_before_closing()


if __name__ == "__main__":
    main()

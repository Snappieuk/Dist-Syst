"""
The web interface of the Asset Management System, for browsing, adding and deleting assets.

    python web.py                  (runs the web interface)
    python web.py --set-password   (creates an account, or resets a lost password)

This is a separate program that sits between a browser and the servers. It speaks HTTP
to the browser, and the system's own TCP protocol to the servers, exactly as the other
clients do. It therefore needs no knowledge of which server is in charge, and keeps
working through a failover.

Everything except the login page needs an account, and anyone logged in can create more.
It listens only on this machine by default, and the connection is not encrypted, so
passwords should not cross a network you don't trust.
"""

import getpass    # asks for a new password without showing it on screen
import hashlib    # hashes passwords, so that the passwords themselves are never stored
import hmac       # compares password hashes safely
import ipaddress  # checks that a forwarded client address is a real address
import json       # the data sent to the browser is JSON
import logging    # records what happens, including errors, without stopping
import os         # os.path finds the page's files
import re         # checks that a username is acceptable
import secrets    # generates login tokens, password salts and a first password
import ssl        # converts the certificate so it can be fingerprinted for the GUI download
import subprocess # runs the C# compiler that builds the desktop app
import sys        # sys.argv holds the --set-password option
import threading  # the locks protect the data shared between request threads
import time       # sessions and lockouts expire
from typing import Optional  # type hints only

import common     # where the web interface listens
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from common import (
    BAD_REQUEST,
    CONFLICT,
    DUPLICATE_ASSET,
    NOT_FOUND,
    audit,
    configure_logging,
    generate_asset_tags,
    send_request,
)

# Where the web interface listens. By default 0.0.0.0:8080, which means any device on the
# network can open it. It can be set in config.json or with ASSET_WEB_ADDRESS.
WEB_ADDRESS = common.WEB_ADDRESS

# How long to wait on a browser before giving up on it, so that an idle connection
# cannot hold a thread forever.
BROWSER_TIMEOUT_SECONDS = 10

# The asset fields a browser may see. Anything else a server stores, such as the
# request_id, is internal to the system.
PUBLIC_FIELDS = ("asset_id", "description", "category", "location", "status", "date_registered",
                 "created_at", "updated_at")

# The folder holding the page's files, found relative to this script so that it works
# whichever folder the program is started from.
STATIC_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Every file a browser may request, by address. Using a fixed list means no address can
# ever reach another file on this machine, however it is written.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Where the single web password was stored before accounts were added. If this exists, it
# is converted to an account once, then removed.
PASSWORD_FILE = os.path.join(common.DATA_FOLDER, "web_password.json")

# How many rounds of hashing a password gets. This is deliberately slow, so that someone
# who obtained the file could only try a few guesses each second.
HASH_ITERATIONS = 600_000

MINIMUM_PASSWORD_LENGTH = 8

# Where each account's username and password hash are stored.
USERS_FILE = os.path.join(common.DATA_FOLDER, "web_users.json")

# Usernames are short and plain, so they are easy to type and safe to display.
USERNAME_PATTERN = re.compile(r"[a-z0-9._-]{3,32}")

USERNAME_TAKEN = "That username is already taken"

# The username offered during setup, and the account an older password is converted to.
FIRST_USERNAME = "admin"

# What each account may do. Everyone logged in can view, add and edit assets. Only an admin
# can also delete assets and create accounts. Accounts saved before roles were added have
# no role and count as admins, so nobody loses what they could already do.
ADMIN = "admin"
USER = "user"
ROLES = (ADMIN, USER)
ADMIN_ONLY = "Only an admin can do that"

# How long a login lasts.
SESSION_SECONDS = 8 * 60 * 60

# Failed logins in a row, for one username, before logins for that username are refused for
# a while, to stop someone trying passwords quickly. It is counted per username, so someone
# on the internet guessing one name can't lock everybody else out.
FAILED_LOGIN_LIMIT = 5
LOCKOUT_SECONDS = 60

# Set these when this program sits behind an HTTPS reverse proxy such as Nginx Proxy
# Manager (config.json "behind_https_proxy" / "trusted_proxy", or ASSET_BEHIND_HTTPS_PROXY /
# ASSET_TRUSTED_PROXY). The session cookie is then marked Secure, and the browser's real
# address is read from X-Forwarded-For, but only when the request came from the proxy.
BEHIND_HTTPS_PROXY = common.BEHIND_HTTPS_PROXY
TRUSTED_PROXY = common.TRUSTED_PROXY

# Usernames that have been tried and failed, so that guessing different made-up names
# can't fill the memory: when the table is full, entries no longer locked are dropped.
MAX_TRACKED_USERNAMES = 1000

# The largest request body accepted. Everything sent here is small.
MAX_BODY_BYTES = 64 * 1024

# One asset's address is this followed by its ID.
ASSET_ADDRESS_PREFIX = "/api/assets/"

# The server's errors that a user can act on, and the HTTP status of each.
USER_ERROR_STATUSES = {
    BAD_REQUEST: 400,
    NOT_FOUND: 404,
    DUPLICATE_ASSET: 409,
    CONFLICT: 409,
}

logger = logging.getLogger("asset_web")

# How much of a typed username a failed login records, so pasting something huge can't
# flood the log. Usernames are at most 32 characters, so a real one is never cut short.
MAX_LOGGED_USERNAME = 32

# Who is logged in: a random token for each browser, with its username and when its login
# expires. These are only held in memory, so restarting this program logs everyone out.
sessions = {}
failed_logins = {}  # failed attempts in a row, by (normalised) username
locked_until = {}   # when each locked username may try again, by username
login_lock = threading.Lock()  # protects the three values above

users = {}  # each account's password hash, by username
users_lock = threading.Lock()  # protects `users` and its file


def hash_password(password: str, salt: Optional[str] = None, iterations: Optional[int] = None) -> dict:
    """Hash a password, returning everything needed to check a guess against it later.

    A random salt means that two identical passwords have different hashes, so ready-made
    tables of common password hashes are no use.
    """
    salt = salt or secrets.token_hex(16)
    iterations = iterations or HASH_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", "surrogatepass"), bytes.fromhex(salt), iterations)
    return {"salt": salt, "iterations": iterations, "hash": digest.hex()}


def password_matches(record: dict, guess: str) -> bool:
    """Check a guess against a stored hash. compare_digest() takes the same time wherever
    the values differ, so timing replies reveals nothing."""
    attempt = hash_password(guess, record["salt"], record["iterations"])
    return hmac.compare_digest(attempt["hash"], record["hash"])


def load_accounts() -> None:
    """Load the accounts when the program starts.

    A password from before accounts were added becomes the "admin" account, so it keeps
    working. If there are no accounts at all, for example when started with nobody at a
    keyboard for setup to ask, one is created with a random password, shown once because
    it can't be recovered from the hash afterwards.
    """
    accounts = read_users_file()

    if not accounts and os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, encoding="utf-8") as file:
            accounts = {FIRST_USERNAME: dict(json.load(file), role=ADMIN)}
        write_users_file(accounts)
        os.remove(PASSWORD_FILE)
        print(f'\n*** The web password now belongs to an account called "{FIRST_USERNAME}".\n', flush=True)

    users.clear()
    users.update(accounts)

    if not accounts:
        password = secrets.token_urlsafe(12)
        add_account(FIRST_USERNAME, password, role=ADMIN)
        print(f'\n*** A web account was created. Username: {FIRST_USERNAME}  Password: {password}')
        print("*** It will not be shown again. To change it, run: python web.py --set-password\n", flush=True)


def create_account_from_prompt() -> bool:
    """Ask for a username and password, then create that account or change its password,
    returning whether that worked.

    A new account made here is an admin. Running this needs the machine and its files,
    which is already full control, so there is nothing to protect by asking. An existing
    account keeps its role.
    """
    username = input(f"Username [{FIRST_USERNAME}]: ").strip() or FIRST_USERNAME
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Type it again: "):
        print("Nothing changed: the two did not match.")
        return False

    problem = add_account(username, password, replace=True, role=ADMIN, keep_role=True)
    if problem:
        print(f"Nothing changed: {problem}.")
        return False

    print(f"Account {normalise_username(username)} saved. Restart web.py if it is running.")
    return True


def normalise_username(username: str) -> str:
    """Usernames ignore capitals, so "Alex" and "alex" can't be two different accounts."""
    return username.strip().lower()


def read_users_file() -> dict:
    """Return the stored accounts, or none if the file doesn't exist yet."""
    try:
        with open(USERS_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


def write_users_file(accounts: dict) -> None:
    """Store the accounts. The swap means a crash can't leave a damaged file, and other
    users of the machine can't read it on Linux and macOS."""
    common.make_folder_for(USERS_FILE)
    temp_path = USERS_FILE + ".tmp"
    with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        json.dump(accounts, file, indent=2)
    os.replace(temp_path, USERS_FILE)


def role_of(record: dict) -> str:
    """An account's role. One saved before roles were added counts as an admin."""
    return record.get("role", ADMIN)


def add_account(username: str, password: str, replace: bool = False, role: Optional[str] = None,
                keep_role: bool = False) -> Optional[str]:
    """Store an account, returning a message saying why not if it can't be done, or None.

    The very first account is always an admin, so someone can manage the rest. Otherwise
    a new account gets `role`, or is a user if none is given. With `keep_role`, an existing
    account being replaced keeps the role it had.

    The slow hash runs outside the lock, so it holds up nobody else. The check for an
    existing name happens inside the lock, so that two people can't both claim one. The
    file is read again first, in case the account was changed from the command line.
    """
    username = normalise_username(username)
    if not USERNAME_PATTERN.fullmatch(username):
        return "A username must be 3 to 32 characters: letters, numbers, dots, dashes or underscores"
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        return f"A password must be at least {MINIMUM_PASSWORD_LENGTH} characters"
    if role is not None and role not in ROLES:
        return f"A role must be one of: {', '.join(ROLES)}"

    record = hash_password(password)
    with users_lock:
        accounts = read_users_file()
        if username in accounts and not replace:
            return USERNAME_TAKEN
        if not accounts:
            record["role"] = ADMIN
        elif keep_role and username in accounts:
            record["role"] = role_of(accounts[username])
        else:
            record["role"] = role or USER
        accounts[username] = record
        write_users_file(accounts)
        users.clear()
        users.update(accounts)
    return None


def create_session(username: str) -> str:
    """Log a browser in, returning its token. Expired sessions are cleared out here too,
    so the list cannot grow forever."""
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with login_lock:
        for expired in [key for key, session in sessions.items() if session["expires"] <= now]:
            del sessions[expired]
        sessions[token] = {"username": username, "expires": now + SESSION_SECONDS}
    return token


def session_username(token: Optional[str]) -> Optional[str]:
    """Return who a token belongs to, or None if it isn't a current login."""
    with login_lock:
        session = sessions.get(token)
        if session is None or session["expires"] <= time.monotonic():
            return None
        return session["username"]


def is_admin(username: str) -> bool:
    """Whether an account is an admin, checked on every request, so a change to its role
    takes effect at once."""
    with users_lock:
        record = users.get(username)
    return record is not None and role_of(record) == ADMIN


def end_session(token: object) -> None:
    with login_lock:
        sessions.pop(token, None)


def try_login(username: str, guess: str) -> tuple:
    """Check a username and password, returning an HTTP status, a JSON body and the new
    session token, if there is one. The slow hash runs outside the lock, so it holds up
    nobody else.

    Failed attempts are counted for each username separately, so guessing one account's
    password locks only that account's logins, never everyone's.

    An unknown username gets the same message and does the same slow work as a wrong
    password, so neither the reply nor its timing reveals which usernames exist.
    """
    username = normalise_username(username)

    with login_lock:
        if time.monotonic() < locked_until.get(username, 0):
            return 429, {"error": "Too many failed logins. Please wait a minute and try again."}, None

    with users_lock:
        record = users.get(username)

    if record is None:
        hash_password(guess)
        matched = False
    else:
        matched = password_matches(record, guess)

    if matched:
        with login_lock:
            failed_logins.pop(username, None)
        return 200, {"username": username}, create_session(username)

    with login_lock:
        now = time.monotonic()
        if len(failed_logins) >= MAX_TRACKED_USERNAMES and username not in failed_logins:
            for name in [name for name in failed_logins if locked_until.get(name, 0) <= now]:
                del failed_logins[name]
            for name in [name for name in locked_until if locked_until[name] <= now]:
                del locked_until[name]
        failed_logins[username] = failed_logins.get(username, 0) + 1
        if failed_logins[username] >= FAILED_LOGIN_LIMIT:
            failed_logins[username] = 0
            locked_until[username] = now + LOCKOUT_SECONDS
            logger.warning("Too many failed logins for %r. Refusing its logins for %s seconds.",
                           username[:MAX_LOGGED_USERNAME], LOCKOUT_SECONDS)
    return 401, {"error": "That username or password is not correct"}, None


def public_asset(asset: dict) -> dict:
    """Return only the parts of a stored asset that a browser may see."""
    public = {field: asset.get(field, "") for field in PUBLIC_FIELDS}

    # The browser sends this back when deleting, so a change it hasn't seen is noticed.
    public["version"] = asset.get("version", 0)

    # Assets saved before tagging was added have no tags, so those are worked out here.
    public["tags"] = asset.get("tags") or generate_asset_tags(asset)

    # Whether it awaits review, whether it is made up, every loan it has had (for the
    # check-out form and the calendar) and what has happened to it.
    public["flagged"] = bool(asset.get("flagged"))
    public["sample"] = bool(asset.get("sample"))
    public["loans"] = asset.get("loans") or []
    public["history"] = asset.get("history") or []
    return public


def assets_reply() -> tuple:
    """Fetch the assets from the servers, returning an HTTP status and a JSON body.

    Problems are logged in full for whoever runs this, but the browser only gets a
    general message, because internal details can help an attacker.
    """
    try:
        reply = send_request({"action": "LIST"})
    except (OSError, ValueError) as exc:
        logger.warning("Could not get the assets: %s", exc)
        return 503, {"error": "No asset server can be reached at the moment"}

    if reply.get("status") != "OK":
        logger.warning("A server could not list the assets: %s", reply.get("detail"))
        return 502, {"error": "The asset server could not provide the list"}

    return 200, {
        "assets": [public_asset(asset) for asset in reply["assets"]],

        # Showing which server answered makes a failover visible in the browser.
        "served_by": reply.get("served_by", "unknown"),
    }


def session_cookie(token: str, max_age: int) -> str:
    """Build the header that gives a browser its login.

    HttpOnly stops scripts reading it, and SameSite=Strict stops other websites sending
    it. It is only marked Secure behind an HTTPS proxy, because otherwise this program
    serves plain HTTP, and a browser would not send a Secure cookie back over it.
    """
    secure = "; Secure" if BEHIND_HTTPS_PROXY else ""
    return f"session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}"


def change_reply(request: dict, success_status: int) -> tuple:
    """Send a change to the servers, returning an HTTP status, the server's reply and a
    JSON body holding any error.

    All the rules, such as rejecting duplicates, stay on the servers. Errors the user can
    act on pass through, because those details are written for people. Others are only
    logged, because internal details can help an attacker.
    """
    try:
        reply = send_request(request)
    except (OSError, ValueError) as exc:
        logger.warning("Could not send a change: %s", exc)
        return 503, None, {"error": "No asset server can be reached at the moment"}

    if reply.get("status") == "OK":
        return success_status, reply, {"served_by": reply.get("served_by", "unknown")}

    if reply.get("code") in USER_ERROR_STATUSES:
        return USER_ERROR_STATUSES[reply["code"]], None, {"error": reply.get("detail", "The change was refused")}

    logger.warning("A server could not make a change: %s", reply.get("detail"))
    return 503, None, {"error": "The asset server could not make the change. Please try again."}


def add_asset_reply(body: dict, username: str = "someone") -> tuple:
    """Register an asset for `username`, returning an HTTP status and a JSON body. Only the
    expected values are passed on, so a browser cannot add anything else to the request."""
    status, reply, result = change_reply(
        {"action": "REGISTER", "request_id": body.get("request_id"), "asset": body.get("asset"), "user": username}, 201
    )
    if reply is not None:
        stored = reply["stored"]
        result["asset"] = public_asset(stored)
        # %r shows exactly what was typed, so a line break in it can't fake a log line.
        logger.info("%s added %s %r, served by the %s",
                    username, stored.get("asset_id"), stored.get("description"), reply.get("served_by"))
    else:
        logger.info("%s could not add an asset: %s", username, result.get("error"))
    return status, result


def delete_asset_reply(asset_id: str, query: str, username: str = "someone") -> tuple:
    """Delete an asset for `username`, returning an HTTP status and a JSON body. The
    address's ?version= says which version the user was looking at, and is required, so
    that an asset someone else has just changed can't be deleted by accident."""
    try:
        version = int(parse_qs(query)["version"][0])
    except (KeyError, ValueError):
        return 400, {"error": "The version of the asset is needed"}

    status, reply, result = change_reply(
        {"action": "DELETE", "asset_id": asset_id, "expected_version": version, "user": username}, 200
    )
    if reply is not None:
        logger.info("%s deleted %r, served by the %s", username, asset_id, reply.get("served_by"))
    else:
        logger.info("%s could not delete %r: %s", username, asset_id, result.get("error"))
    return status, result


def edit_asset_reply(asset_id: str, body: dict, username: str = "someone") -> tuple:
    """Edit an asset for `username`, returning an HTTP status and a JSON body. The body
    holds the changed fields and the version the user was looking at, which is required,
    so that a change someone else has just made can't be overwritten by accident."""
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return 400, {"error": "The version of the asset is needed"}

    status, reply, result = change_reply(
        {"action": "EDIT", "asset_id": asset_id, "changes": body.get("changes"), "expected_version": version,
         "user": username}, 200
    )
    if reply is not None:
        result["asset"] = public_asset(reply["stored"])
        logger.info("%s edited %r (%s), served by the %s", username, asset_id,
                    ", ".join(sorted(body["changes"])), reply.get("served_by"))
    else:
        logger.info("%s could not edit %r: %s", username, asset_id, result.get("error"))
    return status, result


# The things that can be done to one asset by POSTing to /api/assets/<id>/<name>: the
# request each sends to the servers, the fields of the browser's body it passes on, and
# whether only an admin may do it.
ASSET_ACTIONS = {
    "approve": ("APPROVE", (), True),
    "check-out": ("CHECK_OUT", ("borrower", "start", "due"), False),
    "check-in": ("CHECK_IN", ("returned",), False),
}


def asset_action_reply(asset_id: str, name: str, body: dict, username: str = "someone") -> tuple:
    """Approve, check out or check in an asset for `username`, returning an HTTP status and
    a JSON body. Only the expected fields are passed on. The version the user was looking
    at is required, so a change someone else has just made isn't missed."""
    action, fields, _ = ASSET_ACTIONS[name]
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return 400, {"error": "The version of the asset is needed"}

    request = {"action": action, "asset_id": asset_id, "expected_version": version, "user": username}
    request.update({field: body[field] for field in fields if body.get(field) not in (None, "")})
    status, reply, result = change_reply(request, 200)
    if reply is not None:
        result["asset"] = public_asset(reply["stored"])
        logger.info("%s did %s on %r, served by the %s", username, action, asset_id, reply.get("served_by"))
    else:
        logger.info("%s could not do %s on %r: %s", username, action, asset_id, result.get("error"))
    return status, result


def asset_action_address(path: str) -> Optional[tuple]:
    """Split /api/assets/<id>/<name> into the asset ID and the action's name, or return
    None if the path isn't one of these."""
    if not path.startswith(ASSET_ADDRESS_PREFIX):
        return None
    asset_id, _, name = path[len(ASSET_ADDRESS_PREFIX):].rpartition("/")
    return (unquote(asset_id), name) if asset_id and name in ASSET_ACTIONS else None


def create_account_reply(body: dict, created_by: str) -> tuple:
    """Create an account for a logged-in user, returning an HTTP status and a JSON body.

    Only an admin who is logged in can get here. Open sign-up would let anyone who can
    reach the page give themselves an account, which would make the login pointless. The
    new account is a user unless the admin chooses admin.
    """
    username, password, role = body.get("username"), body.get("password"), body.get("role", USER)
    if not isinstance(username, str) or not isinstance(password, str):
        return 400, {"error": "A username and password are needed"}
    if role not in ROLES:
        return 400, {"error": f"A role must be one of: {', '.join(ROLES)}"}

    problem = add_account(username, password, role=role)
    if problem == USERNAME_TAKEN:
        return 409, {"error": problem}
    if problem:
        return 400, {"error": problem}

    username = normalise_username(username)
    logger.info("%s created the account %s, as %s %s", created_by, username, "an" if role == ADMIN else "a", role)
    audit("CREATE_ACCOUNT", user=created_by, account=username, role=role)
    return 201, {"username": username, "role": role}


USERS_ADDRESS_PREFIX = "/api/users/"  # one account's address is this followed by its username
LAST_ADMIN = "There must always be at least one admin"


def list_accounts() -> list:
    """Every account's username and role, in order. Password hashes are never sent."""
    with users_lock:
        return [{"username": name, "role": role_of(record)} for name, record in sorted(users.items())]


def change_accounts(username: str, change) -> object:
    """Apply `change` to the stored accounts, returning a problem message or None. The file
    is read again first, in case it was changed from the command line, and an admin must
    be left afterwards, so nobody can lock everyone out."""
    with users_lock:
        accounts = read_users_file()
        if username not in accounts:
            return "There is no account with that username"
        change(accounts)
        if not any(role_of(record) == ADMIN for record in accounts.values()):
            return LAST_ADMIN
        write_users_file(accounts)
        users.clear()
        users.update(accounts)
    return None


def set_role_reply(username: str, body: dict, changed_by: str) -> tuple:
    """Change an account's role, returning an HTTP status and a JSON body."""
    role = body.get("role")
    if role not in ROLES:
        return 400, {"error": f"A role must be one of: {', '.join(ROLES)}"}

    problem = change_accounts(username, lambda accounts: accounts[username].update(role=role))
    if problem:
        return (409 if problem == LAST_ADMIN else 404), {"error": problem}
    logger.info("%s made %s %s %s", changed_by, username, "an" if role == ADMIN else "a", role)
    audit("CHANGE_ROLE", user=changed_by, account=username, role=role)
    return 200, {"username": username, "role": role}


def delete_account_reply(username: str, deleted_by: str) -> tuple:
    """Delete an account and log it out everywhere, returning an HTTP status and a JSON body.
    Nobody can delete their own account, so an admin can't remove themselves by mistake."""
    if username == deleted_by:
        return 409, {"error": "You can't delete your own account"}

    problem = change_accounts(username, lambda accounts: accounts.pop(username))
    if problem:
        return (409 if problem == LAST_ADMIN else 404), {"error": problem}
    with login_lock:
        for token in [token for token, session in sessions.items() if session["username"] == username]:
            del sessions[token]
    logger.info("%s deleted the account %s", deleted_by, username)
    audit("DELETE_ACCOUNT", user=deleted_by, account=username)
    return 200, {"deleted": username}


GUI_DOWNLOAD_ADDRESS = "/download/gui.py"
GUI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
SAFE_HOST = re.compile(r"[A-Za-z0-9.:-]{1,253}")  # what a host name or IP address may contain


def certificate_fingerprint() -> str:
    """The SHA-256 fingerprint of the servers' certificate (data/tls.pem), written the same
    way as gui.py writes it: AB:CD:... Only the certificate is read, never the private key."""
    with open(common.ensure_tls_file(), "r", encoding="ascii") as file:
        found = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", file.read(), re.S)
    if found is None:
        raise ValueError("There is no certificate in the TLS file")
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(found.group(0))).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def site_servers(web_host: str) -> list:
    """The servers' (host, port) pairs as another computer would reach them. Every server is
    on this machine, so each one is the host the browser used to reach this page, with the
    server's own port, because "127.0.0.1" means nothing on another computer."""
    if not SAFE_HOST.fullmatch(web_host or ""):
        raise ValueError("The web page was reached by an address that can't be used")
    return [(web_host, port) for _host, port in common.SERVERS]


def site_url(host_header: str) -> Optional[str]:
    """The https:// address of this site, for a download, when it sits behind an HTTPS
    reverse proxy (behind_https_proxy is on); otherwise None. It is built from the host
    (and port, if any) the browser used, which is the name the person reaches the site by."""
    if not BEHIND_HTTPS_PROXY:
        return None
    parts = urlsplit("//" + (host_header or ""))
    try:
        port = parts.port
    except ValueError:
        port = None
    if not SAFE_HOST.fullmatch(parts.hostname or "") or (port is None and ":" in (host_header or "").rsplit("]", 1)[-1]):
        raise ValueError("The web page was reached by an address that can't be used")
    shown = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    return f"https://{shown}" + (f":{port}" if port else "")


APP_DOWNLOAD_ADDRESS = "/download/gui.exe"
APP_SOURCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients", "app")
APP_FILE = os.path.join(common.DATA_FOLDER, "AssetApp.exe")  # the built program, reused until the source changes
APP_MARKER = b"--ASSET-" + b"SITE--"  # what the app looks for at the end of its own file
build_lock = threading.Lock()


def find_csc() -> Optional[str]:
    """The C# compiler that comes with Windows, or None on other systems."""
    for folder in ("Framework64", "Framework"):
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Microsoft.NET", folder, "v4.0.30319", "csc.exe")
        if os.path.exists(path):
            return path
    return None


def build_app() -> str:
    """Compile clients/app into data/AssetApp.exe, unless that is already up to date, and
    return its path. Raises OSError if it can't be built."""
    sources = sorted(os.path.join(APP_SOURCE_FOLDER, name) for name in os.listdir(APP_SOURCE_FOLDER)
                     if name.endswith(".cs"))
    newest = max(os.path.getmtime(path) for path in sources)
    with build_lock:  # two downloads at once must not build at once
        if os.path.exists(APP_FILE) and os.path.getmtime(APP_FILE) >= newest:
            return APP_FILE
        compiler = find_csc()
        if compiler is None:
            raise OSError("The C# compiler is only available on Windows")
        os.makedirs(common.DATA_FOLDER, exist_ok=True)
        done = subprocess.run(
            [compiler, "/nologo", "/target:winexe", f"/out:{APP_FILE}", "/r:System.Windows.Forms.dll",
             "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll"] + sources,
            capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise OSError("The app did not compile: " + done.stdout.strip())
        return APP_FILE


def app_with_site_settings(web_host: str, url: Optional[str] = None) -> bytes:
    """The built app with the servers and certificate fingerprint added to the end of the
    file. A .exe ignores extra bytes at its end, and the app reads them when it starts.
    As with gui.py, neither value is secret. Behind an HTTPS proxy (url is given), the web
    address is added instead, and no server addresses or fingerprint."""
    if url:
        settings = json.dumps({"url": url}).encode("utf-8")
    else:
        servers = [f"{host}:{port}" for host, port in site_servers(web_host)]
        settings = json.dumps({"servers": servers, "fingerprint": certificate_fingerprint()}).encode("utf-8")
    with open(build_app(), "rb") as file:
        return file.read() + b"\n" + APP_MARKER + settings


def gui_with_site_settings(web_host: str, url: Optional[str] = None) -> str:
    """The text of gui.py with SITE_SERVERS and SITE_FINGERPRINT filled in, or, behind an
    HTTPS proxy (url is given), with SITE_URL filled in instead.

    None of these values is secret; the secret key and passwords are never put in the file.
    """
    if url:
        changes = (("SITE_URL = None\n", f"SITE_URL = {url!r}\n"),)
    else:
        changes = (("SITE_SERVERS = []\n", f"SITE_SERVERS = {site_servers(web_host)!r}\n"),
                   ("SITE_FINGERPRINT = None\n", f"SITE_FINGERPRINT = {certificate_fingerprint()!r}\n"))

    with open(GUI_FILE, "r", encoding="utf-8") as file:
        source = file.read()
    # Each setting line must be there exactly once, or the file isn't the one expected.
    for old, new in changes:
        if source.count(old) != 1:
            raise ValueError(f"gui.py doesn't contain the line {old.strip()!r} exactly once")
        source = source.replace(old, new)
    return source


class WebHandler(BaseHTTPRequestHandler):
    """Answers one browser request. Each request runs on its own thread."""

    timeout = BROWSER_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        # Only the path matters, so anything after a "?" is ignored.
        path = urlsplit(self.path).path

        if path == "/api/assets":
            username = session_username(self.session_token())
            if username is None:
                self.send_json(401, {"error": "Please log in"})
            else:
                status, reply = assets_reply()
                reply["username"] = username  # so the page can say who is logged in
                reply["role"] = ADMIN if is_admin(username) else USER  # and what they can do
                self.send_json(status, reply)
        elif path == "/api/users":
            username = self.admin_or_refuse()
            if username is not None:
                self.send_json(200, {"accounts": list_accounts()})
        elif path == GUI_DOWNLOAD_ADDRESS:
            self.send_gui_download()
        elif path == APP_DOWNLOAD_ADDRESS:
            self.send_app_download()
        elif path in STATIC_FILES:
            self.send_static_file(*STATIC_FILES[path])
        elif path == "/favicon.ico":
            # Browsers ask for a tab icon by themselves. There isn't one, and saying
            # so stops a "not found" error appearing on every visit.
            self.send_body(204, "image/x-icon", b"")
        else:
            self.send_json(404, {"error": "There is nothing at this address"})

    def do_POST(self) -> None:
        if not self.is_from_this_site():
            self.send_json(403, {"error": "This request came from another site"})
            return

        path = urlsplit(self.path).path
        action_address = asset_action_address(path)
        if path == "/api/login":
            self.handle_login()
        elif path == "/api/logout":
            self.handle_logout()
        elif action_address is not None:
            self.handle_asset_action(*action_address)
        elif path not in ("/api/assets", "/api/users"):
            self.send_json(404, {"error": "There is nothing at this address"})
        else:
            username = session_username(self.session_token())
            if username is None:
                self.send_json(401, {"error": "Please log in"})
                return

            if path == "/api/users" and not is_admin(username):
                logger.warning("%s tried to create an account, but isn't an admin", username)
                self.send_json(403, {"error": ADMIN_ONLY})
                return

            body = self.read_json_body()
            if body is None:
                return
            if path == "/api/assets":
                self.send_json(*add_asset_reply(body, username))
            else:
                self.send_json(*create_account_reply(body, username))

    def do_DELETE(self) -> None:
        if not self.is_from_this_site():
            self.send_json(403, {"error": "This request came from another site"})
            return

        address = urlsplit(self.path)
        target = self.account_address(address.path)
        if target is not None:
            username = self.admin_or_refuse()
            if username is not None:
                self.send_json(*delete_account_reply(target, username))
        elif not address.path.startswith(ASSET_ADDRESS_PREFIX) or address.path == ASSET_ADDRESS_PREFIX:
            self.send_json(404, {"error": "There is nothing at this address"})
        else:
            username = session_username(self.session_token())
            if username is None:
                self.send_json(401, {"error": "Please log in"})
                return
            asset_id = unquote(address.path[len(ASSET_ADDRESS_PREFIX):])
            if not is_admin(username):
                logger.warning("%s tried to delete %r, but isn't an admin", username, asset_id)
                self.send_json(403, {"error": ADMIN_ONLY})
                return
            self.send_json(*delete_asset_reply(asset_id, address.query, username))

    def do_PATCH(self) -> None:
        if not self.is_from_this_site():
            self.send_json(403, {"error": "This request came from another site"})
            return

        path = urlsplit(self.path).path
        target = self.account_address(path)
        if target is not None:
            username = self.admin_or_refuse()
            if username is not None:
                body = self.read_json_body()
                if body is not None:
                    self.send_json(*set_role_reply(target, body, username))
            return
        if not path.startswith(ASSET_ADDRESS_PREFIX) or path == ASSET_ADDRESS_PREFIX:
            self.send_json(404, {"error": "There is nothing at this address"})
            return
        username = session_username(self.session_token())
        if username is None:
            self.send_json(401, {"error": "Please log in"})
            return
        body = self.read_json_body()
        if body is not None:
            asset_id = unquote(path[len(ASSET_ADDRESS_PREFIX):])
            self.send_json(*edit_asset_reply(asset_id, body, username))

    def handle_login(self) -> None:
        body = self.read_json_body()
        if body is None:
            return

        username, guess = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(guess, str):
            self.send_json(400, {"error": "A username and password are needed"})
            return

        status, reply, token = try_login(username, guess)
        browser = self.browser_address()
        if token is None:
            # The password is never logged. The username is shown with %r, and shortened,
            # because it is whatever was typed, which may not be a real username.
            logger.warning("Failed login as %r from %s (%s)", username[:MAX_LOGGED_USERNAME], browser, reply["error"])
            audit("LOGIN_FAILED", user=username[:MAX_LOGGED_USERNAME], address=browser, reason=reply["error"])
            self.send_json(status, reply)
        else:
            logger.info("%s logged in from %s", reply["username"], browser)
            audit("LOGIN", user=reply["username"], address=browser)
            self.send_json(status, reply, session_cookie(token, SESSION_SECONDS))

    def handle_asset_action(self, asset_id: str, name: str) -> None:
        """Approve, check out or check in one asset, for a logged-in user."""
        username = session_username(self.session_token())
        if username is None:
            self.send_json(401, {"error": "Please log in"})
            return
        if ASSET_ACTIONS[name][2] and not is_admin(username):
            logger.warning("%s tried to %s %r, but isn't an admin", username, name, asset_id)
            self.send_json(403, {"error": ADMIN_ONLY})
            return
        body = self.read_json_body()
        if body is not None:
            self.send_json(*asset_action_reply(asset_id, name, body, username))

    def handle_logout(self) -> None:
        token = self.session_token()
        username = session_username(token)
        end_session(token)
        if username is not None:
            logger.info("%s logged out", username)
            audit("LOGOUT", user=username)
        self.send_json(200, {}, session_cookie("", 0))

    def browser_address(self) -> str:
        """The address to record for whoever is making this request.

        Behind a proxy every request comes from the proxy, so its X-Forwarded-For header
        says who really asked. Anyone can send that header, so it is believed only when
        the request really came from the trusted proxy, and only the last entry, which
        is the one the proxy added, is used. It must be a valid address, so it can't
        put anything else into the log.
        """
        direct = self.client_address[0]
        if not BEHIND_HTTPS_PROXY or direct != TRUSTED_PROXY:
            return direct
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return direct

    def session_token(self) -> Optional[str]:
        """Return the token in the browser's cookie, or None if it has none."""
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("session")
        except CookieError:
            return None
        return morsel.value if morsel else None

    @staticmethod
    def account_address(path: str) -> Optional[str]:
        """The username in an account's address, such as /api/users/alex, or None."""
        if path.startswith(USERS_ADDRESS_PREFIX) and path != USERS_ADDRESS_PREFIX:
            return normalise_username(unquote(path[len(USERS_ADDRESS_PREFIX):]))
        return None

    def admin_or_refuse(self) -> Optional[str]:
        """Return the logged-in admin's username, or reply 401 or 403 and return None."""
        username = session_username(self.session_token())
        if username is None:
            self.send_json(401, {"error": "Please log in"})
            return None
        if not is_admin(username):
            logger.warning("%s tried to manage accounts, but isn't an admin", username)
            self.send_json(403, {"error": ADMIN_ONLY})
            return None
        return username

    def is_logged_in(self) -> bool:
        return session_username(self.session_token()) is not None

    def is_from_this_site(self) -> bool:
        """Refuse requests that a browser was tricked into sending by another website.

        Browsers name the site that made such a request in the Origin header. The session
        cookie already should not be sent in that case, so this is a second safeguard.
        """
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

    def read_json_body(self) -> Optional[dict]:
        """Return the request's JSON object, or reply with an error and return None."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1

        if length < 0 or length > MAX_BODY_BYTES:
            self.send_json(400, {"error": "The request is missing its content or is too large"})
            return None

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, RecursionError):
            body = None

        if not isinstance(body, dict):
            self.send_json(400, {"error": "The request must be a JSON object"})
            return None
        return body

    def send_gui_download(self) -> None:
        """Send gui.py with this system's server addresses and certificate fingerprint filled
        in, so the user only has to run it and log in. Only logged-in users get it."""
        if not self.is_logged_in():
            self.send_json(401, {"error": "Please log in"})
            return
        # The host the browser used to reach this page is how this machine is known to the
        # user, which is what "127.0.0.1" in the servers list has to be turned into.
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        try:
            source = gui_with_site_settings(host, site_url(self.headers.get("Host", "")))
        except (OSError, ValueError) as exc:
            logger.error("Could not make the GUI download: %s", exc)
            self.send_json(500, {"error": "The GUI could not be prepared"})
            return
        self.send_body(200, "text/x-python; charset=utf-8", source.encode("utf-8"),
                       extra_headers={"Content-Disposition": 'attachment; filename="gui.py"'})

    def send_app_download(self) -> None:
        """Send the C# desktop app (AssetApp.exe) with this system's server addresses and
        certificate fingerprint added, built on first use. Only logged-in users get it."""
        if not self.is_logged_in():
            self.send_json(401, {"error": "Please log in"})
            return
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        try:
            program = app_with_site_settings(host, site_url(self.headers.get("Host", "")))
        except ValueError as exc:
            logger.error("Could not make the app download: %s", exc)
            self.send_json(500, {"error": "The app could not be prepared"})
            return
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Could not make the app download: %s", exc)
            self.send_json(404, {"error": "The Windows app can't be built on this server. "
                                          "Download the Python app (gui.py) instead."})
            return
        self.send_body(200, "application/vnd.microsoft.portable-executable", program,
                       extra_headers={"Content-Disposition": 'attachment; filename="AssetApp.exe"'})

    def send_static_file(self, file_name: str, content_type: str) -> None:
        with open(os.path.join(STATIC_FOLDER, file_name), "rb") as file:
            self.send_body(200, content_type, file.read())

    def send_json(self, status: int, body: dict, cookie: Optional[str] = None) -> None:
        self.send_body(status, "application/json", json.dumps(body).encode("utf-8"), cookie)

    def send_body(self, status: int, content_type: str, payload: bytes, cookie: Optional[str] = None,
                  extra_headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))

        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)

        # The data changes, so a browser must not keep an old copy.
        self.send_header("Cache-Control", "no-store")

        # The page only needs its own files. Asset details are typed in by people, so
        # this limits the damage if one ever got treated as code. It also stops the
        # browser guessing file types, and stops other sites embedding the page.
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")

        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        """Send request logs to the normal log, rather than straight to the console."""
        logger.debug("%s %s", self.address_string(), format % args)


def main() -> None:
    if "--set-password" in sys.argv:
        create_account_from_prompt()
        return

    # The log goes to the console and to logs/web.log. It records who logged in and what
    # each account added or deleted, but never a password.
    configure_logging("web")
    load_accounts()

    # ThreadingHTTPServer gives each browser request its own thread, so one slow
    # browser does not delay the others.
    with ThreadingHTTPServer(WEB_ADDRESS, WebHandler) as web_server:
        logger.info("Web interface at http://%s:%s (press Ctrl+C to stop)", *common.address_to_connect_to(WEB_ADDRESS))
        if WEB_ADDRESS[0] in ("0.0.0.0", "::"):
            logger.info("It listens on every network, so other devices can open it using this "
                        "computer's own address and port %s.", WEB_ADDRESS[1])
        try:
            web_server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Web interface stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

"""
The web interface of the Asset Management System, for browsing assets.

    python web.py

This is a separate program that sits between a browser and the servers. It speaks HTTP
to the browser, and the system's own TCP protocol to the servers, exactly as the other
clients do. It therefore needs no knowledge of which server is in charge, and keeps
working through a failover.

Viewing the data needs the web password. It listens only on this machine by default, and
the connection is not encrypted, so the password should not cross a network you don't trust.
"""

import getpass    # asks for a new password without showing it on screen
import hashlib    # hashes the password, so that the password itself is never stored
import hmac       # compares password hashes safely
import json       # the data sent to the browser is JSON
import logging    # records what happens, including errors, without stopping
import os         # os.path finds the page's files
import secrets    # generates the first password
import sys        # sys.argv holds the --set-password option
import threading  # the lock protects the login data shared between request threads
import time       # sessions and lockouts expire
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from common import BAD_REQUEST, CONFLICT, DUPLICATE_ASSET, NOT_FOUND, generate_asset_tags, send_request

# Where the web interface listens. 127.0.0.1 means only this machine can open it.
WEB_ADDRESS = ("127.0.0.1", 8080)

# How long to wait on a browser before giving up on it, so that an idle connection
# cannot hold a thread forever.
BROWSER_TIMEOUT_SECONDS = 10

# The asset fields a browser may see. Anything else a server stores, such as the
# request_id, is internal to the system.
PUBLIC_FIELDS = ("asset_id", "description", "category", "location", "status", "date_registered")

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

# Where the hash of the web password is stored.
PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_password.json")

# How many rounds of hashing a password gets. This is deliberately slow, so that someone
# who obtained the file could only try a few guesses each second.
HASH_ITERATIONS = 600_000

MINIMUM_PASSWORD_LENGTH = 8

# How long a login lasts.
SESSION_SECONDS = 8 * 60 * 60

# Wrong passwords in a row before logins are refused for a while, to stop someone trying
# passwords quickly. It applies to everyone, because there is only one password.
FAILED_LOGIN_LIMIT = 5
LOCKOUT_SECONDS = 60

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

password_record = None  # the hash of the web password, set when the program starts

# Who is logged in: a random token for each browser, and when its login expires. These are
# only held in memory, so restarting this program logs everyone out.
sessions = {}
failed_logins = 0
locked_until = 0
login_lock = threading.Lock()  # protects the three values above


def hash_password(password: str, salt: str = None, iterations: int = None) -> dict:
    """Hash a password, returning everything needed to check a guess against it later.

    A random salt means that two identical passwords have different hashes, so ready-made
    tables of common password hashes are no use.
    """
    salt = salt or secrets.token_hex(16)
    iterations = iterations or HASH_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", "surrogatepass"), bytes.fromhex(salt), iterations)
    return {"salt": salt, "iterations": iterations, "hash": digest.hex()}


def password_matches(guess: str) -> bool:
    """Check a guess against the stored hash. compare_digest() takes the same time wherever
    the values differ, so timing replies reveals nothing."""
    attempt = hash_password(guess, password_record["salt"], password_record["iterations"])
    return hmac.compare_digest(attempt["hash"], password_record["hash"])


def save_password(password: str) -> dict:
    """Store the hash of a new password, and return it. The swap means a crash cannot leave
    a damaged file, and other users of the machine cannot read it on Linux and macOS."""
    record = hash_password(password)
    temp_path = PASSWORD_FILE + ".tmp"
    with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        json.dump(record, file)
    os.replace(temp_path, PASSWORD_FILE)
    return record


def load_or_create_password() -> dict:
    """Read the stored password hash. On the first run, create a random password and show
    it once, because it cannot be recovered from the hash afterwards."""
    try:
        with open(PASSWORD_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        pass

    password = secrets.token_urlsafe(12)
    record = save_password(password)
    print(f"\n*** The web password is: {password}")
    print("*** It will not be shown again. To change it, run: python web.py --set-password\n", flush=True)
    return record


def set_password_from_prompt() -> None:
    """Ask for a new web password and store it."""
    password = getpass.getpass("New web password: ")
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        print(f"Nothing changed: it must be at least {MINIMUM_PASSWORD_LENGTH} characters.")
    elif password != getpass.getpass("Type it again: "):
        print("Nothing changed: the two did not match.")
    else:
        save_password(password)
        print("Password changed. Restart web.py if it is running.")


def create_session() -> str:
    """Log a browser in, returning its token. Expired sessions are cleared out here too,
    so the list cannot grow forever."""
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with login_lock:
        for expired in [key for key, expiry in sessions.items() if expiry <= now]:
            del sessions[expired]
        sessions[token] = now + SESSION_SECONDS
    return token


def session_is_valid(token: object) -> bool:
    with login_lock:
        return token in sessions and sessions[token] > time.monotonic()


def end_session(token: object) -> None:
    with login_lock:
        sessions.pop(token, None)


def try_login(guess: str) -> tuple:
    """Check a password, returning an HTTP status, a JSON body and the new session token,
    if there is one. The slow hash runs outside the lock, so it holds up nobody else."""
    global failed_logins, locked_until

    with login_lock:
        if time.monotonic() < locked_until:
            return 429, {"error": "Too many wrong passwords. Please wait a minute and try again."}, None

    if password_matches(guess):
        with login_lock:
            failed_logins = 0
        return 200, {"logged_in": True}, create_session()

    with login_lock:
        failed_logins += 1
        if failed_logins >= FAILED_LOGIN_LIMIT:
            failed_logins = 0
            locked_until = time.monotonic() + LOCKOUT_SECONDS
            logger.warning("Too many wrong passwords. Refusing logins for %s seconds.", LOCKOUT_SECONDS)
    return 401, {"error": "That password is not correct"}, None


def public_asset(asset: dict) -> dict:
    """Return only the parts of a stored asset that a browser may see."""
    public = {field: asset.get(field, "") for field in PUBLIC_FIELDS}

    # The browser sends this back when deleting, so a change it hasn't seen is noticed.
    public["version"] = asset.get("version", 0)

    # Assets saved before tagging was added have no tags, so those are worked out here.
    public["tags"] = asset.get("tags") or generate_asset_tags(asset)
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
    it. It isn't marked Secure, because this program serves plain HTTP.
    """
    return f"session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"


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


def add_asset_reply(body: dict) -> tuple:
    """Register an asset, returning an HTTP status and a JSON body. Only the expected
    values are passed on, so a browser cannot add anything else to the request."""
    status, reply, result = change_reply(
        {"action": "REGISTER", "request_id": body.get("request_id"), "asset": body.get("asset")}, 201
    )
    if reply is not None:
        result["asset"] = public_asset(reply["stored"])
    return status, result


def delete_asset_reply(asset_id: str, query: str) -> tuple:
    """Delete an asset, returning an HTTP status and a JSON body. The address's ?version=
    says which version the user was looking at, and is required, so that an asset someone
    else has just changed can't be deleted by accident."""
    try:
        version = int(parse_qs(query)["version"][0])
    except (KeyError, ValueError):
        return 400, {"error": "The version of the asset is needed"}

    status, _, result = change_reply(
        {"action": "DELETE", "asset_id": asset_id, "expected_version": version}, 200
    )
    return status, result


class WebHandler(BaseHTTPRequestHandler):
    """Answers one browser request. Each request runs on its own thread."""

    timeout = BROWSER_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        # Only the path matters, so anything after a "?" is ignored.
        path = urlsplit(self.path).path

        if path == "/api/assets":
            if self.is_logged_in():
                self.send_json(*assets_reply())
            else:
                self.send_json(401, {"error": "Please log in"})
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
        if path == "/api/login":
            self.handle_login()
        elif path == "/api/logout":
            self.handle_logout()
        elif path != "/api/assets":
            self.send_json(404, {"error": "There is nothing at this address"})
        elif not self.is_logged_in():
            self.send_json(401, {"error": "Please log in"})
        else:
            body = self.read_json_body()
            if body is not None:
                self.send_json(*add_asset_reply(body))

    def do_DELETE(self) -> None:
        if not self.is_from_this_site():
            self.send_json(403, {"error": "This request came from another site"})
            return

        address = urlsplit(self.path)
        if not address.path.startswith(ASSET_ADDRESS_PREFIX) or address.path == ASSET_ADDRESS_PREFIX:
            self.send_json(404, {"error": "There is nothing at this address"})
        elif not self.is_logged_in():
            self.send_json(401, {"error": "Please log in"})
        else:
            asset_id = unquote(address.path[len(ASSET_ADDRESS_PREFIX):])
            self.send_json(*delete_asset_reply(asset_id, address.query))

    def handle_login(self) -> None:
        body = self.read_json_body()
        if body is None:
            return

        guess = body.get("password")
        if not isinstance(guess, str):
            self.send_json(400, {"error": "A password is needed"})
            return

        status, reply, token = try_login(guess)
        if token is None:
            self.send_json(status, reply)
        else:
            self.send_json(status, reply, session_cookie(token, SESSION_SECONDS))

    def handle_logout(self) -> None:
        end_session(self.session_token())
        self.send_json(200, {"logged_in": False}, session_cookie("", 0))

    def session_token(self) -> object:
        """Return the token in the browser's cookie, or None if it has none."""
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("session")
        except CookieError:
            return None
        return morsel.value if morsel else None

    def is_logged_in(self) -> bool:
        return session_is_valid(self.session_token())

    def is_from_this_site(self) -> bool:
        """Refuse requests that a browser was tricked into sending by another website.

        Browsers name the site that made such a request in the Origin header. The session
        cookie already should not be sent in that case, so this is a second safeguard.
        """
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

    def read_json_body(self) -> object:
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

    def send_static_file(self, file_name: str, content_type: str) -> None:
        with open(os.path.join(STATIC_FOLDER, file_name), "rb") as file:
            self.send_body(200, content_type, file.read())

    def send_json(self, status: int, body: dict, cookie: str = None) -> None:
        self.send_body(status, "application/json", json.dumps(body).encode("utf-8"), cookie)

    def send_body(self, status: int, content_type: str, payload: bytes, cookie: str = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))

        if cookie is not None:
            self.send_header("Set-Cookie", cookie)

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
    global password_record

    if "--set-password" in sys.argv:
        set_password_from_prompt()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [web] %(message)s")
    password_record = load_or_create_password()

    # ThreadingHTTPServer gives each browser request its own thread, so one slow
    # browser does not delay the others.
    with ThreadingHTTPServer(WEB_ADDRESS, WebHandler) as web_server:
        logger.info("Web interface at http://%s:%s (press Ctrl+C to stop)", *WEB_ADDRESS)
        try:
            web_server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Web interface stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

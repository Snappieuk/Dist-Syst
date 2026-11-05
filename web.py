"""
The web interface of the Asset Management System, for browsing assets.

    python web.py

This is a separate program that sits between a browser and the servers. It speaks HTTP
to the browser, and the system's own TCP protocol to the servers, exactly as the other
clients do. It therefore needs no knowledge of which server is in charge, and keeps
working through a failover.

It only reads data. There is no login, so anyone who can open the page can see it, and
it listens only on this machine by default.
"""

import getpass    # asks for a new password without showing it on screen
import hashlib    # hashes the password, so that the password itself is never stored
import hmac       # compares password hashes safely
import json       # the data sent to the browser is JSON
import logging    # records what happens, including errors, without stopping
import os         # os.path finds the page's files
import secrets    # generates the first password
import sys        # sys.argv holds the --set-password option
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from common import generate_asset_tags, send_request

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

logger = logging.getLogger("asset_web")

password_record = None  # the hash of the web password, set when the program starts


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


def public_asset(asset: dict) -> dict:
    """Return only the parts of a stored asset that a browser may see."""
    public = {field: asset.get(field, "") for field in PUBLIC_FIELDS}

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


class WebHandler(BaseHTTPRequestHandler):
    """Answers one browser request. Each request runs on its own thread."""

    timeout = BROWSER_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        # Only the path matters, so anything after a "?" is ignored.
        path = urlsplit(self.path).path

        if path == "/api/assets":
            self.send_json(*assets_reply())
        elif path in STATIC_FILES:
            self.send_static_file(*STATIC_FILES[path])
        elif path == "/favicon.ico":
            # Browsers ask for a tab icon by themselves. There isn't one, and saying
            # so stops a "not found" error appearing on every visit.
            self.send_body(204, "image/x-icon", b"")
        else:
            self.send_json(404, {"error": "There is nothing at this address"})

    def send_static_file(self, file_name: str, content_type: str) -> None:
        with open(os.path.join(STATIC_FOLDER, file_name), "rb") as file:
            self.send_body(200, content_type, file.read())

    def send_json(self, status: int, body: dict) -> None:
        self.send_body(status, "application/json", json.dumps(body).encode("utf-8"))

    def send_body(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))

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

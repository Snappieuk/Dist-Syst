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

import json       # the data sent to the browser is JSON
import logging    # records what happens, including errors, without stopping
import os         # os.path finds the page's files
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
}

logger = logging.getLogger("asset_web")


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

    return 200, {"assets": [public_asset(asset) for asset in reply["assets"]]}


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [web] %(message)s")

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

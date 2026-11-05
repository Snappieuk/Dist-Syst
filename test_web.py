"""
Tests for the web interface.

    python -m unittest -v test_web

These run a real asset server and a real web server, and make real HTTP requests, so
they test the whole path from a browser to the data. The asset server uses a temporary
data file and spare ports, so your real assets.json is never touched.
"""

import json
import logging
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import common
import node
import web


# These tests cause errors on purpose, so the logged warnings are only noise here.
logging.getLogger("asset_web").setLevel(logging.ERROR)


def make_asset(description="Dell laptop", **changes) -> dict:
    """Build what a client sends to register an asset, optionally changing fields."""
    asset = {
        "description": description,
        "category": "IT Equipment",
        "location": "Site A",
        "status": "In Use",
    }
    asset.update(changes)
    return asset


def register(asset: dict) -> dict:
    return common.send_request(common.new_register_request(asset))


class WebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Port 0 asks the operating system for any free port.
        listener = node.create_listening_socket("127.0.0.1", 0)
        cls.asset_server = listener.getsockname()
        threading.Thread(target=node.run_server, args=(listener,), daemon=True).start()

        cls.web_server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.WebHandler)
        cls.addClassCleanup(cls.web_server.server_close)
        cls.addClassCleanup(cls.web_server.shutdown)
        threading.Thread(target=cls.web_server.serve_forever, daemon=True).start()

    def setUp(self):
        # Every test starts with no assets and its own temporary data file.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        node.ASSETS_FILE = os.path.join(temp_dir.name, "assets.json")
        node.assets.clear()
        node.deleted_ids.clear()
        node.last_seq = 0
        node.accepts_writes = True

        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [self.asset_server]

    def fetch(self, path: str, method: str = "GET") -> tuple:
        """Make an HTTP request, returning the status, headers and body."""
        port = self.web_server.server_address[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def fetch_json(self, path: str) -> tuple:
        status, _, body = self.fetch(path)
        return status, json.loads(body)


class AssetsApiTest(WebTestCase):
    def test_no_assets_gives_an_empty_list(self):
        self.assertEqual(self.fetch_json("/api/assets"), (200, {"assets": []}))

    def test_a_registered_asset_is_listed(self):
        asset_id = register(make_asset())["stored"]["asset_id"]

        status, body = self.fetch_json("/api/assets")

        self.assertEqual(status, 200)
        self.assertEqual(body["assets"][0]["asset_id"], asset_id)
        self.assertEqual(body["assets"][0]["description"], "Dell laptop")
        self.assertIn("dell", body["assets"][0]["tags"])

    def test_internal_fields_are_not_sent_to_the_browser(self):
        register(make_asset())

        _, body = self.fetch_json("/api/assets")

        self.assertEqual(
            set(body["assets"][0]),
            {"asset_id", "description", "category", "location", "status", "date_registered", "tags"},
        )

    def test_assets_saved_without_tags_still_get_them(self):
        node.assets.append(make_asset(asset_id="AST-OLD"))

        _, body = self.fetch_json("/api/assets")

        self.assertIn("dell", body["assets"][0]["tags"])

    def test_the_browser_is_told_not_to_keep_a_copy(self):
        _, headers, _ = self.fetch("/api/assets")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_an_unknown_address_is_not_found(self):
        status, body = self.fetch_json("/nothing-here")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_every_server_being_down_gives_a_clear_error(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            common.SERVERS = [spare.getsockname()]

        status, body = self.fetch_json("/api/assets")

        self.assertEqual(status, 503)
        self.assertIn("No asset server", body["error"])

    def test_a_server_error_gives_a_clear_error_without_its_details(self):
        node.assets.append(make_asset(asset_id="AST-BIG", description="x" * (common.MAX_MESSAGE_BYTES + 1)))

        status, body = self.fetch_json("/api/assets")

        self.assertEqual(status, 502)
        self.assertNotIn("bytes", body["error"])

    def test_changing_data_is_not_possible(self):
        register(make_asset())

        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method):
                status, _, _ = self.fetch("/api/assets", method)
                self.assertEqual(status, 501)  # not implemented
        self.assertEqual(len(node.assets), 1)


class PageTest(WebTestCase):
    def test_the_page_is_served(self):
        status, headers, body = self.fetch("/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Asset Browser", body)

    def test_the_script_is_served(self):
        status, headers, _ = self.fetch("/app.js")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_the_script_never_adds_asset_details_as_html(self):
        _, _, body = self.fetch("/app.js")

        for unsafe in (b".innerHTML", b".outerHTML", b"insertAdjacentHTML", b"document.write"):
            with self.subTest(unsafe):
                self.assertNotIn(unsafe, body)

    def test_security_headers_are_sent(self):
        for path in ("/", "/api/assets", "/nothing-here"):
            with self.subTest(path):
                _, headers, _ = self.fetch(path)
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_other_files_cannot_be_reached(self):
        for path in ("/web.py", "/index.html", "/../web.py", "/static/../web.py", "/%2e%2e/web.py", "/assets.json"):
            with self.subTest(path):
                self.assertEqual(self.fetch(path)[0], 404)


if __name__ == "__main__":
    unittest.main()

"""
Tests for the web interface.

    python -m unittest -v test_web

These run a real asset server and a real web server, and make real HTTP requests, so
they test the whole path from a browser to the data. The asset server uses a temporary
data file and spare ports, so your real assets.json is never touched.
"""

import contextlib
import io
import json
import logging
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import common
import node
import web


# These tests cause errors on purpose, so the logged warnings are only noise here.
logging.getLogger("asset_web").setLevel(logging.ERROR)


PASSWORD = "test password"


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

        # A cheap hash keeps the tests fast. The real one is slow on purpose.
        self.addCleanup(setattr, web, "password_record", web.password_record)
        web.password_record = web.hash_password(PASSWORD, iterations=1000)
        web.sessions.clear()
        web.failed_logins = 0
        web.locked_until = 0

        # Tests start logged in, unless they set this to None.
        status, headers, _ = self.fetch("/api/login", "POST", {"password": PASSWORD}, cookie=None)
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def fetch(self, path: str, method: str = "GET", body: dict = None, extra_headers: dict = None, **options) -> tuple:
        """Make an HTTP request, returning the status, headers and body. It sends the test's
        login, or another cookie if one is given with cookie=."""
        headers = dict(extra_headers or {})
        cookie = options.get("cookie", getattr(self, "cookie", None))
        if cookie:
            headers["Cookie"] = cookie
        data = None if body is None else json.dumps(body).encode("utf-8")

        port = self.web_server.server_address[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def fetch_json(self, path: str) -> tuple:
        status, _, body = self.fetch(path)
        return status, json.loads(body)


class PasswordTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        for name, value in (
            ("PASSWORD_FILE", os.path.join(temp_dir.name, "web_password.json")),
            ("HASH_ITERATIONS", 1000),  # the real number is slow on purpose
            ("password_record", None),
        ):
            self.addCleanup(setattr, web, name, getattr(web, name))
            setattr(web, name, value)

    def test_only_the_right_password_matches(self):
        web.password_record = web.hash_password("correct horse")

        self.assertTrue(web.password_matches("correct horse"))
        self.assertFalse(web.password_matches("wrong horse"))

    def test_the_password_itself_is_not_stored(self):
        web.save_password("correct horse")

        with open(web.PASSWORD_FILE, encoding="utf-8") as file:
            self.assertNotIn("correct horse", file.read())

    def test_identical_passwords_have_different_hashes(self):
        self.assertNotEqual(web.hash_password("same")["hash"], web.hash_password("same")["hash"])

    def test_the_first_run_creates_a_password_and_shows_it_once(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            first = web.load_or_create_password()
        self.assertIn("web password is:", output.getvalue())

        web.password_record = first
        shown = output.getvalue().split("web password is: ")[1].split()[0]
        self.assertTrue(web.password_matches(shown))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(web.load_or_create_password(), first)
        self.assertEqual(output.getvalue(), "")

    def test_setting_a_password_needs_a_long_enough_match(self):
        attempts = (
            (["short", "short"], False),
            (["long enough 1", "long enough 2"], False),
            (["long enough", "long enough"], True),
        )
        for answers, changes in attempts:
            with self.subTest(answers=answers):
                with mock.patch("getpass.getpass", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
                    web.set_password_from_prompt()
                self.assertEqual(os.path.exists(web.PASSWORD_FILE), changes)


class AssetsApiTest(WebTestCase):
    def test_no_assets_gives_an_empty_list(self):
        status, body = self.fetch_json("/api/assets")
        self.assertEqual(status, 200)
        self.assertEqual(body["assets"], [])

    def test_the_reply_names_the_server_that_answered(self):
        self.addCleanup(setattr, node, "ROLE", node.ROLE)

        for role in ("primary", "backup"):
            with self.subTest(role):
                node.ROLE = role
                self.assertEqual(self.fetch_json("/api/assets")[1]["served_by"], role)

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
            {"asset_id", "description", "category", "location", "status", "date_registered", "tags", "version"},
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

    def test_unsupported_changes_are_refused(self):
        register(make_asset())

        for method, path, expected in (("PUT", "/api/assets", 501), ("DELETE", "/api/assets", 404), ("DELETE", "/api/assets/", 404)):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.fetch(path, method)[0], expected)
        self.assertEqual(len(node.assets), 1)


class DeleteAssetTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = register(make_asset())["stored"]["asset_id"]

    def delete(self, asset_id: str = None, version: object = 1, **options) -> tuple:
        path = f"/api/assets/{asset_id or self.asset_id}?version={version}"
        status, _, reply = self.fetch(path, "DELETE", **options)
        return status, json.loads(reply)

    def test_an_asset_can_be_deleted(self):
        status, body = self.delete()

        self.assertEqual(status, 200)
        self.assertEqual(body["served_by"], "primary")
        self.assertEqual(node.assets, [])
        self.assertEqual(self.fetch_json("/api/assets")[1]["assets"], [])

    def test_deleting_needs_a_login(self):
        self.assertEqual(self.delete(cookie=None)[0], 401)
        self.assertEqual(len(node.assets), 1)

    def test_deleting_from_another_site_is_refused(self):
        status, _ = self.delete(extra_headers={"Origin": "http://evil.example"})

        self.assertEqual(status, 403)
        self.assertEqual(len(node.assets), 1)

    def test_an_asset_someone_else_changed_is_not_deleted(self):
        common.send_request({"action": "SET_STATUS", "asset_id": self.asset_id, "status": common.NOT_IN_USE})

        status, body = self.delete(version=1)

        self.assertEqual(status, 409)
        self.assertIn("changed by someone else", body["error"])
        self.assertEqual(len(node.assets), 1)

    def test_the_version_is_required(self):
        for path in (f"/api/assets/{self.asset_id}", f"/api/assets/{self.asset_id}?version=abc"):
            with self.subTest(path):
                self.assertEqual(self.fetch(path, "DELETE")[0], 400)
        self.assertEqual(len(node.assets), 1)

    def test_an_unknown_asset_is_not_found(self):
        self.assertEqual(self.delete("AST-NOPE")[0], 404)

    def test_an_id_with_unusual_characters_can_be_deleted(self):
        node.assets.append({**make_asset("Old"), "asset_id": "OLD ID/1", "version": 1})

        status, _ = self.delete("OLD%20ID%2F1")

        self.assertEqual(status, 200)
        self.assertEqual([asset["asset_id"] for asset in node.assets], [self.asset_id])


class AddAssetTest(WebTestCase):
    def add(self, asset: object = None, request_id: object = "request-1", **options) -> tuple:
        body = {"request_id": request_id, "asset": make_asset() if asset is None else asset}
        status, _, reply = self.fetch("/api/assets", "POST", body, **options)
        return status, json.loads(reply)

    def test_an_asset_can_be_added(self):
        status, body = self.add()

        self.assertEqual(status, 201)
        self.assertEqual(body["asset"]["description"], "Dell laptop")
        self.assertEqual(body["served_by"], "primary")
        self.assertNotIn("request_id", body["asset"])
        self.assertEqual([asset["asset_id"] for asset in node.assets], [body["asset"]["asset_id"]])

    def test_adding_needs_a_login(self):
        self.assertEqual(self.add(cookie=None)[0], 401)
        self.assertEqual(node.assets, [])

    def test_adding_from_another_site_is_refused(self):
        status, _ = self.add(extra_headers={"Origin": "http://evil.example"})

        self.assertEqual(status, 403)
        self.assertEqual(node.assets, [])

    def test_a_retry_does_not_add_a_second_asset(self):
        first = self.add()
        second = self.add()

        self.assertEqual(first[1]["asset"]["asset_id"], second[1]["asset"]["asset_id"])
        self.assertEqual(len(node.assets), 1)

    def test_a_duplicate_item_is_refused_with_a_useful_message(self):
        self.add(request_id="request-1")

        status, body = self.add(request_id="request-2")

        self.assertEqual(status, 409)
        self.assertIn("already registered", body["error"])
        self.assertEqual(len(node.assets), 1)

    def test_an_invalid_asset_is_refused_with_a_useful_message(self):
        status, body = self.add(make_asset(description=""))

        self.assertEqual(status, 400)
        self.assertIn("description", body["error"])

    def test_invalid_requests_are_refused(self):
        for asset in ([1, 2], "text", 7):
            with self.subTest(asset=asset):
                self.assertEqual(self.add(asset)[0], 400)
        self.assertEqual(node.assets, [])

    def test_a_browser_cannot_set_the_id_or_the_shared_secret(self):
        body = {"request_id": "r", "asset": make_asset(asset_id="MY-ID"), "auth": "wrong", "action": "DELETE"}

        status, _, reply = self.fetch("/api/assets", "POST", body)

        self.assertEqual(status, 201)
        self.assertNotEqual(json.loads(reply)["asset"]["asset_id"], "MY-ID")

    def test_every_server_being_down_gives_a_clear_error(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            common.SERVERS = [spare.getsockname()]

        status, body = self.add()

        self.assertEqual(status, 503)
        self.assertIn("No asset server", body["error"])

    def test_a_failed_save_gives_a_general_error(self):
        blocker = node.ASSETS_FILE + ".tmp"
        os.mkdir(blocker)
        self.addCleanup(os.rmdir, blocker)
        logging.getLogger("asset_server").disabled = True
        self.addCleanup(setattr, logging.getLogger("asset_server"), "disabled", False)

        status, body = self.add()

        self.assertEqual(status, 503)
        self.assertIn("try again", body["error"])


class LoginTest(WebTestCase):
    def login(self, password: object = PASSWORD, **options) -> tuple:
        return self.fetch("/api/login", "POST", {"password": password}, cookie=None, **options)

    def test_the_asset_list_needs_a_login(self):
        register(make_asset())

        status, _, body = self.fetch("/api/assets", cookie=None)

        self.assertEqual(status, 401)
        self.assertNotIn(b"Dell", body)

    def test_a_made_up_or_damaged_cookie_is_refused(self):
        for cookie in ("session=made-up", "session=", 'bad"cookie;;=', "other=value"):
            with self.subTest(cookie):
                self.assertEqual(self.fetch("/api/assets", cookie=cookie)[0], 401)

    def test_the_right_password_logs_in(self):
        status, headers, _ = self.login()

        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.fetch("/api/assets", cookie=cookie.split(";")[0])[0], 200)

    def test_a_wrong_password_gives_no_login(self):
        status, headers, _ = self.login("wrong")

        self.assertEqual(status, 401)
        self.assertIsNone(headers["Set-Cookie"])

    def test_invalid_login_requests_are_refused(self):
        self.assertEqual(self.login(7)[0], 400)

        for data in (b"not json", b"[1, 2]"):
            with self.subTest(data):
                port = self.web_server.server_address[1]
                request = urllib.request.Request(f"http://127.0.0.1:{port}/api/login", data=data, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()

    def test_a_huge_request_is_refused_without_reading_it(self):
        status, _, _ = self.fetch(
            "/api/login", "POST", {"password": "x" * (web.MAX_BODY_BYTES + 1)}, cookie=None
        )
        self.assertEqual(status, 400)

    def test_logging_out_ends_the_session(self):
        self.assertEqual(self.fetch("/api/assets")[0], 200)

        status, headers, _ = self.fetch("/api/logout", "POST")

        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.fetch("/api/assets")[0], 401)

    def test_an_expired_session_is_refused(self):
        self.addCleanup(setattr, web, "SESSION_SECONDS", web.SESSION_SECONDS)
        web.SESSION_SECONDS = 0
        _, headers, _ = self.login()

        self.assertEqual(self.fetch("/api/assets", cookie=headers["Set-Cookie"].split(";")[0])[0], 401)

    def test_too_many_wrong_passwords_lock_logins_for_a_while(self):
        for _ in range(web.FAILED_LOGIN_LIMIT):
            self.assertEqual(self.login("wrong")[0], 401)

        # Even the right password is refused now, but existing logins carry on.
        self.assertEqual(self.login()[0], 429)
        self.assertEqual(self.fetch("/api/assets")[0], 200)

        web.locked_until = 0
        self.assertEqual(self.login()[0], 200)

    def test_a_correct_login_resets_the_wrong_password_count(self):
        for _ in range(web.FAILED_LOGIN_LIMIT - 1):
            self.login("wrong")
        self.assertEqual(self.login()[0], 200)

        self.assertEqual(self.login("wrong")[0], 401)
        self.assertEqual(self.login()[0], 200)

    def test_requests_from_other_sites_are_refused(self):
        status, _, _ = self.login(extra_headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_requests_from_this_site_are_accepted(self):
        port = self.web_server.server_address[1]
        status, _, _ = self.login(extra_headers={"Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(status, 200)


class PageTest(WebTestCase):
    def test_the_page_is_served(self):
        status, headers, body = self.fetch("/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Asset Browser", body)

    def test_the_script_and_styles_are_served(self):
        for path, content_type in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            with self.subTest(path):
                status, headers, _ = self.fetch(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], f"{content_type}; charset=utf-8")

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

    def test_asking_for_a_tab_icon_is_not_an_error(self):
        self.assertEqual(self.fetch("/favicon.ico")[0], 204)

    def test_other_files_cannot_be_reached(self):
        for path in ("/web.py", "/index.html", "/../web.py", "/static/../web.py", "/%2e%2e/web.py", "/assets.json"):
            with self.subTest(path):
                self.assertEqual(self.fetch(path)[0], 404)


if __name__ == "__main__":
    unittest.main()

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


USERNAME = "tester"
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
        self.addCleanup(setattr, web, "USERS_FILE", web.USERS_FILE)
        self.addCleanup(setattr, web, "HASH_ITERATIONS", web.HASH_ITERATIONS)
        self.addCleanup(web.users.clear)
        web.USERS_FILE = os.path.join(temp_dir.name, "web_users.json")
        web.HASH_ITERATIONS = 1000
        web.users.clear()
        web.add_account(USERNAME, PASSWORD)
        web.sessions.clear()
        web.failed_logins = 0
        web.locked_until = 0

        # Tests start logged in, unless they set this to None.
        status, headers, _ = self.fetch("/api/login", "POST", {"username": USERNAME, "password": PASSWORD}, cookie=None)
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
            ("USERS_FILE", os.path.join(temp_dir.name, "web_users.json")),
            ("HASH_ITERATIONS", 1000),  # the real number is slow on purpose
        ):
            self.addCleanup(setattr, web, name, getattr(web, name))
            setattr(web, name, value)
        self.addCleanup(web.users.clear)
        web.users.clear()

    def test_only_the_right_password_matches(self):
        record = web.hash_password("correct horse")

        self.assertTrue(web.password_matches(record, "correct horse"))
        self.assertFalse(web.password_matches(record, "wrong horse"))

    def test_identical_passwords_have_different_hashes(self):
        self.assertNotEqual(web.hash_password("same")["hash"], web.hash_password("same")["hash"])

    def load_accounts_quietly(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            web.load_accounts()
        return output.getvalue()

    def test_a_first_run_with_nobody_to_ask_creates_an_account_and_shows_it_once(self):
        output = self.load_accounts_quietly()

        shown = output.split("Password: ")[1].split()[0]
        self.assertIn("Username: admin", output)
        self.assertTrue(web.password_matches(web.users["admin"], shown))

        self.assertEqual(self.load_accounts_quietly(), "")
        self.assertEqual(list(web.users), ["admin"])

    def test_a_password_from_before_accounts_becomes_the_admin_account(self):
        with open(web.PASSWORD_FILE, "w", encoding="utf-8") as file:
            json.dump(web.hash_password("my old password"), file)

        output = self.load_accounts_quietly()

        self.assertIn("admin", output)
        self.assertTrue(web.password_matches(web.users["admin"], "my old password"))
        self.assertFalse(os.path.exists(web.PASSWORD_FILE))

        # It only moves once, and it stays moved after a restart.
        web.users.clear()
        self.assertEqual(self.load_accounts_quietly(), "")
        self.assertTrue(web.password_matches(web.users["admin"], "my old password"))

    def create_from_prompt(self, username: str, *passwords) -> tuple:
        output = io.StringIO()
        with mock.patch("builtins.input", return_value=username), \
                mock.patch("getpass.getpass", side_effect=list(passwords)), \
                contextlib.redirect_stdout(output):
            return web.create_account_from_prompt(), output.getvalue()

    def test_the_command_line_can_create_an_account(self):
        result, _ = self.create_from_prompt("Alex", "long enough", "long enough")

        self.assertTrue(result)
        self.assertTrue(web.password_matches(web.users["alex"], "long enough"))

    def test_the_command_line_can_reset_a_lost_password(self):
        self.create_from_prompt("alex", "first password", "first password")

        result, _ = self.create_from_prompt("alex", "second password", "second password")

        self.assertTrue(result)
        self.assertTrue(web.password_matches(web.users["alex"], "second password"))

    def test_the_command_line_uses_admin_if_no_username_is_given(self):
        self.create_from_prompt("", "long enough", "long enough")
        self.assertEqual(list(web.users), ["admin"])

    def test_the_command_line_refuses_bad_details(self):
        for username, first, second, reason in (
            ("alex", "long enough", "different", "did not match"),
            ("alex", "short", "short", "at least"),
            ("<bad>", "long enough", "long enough", "username"),
        ):
            with self.subTest(reason):
                result, output = self.create_from_prompt(username, first, second)
                self.assertFalse(result)
                self.assertIn(reason, output)
        self.assertEqual(web.users, {})


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.addCleanup(setattr, web, "USERS_FILE", web.USERS_FILE)
        self.addCleanup(setattr, web, "HASH_ITERATIONS", web.HASH_ITERATIONS)
        self.addCleanup(web.users.clear)
        web.USERS_FILE = os.path.join(temp_dir.name, "web_users.json")
        web.HASH_ITERATIONS = 1000  # the real number is slow on purpose
        web.users.clear()

    def test_an_account_is_stored_without_its_password(self):
        self.assertIsNone(web.add_account("alex", "correct horse"))

        self.assertIn("alex", web.users)
        with open(web.USERS_FILE, encoding="utf-8") as file:
            stored = file.read()
        self.assertIn("alex", stored)
        self.assertNotIn("correct horse", stored)

    def test_usernames_ignore_capitals_and_spaces(self):
        web.add_account("  Alex ", "correct horse")

        self.assertEqual(list(web.users), ["alex"])
        self.assertIn("taken", web.add_account("ALEX", "another password"))

    def test_an_existing_account_is_only_replaced_on_request(self):
        web.add_account("alex", "first password")
        before = web.users["alex"]

        web.add_account("alex", "second password")
        self.assertEqual(web.users["alex"], before)

        self.assertIsNone(web.add_account("alex", "second password", replace=True))
        self.assertNotEqual(web.users["alex"], before)

    def test_unacceptable_usernames_are_refused(self):
        for username in ("", "ab", "x" * 33, "has space", "<script>", "café", "a/b"):
            with self.subTest(username):
                self.assertIn("username", web.add_account(username, "long enough"))
        self.assertEqual(web.users, {})

    def test_a_short_password_is_refused(self):
        self.assertIn("password", web.add_account("alex", "short"))
        self.assertEqual(web.users, {})

    def test_many_people_claiming_one_name_create_one_account(self):
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(web.add_account("alex", "long enough")))
            for _ in range(10)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count(None), 1)
        self.assertEqual(list(web.read_users_file()), ["alex"])


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
    def test_the_reply_says_who_is_logged_in(self):
        self.assertEqual(self.fetch_json("/api/assets")[1]["username"], USERNAME)

    def test_the_username_is_still_sent_during_an_outage(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            common.SERVERS = [spare.getsockname()]

        status, body = self.fetch_json("/api/assets")

        self.assertEqual(status, 503)
        self.assertEqual(body["username"], USERNAME)

    def test_each_login_is_kept_separate(self):
        web.add_account("second", "second password")
        _, headers, _ = self.login("second password", "second")
        second = headers["Set-Cookie"].split(";")[0]

        self.assertEqual(json.loads(self.fetch("/api/assets", cookie=second)[2])["username"], "second")
        self.assertEqual(self.fetch_json("/api/assets")[1]["username"], USERNAME)

        self.fetch("/api/logout", "POST", cookie=second)
        self.assertEqual(self.fetch("/api/assets", cookie=second)[0], 401)
        self.assertEqual(self.fetch_json("/api/assets")[0], 200)

    def login(self, password: object = PASSWORD, username: object = USERNAME, **options) -> tuple:
        body = {"username": username, "password": password}
        return self.fetch("/api/login", "POST", body, cookie=None, **options)

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

    def test_the_username_ignores_capitals(self):
        self.assertEqual(self.login(username=USERNAME.upper())[0], 200)

    def test_an_unknown_username_looks_the_same_as_a_wrong_password(self):
        unknown = self.login(username="nobody")
        wrong = self.login("wrong")

        self.assertEqual(unknown[0], 401)
        self.assertEqual(unknown[2], wrong[2])

    def test_an_unknown_username_still_does_the_slow_work(self):
        with mock.patch.object(web, "hash_password", wraps=web.hash_password) as hashing:
            self.login(username="nobody")
        hashing.assert_called_once()

    def test_failed_logins_with_different_usernames_all_count(self):
        for number in range(web.FAILED_LOGIN_LIMIT):
            self.login("wrong", username=f"user{number}")

        self.assertEqual(self.login()[0], 429)

    def test_invalid_login_requests_are_refused(self):
        self.assertEqual(self.login(7)[0], 400)
        self.assertEqual(self.login(username=7)[0], 400)

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

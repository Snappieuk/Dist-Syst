"""
Tests for the web interface.

    python -m unittest -v tests.test_web

These run a real asset server and a real web server, and make real HTTP requests, so
they test the whole path from a browser to the data. The asset server uses a temporary
data file and spare ports, so your real assets.json and accounts are never touched. The
programs do share secret.key, which is created in data/ if it doesn't exist yet.
"""

import contextlib
import io
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import unittest
from typing import Optional
import urllib.error
import urllib.request
from unittest import mock

import common
import node
import web

# The project folder, where the real data, accounts and secret live.

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
        node.deleted_at.clear()
        node.last_seq = 0
        node.accepts_writes = True

        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [self.asset_server]

        # A cheap hash keeps the tests fast. The real one is slow on purpose. Both account
        # files are temporary, so a test can never touch the real ones.
        self.addCleanup(setattr, web, "USERS_FILE", web.USERS_FILE)
        self.addCleanup(setattr, web, "PASSWORD_FILE", web.PASSWORD_FILE)
        self.addCleanup(setattr, web, "HASH_ITERATIONS", web.HASH_ITERATIONS)
        self.addCleanup(web.users.clear)
        web.USERS_FILE = os.path.join(temp_dir.name, "web_users.json")
        web.PASSWORD_FILE = os.path.join(temp_dir.name, "web_password.json")
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

    def fetch(self, path: str, method: str = "GET", body: Optional[dict] = None,
              extra_headers: Optional[dict] = None, **options) -> tuple:
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
        self.assertIn("taken", str(web.add_account("ALEX", "another password")))

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
                self.assertIn("username", str(web.add_account(username, "long enough")))
        self.assertEqual(web.users, {})

    def test_a_short_password_is_refused(self):
        self.assertIn("password", str(web.add_account("alex", "short")))
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
            {"asset_id", "description", "category", "location", "status", "date_registered", "tags", "version",
             "created_at", "updated_at", "flagged", "sample", "loans", "history"},
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

    def delete(self, asset_id: Optional[str] = None, version: object = 1, **options) -> tuple:
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


class EditAssetTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = register(make_asset())["stored"]["asset_id"]

    def edit(self, changes: object, version: object = 1, asset_id: Optional[str] = None, **options) -> tuple:
        body = {"changes": changes}
        if version is not None:
            body["version"] = version
        status, _, reply = self.fetch(f"/api/assets/{asset_id or self.asset_id}", "PATCH", body, **options)
        return status, json.loads(reply)

    def test_an_asset_can_be_edited(self):
        status, body = self.edit({"location": "Site B", "status": common.NOT_IN_USE})

        self.assertEqual(status, 200)
        self.assertEqual(body["asset"]["location"], "Site B")
        self.assertEqual(body["asset"]["version"], 2)
        self.assertEqual(body["served_by"], "primary")
        self.assertEqual(node.assets[0]["status"], common.NOT_IN_USE)
        self.assertNotIn("request_id", body["asset"])

    def test_editing_needs_a_login(self):
        self.assertEqual(self.edit({"location": "Site B"}, cookie=None)[0], 401)
        self.assertEqual(node.assets[0]["location"], "Site A")

    def test_editing_from_another_site_is_refused(self):
        status, _ = self.edit({"location": "Site B"}, extra_headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_the_version_is_required(self):
        for version in (None, "1", True):
            with self.subTest(version):
                self.assertEqual(self.edit({"location": "Site B"}, version=version)[0], 400)
        self.assertEqual(node.assets[0]["version"], 1)

    def test_a_change_someone_else_made_is_not_overwritten(self):
        self.edit({"location": "Site B"})

        status, body = self.edit({"location": "Site C"}, version=1)

        self.assertEqual(status, 409)
        self.assertIn("changed by someone else", body["error"])
        self.assertEqual(node.assets[0]["location"], "Site B")

    def test_an_edit_that_makes_a_duplicate_is_refused(self):
        register(make_asset("HP laptop"))

        status, _ = self.edit({"description": "HP laptop"})

        self.assertEqual(status, 409)

    def test_invalid_changes_are_refused(self):
        for changes in ({}, {"status": "Broken"}, {"asset_id": "MINE"}, "description"):
            with self.subTest(changes):
                self.assertEqual(self.edit(changes)[0], 400)

    def test_an_unknown_asset_is_not_found(self):
        self.assertEqual(self.edit({"location": "Site B"}, asset_id="AST-NOPE")[0], 404)

    def test_the_edit_is_recorded_with_who_made_it(self):
        with self.assertLogs("asset_web", "INFO") as logs:
            self.edit({"location": "Site B", "status": common.NOT_IN_USE})
        self.assertIn(f"{USERNAME} edited '{self.asset_id}' (location, status)", "\n".join(logs.output))

    def test_the_page_has_an_edit_form_offering_only_the_two_statuses(self):
        _, _, page = self.fetch("/", cookie=None)
        page = page.decode("utf-8")
        self.assertIn('id="edit-form"', page)
        edit_form = page[page.index('id="edit-form"'):]
        edit_form = edit_form[:edit_form.index("</form>")]
        self.assertEqual(edit_form.count("<option>"), 2)


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


class CreateAccountTest(WebTestCase):
    def create(self, username: object = "newuser", password: object = "new password", **options) -> tuple:
        body = {"username": username, "password": password}
        status, _, reply = self.fetch("/api/users", "POST", body, **options)
        return status, json.loads(reply)

    def test_a_logged_in_user_can_create_an_account_that_can_log_in(self):
        status, body = self.create("NewUser")

        self.assertEqual(status, 201)
        self.assertEqual(body["username"], "newuser")
        login = {"username": "newuser", "password": "new password"}
        self.assertEqual(self.fetch("/api/login", "POST", login, cookie=None)[0], 200)

    def test_the_new_account_survives_a_restart(self):
        self.create()

        web.users.clear()
        web.load_accounts()

        self.assertIn("newuser", web.users)

    def test_creating_an_account_needs_a_login(self):
        status, _ = self.create(cookie=None)

        self.assertEqual(status, 401)
        self.assertNotIn("newuser", web.users)

    def test_creating_an_account_from_another_site_is_refused(self):
        status, _ = self.create(extra_headers={"Origin": "http://evil.example"})

        self.assertEqual(status, 403)
        self.assertNotIn("newuser", web.users)

    def test_a_taken_username_is_refused_and_keeps_its_password(self):
        before = web.users[USERNAME]

        status, body = self.create(USERNAME.upper())

        self.assertEqual(status, 409)
        self.assertIn("taken", body["error"])
        self.assertEqual(web.users[USERNAME], before)

    def test_bad_details_are_refused_with_a_useful_message(self):
        for username, password, reason in (
            ("x", "new password", "username"),
            ("<script>", "new password", "username"),
            ("newuser", "short", "password"),
            (7, "new password", "needed"),
            ("newuser", None, "needed"),
        ):
            with self.subTest(username=username, password=password):
                status, body = self.create(username, password)
                self.assertEqual(status, 400)
                self.assertIn(reason, body["error"])
        self.assertEqual(list(web.users), [USERNAME])

    def test_password_hashes_are_never_sent_to_the_browser(self):
        _, reply = self.create()
        self.assertNotIn("hash", json.dumps(reply))
        self.assertNotIn("hash", json.dumps(self.fetch_json("/api/assets")[1]))


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


class AuditLogTest(WebTestCase):
    """The web log records who did what, but never a password."""

    def logged(self, action) -> str:
        with self.assertLogs("asset_web", "INFO") as logs:
            action()
        return "\n".join(logs.output)

    def test_logins_are_recorded_with_who_and_where_from(self):
        body = {"username": USERNAME, "password": PASSWORD}
        text = self.logged(lambda: self.fetch("/api/login", "POST", body, cookie=None))

        self.assertIn(f"{USERNAME} logged in from 127.0.0.1", text)
        self.assertNotIn(PASSWORD, text)

    def test_failed_logins_are_recorded_without_the_password(self):
        body = {"username": "intruder", "password": "a guessed password"}
        text = self.logged(lambda: self.fetch("/api/login", "POST", body, cookie=None))

        self.assertIn("Failed login as 'intruder' from 127.0.0.1", text)
        self.assertNotIn("a guessed password", text)

    def test_logging_out_is_recorded(self):
        text = self.logged(lambda: self.fetch("/api/logout", "POST"))
        self.assertIn(f"{USERNAME} logged out", text)

    def test_adding_and_deleting_say_who_did_it(self):
        body = {"request_id": "r1", "asset": make_asset("Line one\nFAKE LOG LINE")}
        added = {}

        def add():
            added.update(json.loads(self.fetch("/api/assets", "POST", body)[2])["asset"])

        text = self.logged(add)
        self.assertIn(f"{USERNAME} added {added['asset_id']}", text)
        self.assertIn(r"'Line one\nFAKE LOG LINE'", text)  # kept on one line

        text = self.logged(lambda: self.fetch(f"/api/assets/{added['asset_id']}?version=1", "DELETE"))
        self.assertIn(f"{USERNAME} deleted '{added['asset_id']}'", text)

    def test_a_refused_change_is_recorded(self):
        register(make_asset())
        body = {"request_id": "r2", "asset": make_asset()}

        text = self.logged(lambda: self.fetch("/api/assets", "POST", body))

        self.assertIn(f"{USERNAME} could not add an asset", text)


class RoleTest(WebTestCase):
    """Admins can do everything. Users can view, add and edit, but not delete or create accounts."""

    def setUp(self):
        super().setUp()
        web.add_account("worker", "worker password", role=web.USER)
        _, headers, _ = self.fetch("/api/login", "POST", {"username": "worker", "password": "worker password"},
                                   cookie=None)
        self.user_cookie = headers["Set-Cookie"].split(";")[0]
        self.asset_id = register(make_asset())["stored"]["asset_id"]

    def as_user(self, path: str, method: str = "GET", body: Optional[dict] = None) -> tuple:
        status, _, reply = self.fetch(path, method, body, cookie=self.user_cookie)
        return status, json.loads(reply)

    def test_the_first_account_is_an_admin(self):
        self.assertEqual(web.role_of(web.users[USERNAME]), web.ADMIN)
        self.assertEqual(self.fetch_json("/api/assets")[1]["role"], web.ADMIN)

    def test_accounts_from_before_roles_count_as_admins(self):
        record = web.hash_password("old password")
        web.write_users_file({"oldtimer": record})
        web.load_accounts()

        self.assertNotIn("role", web.read_users_file()["oldtimer"])
        self.assertTrue(web.is_admin("oldtimer"))

    def test_an_older_single_password_becomes_an_admin(self):
        os.remove(web.USERS_FILE)
        self.assertNotEqual(os.path.dirname(web.PASSWORD_FILE), common.DATA_FOLDER)  # never the real file
        with open(web.PASSWORD_FILE, "w", encoding="utf-8") as file:
            json.dump(web.hash_password("old password"), file)

        with contextlib.redirect_stdout(io.StringIO()):
            web.load_accounts()

        self.assertTrue(web.is_admin(web.FIRST_USERNAME))

    def test_a_user_is_told_their_role(self):
        self.assertEqual(self.as_user("/api/assets")[1]["role"], web.USER)

    def test_a_user_can_view_add_and_edit(self):
        self.assertEqual(self.as_user("/api/assets")[0], 200)
        status, _ = self.as_user("/api/assets", "POST", {"request_id": "u1", "asset": make_asset("HP laptop")})
        self.assertEqual(status, 201)
        status, _ = self.as_user(f"/api/assets/{self.asset_id}", "PATCH", {"version": 1, "changes": {"location": "B"}})
        self.assertEqual(status, 200)

    def test_only_an_admin_can_approve_a_flagged_asset(self):
        node.assets[0] = dict(node.assets[0], flagged=True)
        path = f"/api/assets/{self.asset_id}/approve"

        with self.assertLogs("asset_web", "WARNING"):
            self.assertEqual(self.as_user(path, "POST", {"version": 1})[0], 403)
        self.assertTrue(node.assets[0]["flagged"])

        status, _, reply = self.fetch(path, "POST", {"version": 1})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(reply)["asset"]["flagged"])

    def test_a_user_can_check_an_asset_out_and_in(self):
        status, body = self.as_user(f"/api/assets/{self.asset_id}/check-out", "POST",
                                    {"version": 1, "borrower": "Sam Lee", "start": "2026-11-01", "due": "2026-11-08"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["asset"]["loans"][0]["borrower"], "Sam Lee")
        self.assertEqual(body["asset"]["history"][-1]["user"], "worker")

        status, body = self.as_user(f"/api/assets/{self.asset_id}/check-in", "POST", {"version": 2})
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(body["asset"]["loans"][0]["returned"])

    def test_a_bad_loan_or_unknown_action_is_refused(self):
        status, body = self.as_user(f"/api/assets/{self.asset_id}/check-out", "POST",
                                    {"version": 1, "borrower": "Sam", "start": "2026-11-09", "due": "2026-11-01"})
        self.assertEqual(status, 400)
        self.assertIn("due date", body["error"])
        self.assertEqual(self.as_user(f"/api/assets/{self.asset_id}/explode", "POST", {"version": 1})[0], 404)
        self.assertEqual(self.as_user(f"/api/assets/{self.asset_id}/check-in", "POST", {})[0], 400)  # no version

    def test_the_browser_sees_flags_loans_and_history(self):
        asset = self.as_user("/api/assets")[1]["assets"][0]
        self.assertEqual((asset["flagged"], asset["sample"], asset["loans"]), (False, False, []))
        self.assertEqual(asset["history"][0]["action"], "REGISTER")

    def test_a_user_cannot_delete(self):
        with self.assertLogs("asset_web", "WARNING") as logs:
            status, body = self.as_user(f"/api/assets/{self.asset_id}?version=1", "DELETE")

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], web.ADMIN_ONLY)
        self.assertEqual(len(node.assets), 1)
        self.assertIn("worker tried to delete", logs.output[0])

    def test_a_user_cannot_create_accounts(self):
        status, _ = self.as_user("/api/users", "POST", {"username": "sneaky", "password": "sneaky password",
                                                        "role": web.ADMIN})

        self.assertEqual(status, 403)
        self.assertNotIn("sneaky", web.read_users_file())

    def test_an_admin_can_delete(self):
        status, _, _ = self.fetch(f"/api/assets/{self.asset_id}?version=1", "DELETE")
        self.assertEqual(status, 200)

    def test_an_admin_creates_users_unless_they_choose_admin(self):
        for name, chosen, expected in (("plain", None, web.USER), ("second-admin", web.ADMIN, web.ADMIN)):
            with self.subTest(name):
                body = {"username": name, "password": "a fine password"}
                if chosen:
                    body["role"] = chosen
                status, _, reply = self.fetch("/api/users", "POST", body)
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(reply)["role"], expected)
                self.assertEqual(web.role_of(web.read_users_file()[name]), expected)

    def test_an_unknown_role_is_refused(self):
        status, _, _ = self.fetch("/api/users", "POST", {"username": "boss", "password": "a fine password",
                                                         "role": "owner"})
        self.assertEqual(status, 400)
        self.assertNotIn("boss", web.read_users_file())

    def test_a_role_change_takes_effect_at_once(self):
        accounts = web.read_users_file()
        accounts["worker"]["role"] = web.ADMIN
        web.write_users_file(accounts)
        web.load_accounts()

        status, _ = self.as_user(f"/api/assets/{self.asset_id}?version=1", "DELETE")

        self.assertEqual(status, 200)

    def test_the_command_line_makes_admins_and_keeps_existing_roles(self):
        with mock.patch("builtins.input", return_value="fresh"), \
                mock.patch("getpass.getpass", return_value="fresh password"), \
                contextlib.redirect_stdout(io.StringIO()):
            web.create_account_from_prompt()
        with mock.patch("builtins.input", return_value="worker"), \
                mock.patch("getpass.getpass", return_value="new worker password"), \
                contextlib.redirect_stdout(io.StringIO()):
            web.create_account_from_prompt()

        accounts = web.read_users_file()
        self.assertEqual(web.role_of(accounts["fresh"]), web.ADMIN)
        self.assertEqual(web.role_of(accounts["worker"]), web.USER)

    def test_the_page_hides_admin_controls_until_it_knows_the_role(self):
        _, _, page = self.fetch("/", cookie=None)
        self.assertIn(b'id="manage-accounts" type="button" class="menu-item" hidden', page)
        self.assertIn(b'id="account" class="account-menu" hidden', page)


class ManageAccountsTest(WebTestCase):
    """Admins can list accounts, change their roles and delete them, but never remove the
    last admin or delete themselves."""

    def setUp(self):
        super().setUp()
        web.add_account("worker", "worker password", role=web.USER)

    def call(self, path: str, method: str = "GET", body: Optional[dict] = None, **options) -> tuple:
        status, _, reply = self.fetch(path, method, body, **options)
        return status, json.loads(reply)

    def log_in_as(self, username: str, password: str) -> str:
        _, headers, _ = self.fetch("/api/login", "POST", {"username": username, "password": password}, cookie=None)
        return headers["Set-Cookie"].split(";")[0]

    def test_an_admin_sees_every_account_and_role_but_no_passwords(self):
        status, body = self.call("/api/users")

        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"], [{"username": USERNAME, "role": "admin"},
                                            {"username": "worker", "role": "user"}])
        self.assertNotIn("hash", json.dumps(body))

    def test_a_role_can_be_changed(self):
        status, body = self.call("/api/users/worker", "PATCH", {"role": "admin"})

        self.assertEqual((status, body), (200, {"username": "worker", "role": "admin"}))
        self.assertEqual(web.role_of(web.read_users_file()["worker"]), web.ADMIN)

    def test_an_account_can_be_deleted_and_is_logged_out(self):
        worker = self.log_in_as("worker", "worker password")

        status, _ = self.call("/api/users/worker", "DELETE")

        self.assertEqual(status, 200)
        self.assertNotIn("worker", web.read_users_file())
        self.assertEqual(self.fetch("/api/assets", cookie=worker)[0], 401)

    def test_nobody_can_delete_their_own_account(self):
        status, body = self.call(f"/api/users/{USERNAME}", "DELETE")

        self.assertEqual(status, 409)
        self.assertIn("your own account", body["error"])
        self.assertIn(USERNAME, web.read_users_file())

    def test_the_last_admin_cannot_be_demoted(self):
        status, body = self.call(f"/api/users/{USERNAME}", "PATCH", {"role": "user"})

        self.assertEqual((status, body["error"]), (409, web.LAST_ADMIN))
        self.assertTrue(web.is_admin(USERNAME))

    def test_an_admin_can_step_down_once_there_is_another(self):
        self.call("/api/users/worker", "PATCH", {"role": "admin"})

        self.assertEqual(self.call(f"/api/users/{USERNAME}", "PATCH", {"role": "user"})[0], 200)
        self.assertFalse(web.is_admin(USERNAME))

    def test_unknown_accounts_and_roles_are_refused(self):
        self.assertEqual(self.call("/api/users/nobody", "PATCH", {"role": "user"})[0], 404)
        self.assertEqual(self.call("/api/users/nobody", "DELETE")[0], 404)
        self.assertEqual(self.call("/api/users/worker", "PATCH", {"role": "owner"})[0], 400)

    def test_usernames_in_the_address_ignore_capitals(self):
        self.assertEqual(self.call("/api/users/WORKER", "DELETE")[0], 200)

    def test_users_cannot_manage_accounts(self):
        worker = self.log_in_as("worker", "worker password")
        for path, method, body in (("/api/users", "GET", None), ("/api/users/worker", "PATCH", {"role": "admin"}),
                                   (f"/api/users/{USERNAME}", "DELETE", None)):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(path, method, body, cookie=worker)[0], 403)
        self.assertEqual(web.role_of(web.read_users_file()["worker"]), web.USER)
        self.assertIn(USERNAME, web.read_users_file())

    def test_managing_accounts_needs_a_login_and_this_site(self):
        self.assertEqual(self.call("/api/users", cookie=None)[0], 401)
        status, _ = self.call("/api/users/worker", "DELETE", extra_headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("worker", web.read_users_file())

    def test_changes_are_audited(self):
        with self.assertLogs(common.audit_logger, "INFO") as logs:
            self.call("/api/users/worker", "PATCH", {"role": "admin"})
            self.call("/api/users/worker", "DELETE")
        records = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual([(r["action"], r["user"], r["account"]) for r in records],
                         [("CHANGE_ROLE", USERNAME, "worker"), ("DELETE_ACCOUNT", USERNAME, "worker")])


class AuditFileTest(WebTestCase):
    """The audit log records logins and every change, with the web user who made it."""

    def audited(self, action) -> list:
        with self.assertLogs(common.audit_logger, "INFO") as logs:
            common.audit("TEST_START")
            action()
        return [json.loads(record.getMessage()) for record in logs.records][1:]

    def test_logins_failed_logins_and_logouts_are_recorded(self):
        records = self.audited(lambda: (
            self.fetch("/api/login", "POST", {"username": USERNAME, "password": PASSWORD}, cookie=None),
            self.fetch("/api/login", "POST", {"username": "intruder", "password": "secret guess"}, cookie=None),
            self.fetch("/api/logout", "POST"),
        ))

        self.assertEqual([(r["action"], r["user"]) for r in records],
                         [("LOGIN", USERNAME), ("LOGIN_FAILED", "intruder"), ("LOGOUT", USERNAME)])
        self.assertEqual(records[0]["address"], "127.0.0.1")
        self.assertNotIn("secret guess", json.dumps(records))
        self.assertNotIn(PASSWORD, json.dumps(records))

    def test_changes_through_the_page_name_the_web_user(self):
        body = {"request_id": "web-1", "asset": make_asset()}
        records = self.audited(lambda: self.fetch("/api/assets", "POST", body))
        asset_id = records[0]["asset_id"]
        records += self.audited(lambda: self.fetch(f"/api/assets/{asset_id}", "PATCH",
                                                   {"version": 1, "changes": {"location": "B"}}))
        records += self.audited(lambda: self.fetch(f"/api/assets/{asset_id}?version=2", "DELETE"))

        self.assertEqual([(r["action"], r["asset_id"], r["user"]) for r in records], [
            ("REGISTER", asset_id, USERNAME), ("EDIT", asset_id, USERNAME), ("DELETE", asset_id, USERNAME),
        ])
        self.assertEqual(records[0]["request_id"], "web-1")

    def test_creating_an_account_is_recorded(self):
        records = self.audited(lambda: self.fetch("/api/users", "POST",
                                                  {"username": "newbie", "password": "a fine password"}))
        self.assertEqual(records, [dict(records[0], action="CREATE_ACCOUNT", user=USERNAME,
                                        account="newbie", role=web.USER)])


class GuiDownloadTest(WebTestCase):
    """The "Download desktop app" link: gui.py with this system's settings filled in."""

    def download(self, **options) -> tuple:
        return self.fetch(web.GUI_DOWNLOAD_ADDRESS, **options)

    def test_it_needs_a_login(self):
        self.assertEqual(self.download(cookie=None)[0], 401)

    def test_it_is_a_download_of_gui_py(self):
        status, headers, _ = self.download()
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("gui.py", headers["Content-Disposition"])

    def test_it_holds_the_real_fingerprint_and_the_servers(self):
        import ast
        import ssl
        import gui

        _, _, body = self.download()
        source = body.decode("utf-8")
        settings = {}
        for node_ in ast.parse(source).body:  # read the two settings without running the file
            if (isinstance(node_, ast.Assign) and isinstance(node_.targets[0], ast.Name)
                    and node_.targets[0].id in ("SITE_SERVERS", "SITE_FINGERPRINT")):
                settings[node_.targets[0].id] = ast.literal_eval(node_.value)

        with open(common.ensure_tls_file(), "r", encoding="ascii") as file:
            pem = file.read()
        certificate = pem[pem.index("-----BEGIN CERTIFICATE-----"):]
        expected = gui.fingerprint(ssl.PEM_cert_to_DER_cert(certificate))
        self.assertEqual(settings["SITE_FINGERPRINT"], expected)
        # The test's asset server is on 127.0.0.1, which is what the browser used too.
        self.assertEqual(settings["SITE_SERVERS"], [tuple(self.asset_server)])

    def test_loopback_servers_become_the_address_the_browser_used(self):
        with mock.patch.object(common, "SERVERS", [("127.0.0.1", 65432), ("10.0.0.5", 65433)]):
            source = web.gui_with_site_settings("files.example")
        self.assertIn("SITE_SERVERS = [('files.example', 65432), ('10.0.0.5', 65433)]\n", source)

    def test_it_holds_no_secrets(self):
        _, _, body = self.download()
        self.assertNotIn(b"PRIVATE KEY", body)
        with open(common.SECRET_FILE, "rb") as file:
            self.assertNotIn(file.read().strip(), body)

    def test_an_unsafe_host_is_refused(self):
        with self.assertRaises(ValueError):
            web.gui_with_site_settings("x'; import os #")

    def test_the_page_links_to_it(self):
        _, _, body = self.fetch("/")
        self.assertIn(web.GUI_DOWNLOAD_ADDRESS.encode(), body)


class AppDownloadTest(WebTestCase):
    """The "Download Windows app" link: the C# app with this system's settings appended."""

    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        patcher = mock.patch.object(web, "APP_FILE", os.path.join(folder, "AssetApp.exe"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, **options) -> tuple:
        return self.fetch(web.APP_DOWNLOAD_ADDRESS, **options)

    def test_it_needs_a_login(self):
        self.assertEqual(self.download(cookie=None)[0], 401)

    def test_it_says_so_when_the_app_cannot_be_built(self):
        with mock.patch.object(web, "find_csc", return_value=None):
            self.assertEqual(self.download()[0], 404)

    def test_it_is_an_exe_ending_in_the_site_settings(self):
        if web.find_csc() is None:
            self.skipTest("the C# compiler is only on Windows")
        status, headers, body = self.download()

        self.assertEqual(status, 200)
        self.assertIn("AssetApp.exe", headers["Content-Disposition"])
        self.assertEqual(body[:2], b"MZ")  # the start of every Windows program
        settings = json.loads(body[body.rindex(web.APP_MARKER) + len(web.APP_MARKER):])
        self.assertEqual(settings["servers"], [f"{host}:{port}" for host, port in [self.asset_server]])
        self.assertEqual(settings["fingerprint"], web.certificate_fingerprint())

    def test_the_marker_is_not_written_whole_in_the_source(self):
        # The app must not find its own code when it looks for the settings.
        for name in os.listdir(web.APP_SOURCE_FOLDER):
            with open(os.path.join(web.APP_SOURCE_FOLDER, name), "rb") as file:
                self.assertNotIn(web.APP_MARKER, file.read())

    def test_loopback_servers_become_the_address_the_browser_used(self):
        with mock.patch.object(common, "SERVERS", [("127.0.0.1", 65432), ("10.0.0.5", 65433)]):
            self.assertEqual(web.site_servers("files.example"), [("files.example", 65432), ("10.0.0.5", 65433)])

    def test_the_page_links_to_it(self):
        _, _, body = self.fetch("/")
        self.assertIn(web.APP_DOWNLOAD_ADDRESS.encode(), body)


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

    def test_every_page_has_a_tab_and_a_section(self):
        _, _, body = self.fetch("/")

        for page in (b"assets", b"loans", b"people", b"calendar", b"review"):
            with self.subTest(page):
                self.assertIn(b'data-tab="' + page + b'"', body)
                self.assertIn(b'data-page="' + page + b'"', body)

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

"""
Tests for gui.py's HTTPS mode, used from outside the network through a reverse proxy.

    python -m unittest -v tests.test_gui_https

These run a real asset server and a real web.py, and put TLS in front of web.py with a
certificate signed by a test authority made here. gui.py is then pointed at
https://localhost:<port>, and checks the certificate the normal way against that
authority. Nothing is faked on the way: every request goes GUI -> HTTPS -> web.py ->
asset server. (The cryptography package, which the project already needs for its own
certificate, makes the test certificates. gui.py itself uses only the standard library.)
"""

import datetime
import gc
import ipaddress
import os
import socket
import ssl
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from typing import Optional
from unittest import mock

import common
import web
from tests import test_web

USERNAME, PASSWORD = test_web.USERNAME, test_web.PASSWORD


def make_test_certificates(folder: str) -> tuple:
    """Make a test authority and a certificate for localhost signed by it. Returns the paths
    of the authority's certificate and of the server's certificate and key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())

    def name(text: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])

    ca = (x509.CertificateBuilder().subject_name(name("Test CA")).issuer_name(name("Test CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    server = (x509.CertificateBuilder().subject_name(name("localhost")).issuer_name(ca.subject)
              .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                          x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                             critical=False)
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))

    paths = (os.path.join(folder, "ca.pem"), os.path.join(folder, "server.pem"))
    with open(paths[0], "wb") as file:
        file.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(paths[1], "wb") as file:
        file.write(server.public_bytes(serialization.Encoding.PEM))
        file.write(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    return paths


class HttpsGuiTest(test_web.WebTestCase):
    """The GUI's HTTPS mode, against a real web.py behind TLS."""

    @classmethod
    def setUpClass(cls):
        try:
            import gui
        except Exception as exc:  # tkinter is not installed
            raise unittest.SkipTest(f"gui.py cannot be imported here: {exc}")
        cls.gui = gui
        super().setUpClass()

        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.ca_path, server_path = make_test_certificates(folder.name)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_path)
        # From now on web.py only answers over TLS, as it does behind the proxy's HTTPS.
        cls.web_server.socket = context.wrap_socket(cls.web_server.socket, server_side=True)

    def fetch(self, path: str, method: str = "GET", body: Optional[dict] = None,
              extra_headers: Optional[dict] = None, **options) -> tuple:
        """The base class's way of making requests, over HTTPS."""
        data = None if body is None else __import__("json").dumps(body).encode("utf-8")
        headers = dict(extra_headers or {})
        cookie = options.get("cookie", getattr(self, "cookie", None))
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(f"{self.site}{path}", data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.trusting_context()))
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def trusting_context(self) -> ssl.SSLContext:
        """The normal checks, with the test authority added as a trusted one."""
        return ssl.create_default_context(cafile=self.ca_path)

    @property
    def site(self) -> str:
        return f"https://localhost:{self.web_server.server_address[1]}"

    def setUp(self):
        # Free the earlier tests' windows on this thread. Tcl crashes if a server thread
        # happens to be the one that frees them.
        gc.collect()
        gui = self.gui
        self.addCleanup(lambda: vars(gui.session).clear())
        patcher = mock.patch("gui.https_context", self.trusting_context)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.certificate_patch = patcher
        super().setUp()
        web.add_account("worker", "worker password", role=web.USER)
        gui.session.web_url, gui.session.opener = self.site, gui.new_https_connection()

    def send(self, request: dict) -> dict:
        return self.gui.send_request(request)

    def log_in(self, name: str = USERNAME, password: str = PASSWORD) -> dict:
        return self.send({"action": "LOGIN", "username": name, "password": password})

    def register_asset(self, **changes) -> dict:
        reply = self.send(self.gui.new_register_request(test_web.make_asset(**changes)))
        self.assertEqual(reply["status"], "OK", reply)
        return reply["stored"]

    # ---- Logging in ----

    def test_a_wrong_password_is_refused_with_the_servers_message(self):
        reply = self.log_in(password="not it")
        self.assertEqual((reply["status"], reply["code"]), ("ERROR", "UNAUTHORIZED"))
        self.assertIn("not correct", reply["detail"])

    def test_nothing_can_be_listed_before_logging_in(self):
        reply = self.send({"action": "LIST"})
        self.assertEqual((reply["status"], reply["code"]), ("ERROR", "UNAUTHORIZED"))

    def test_logging_in_gives_the_name_and_role_the_window_needs(self):
        reply = self.log_in()
        self.assertEqual((reply["status"], reply["username"], reply["role"]), ("OK", USERNAME, "admin"))
        self.assertEqual(self.log_in("worker", "worker password")["role"], "user")

    def test_too_many_wrong_passwords_say_so(self):
        for _ in range(web.FAILED_LOGIN_LIMIT):
            self.log_in(password="not it")
        reply = self.log_in()
        self.assertEqual(reply["status"], "ERROR")
        self.assertIn("Too many", reply["detail"])

    # ---- Everything the window does ----

    def test_the_whole_window_works_over_https(self):
        gui = self.gui
        self.log_in()
        stored = self.register_asset()
        asset_id = stored["asset_id"]

        listing = self.send({"action": "LIST"})
        self.assertEqual([asset["asset_id"] for asset in listing["assets"]], [asset_id])
        self.assertIn("served_by", listing)

        # Searching and filtering by status happen in the program, as the servers would.
        self.assertEqual(len(self.send({"action": "LIST", "query": "DELL"})["assets"]), 1)
        self.assertEqual(self.send({"action": "LIST", "query": "nothing like it"})["assets"], [])
        self.assertEqual(len(self.send({"action": "LIST", "status": "In Use"})["assets"]), 1)
        self.assertEqual(self.send({"action": "LIST", "status": "Not In Use"})["assets"], [])

        version = stored["version"]
        changed = self.send({"action": "SET_STATUS", "asset_id": asset_id, "status": "Not In Use",
                             "expected_version": version})
        self.assertEqual((changed["status"], changed["stored"]["status"]), ("OK", "Not In Use"))
        version = changed["stored"]["version"]

        edited = self.send({"action": "EDIT", "asset_id": asset_id, "changes": {"location": "Site B"},
                            "expected_version": version})
        self.assertEqual(edited["stored"]["location"], "Site B")
        version = edited["stored"]["version"]

        out = self.send({"action": "CHECK_OUT", "asset_id": asset_id, "borrower": "Sam", "start": "2026-01-01",
                         "due": "2026-01-08", "expected_version": version})
        self.assertEqual(out["status"], "OK", out)
        self.assertEqual(out["stored"]["loans"][-1]["borrower"], "Sam")
        version = out["stored"]["version"]

        back = self.send({"action": "CHECK_IN", "asset_id": asset_id, "expected_version": version})
        self.assertEqual(back["status"], "OK", back)
        self.assertTrue(back["stored"]["loans"][-1]["returned"])
        version = back["stored"]["version"]

        approved = self.send({"action": "APPROVE", "asset_id": asset_id, "expected_version": version})
        self.assertEqual(approved["status"], "OK", approved)
        version = approved["stored"]["version"]

        deleted = self.send({"action": "DELETE", "asset_id": asset_id, "expected_version": version})
        self.assertEqual(deleted["status"], "OK", deleted)
        self.assertEqual(self.send({"action": "LIST"})["assets"], [])

    def test_a_change_made_from_an_old_version_is_refused_as_a_conflict(self):
        self.log_in()
        stored = self.register_asset()
        reply = self.send({"action": "SET_STATUS", "asset_id": stored["asset_id"], "status": "Not In Use",
                           "expected_version": stored["version"] + 5})
        self.assertEqual((reply["status"], reply["code"]), ("ERROR", "CONFLICT"))
        self.assertTrue(reply["detail"])

    def test_a_duplicate_is_refused_with_the_servers_message(self):
        self.log_in()
        self.register_asset()
        reply = self.send(self.gui.new_register_request(test_web.make_asset()))
        self.assertEqual((reply["status"], reply["code"]), ("ERROR", "CONFLICT"))
        self.assertIn("already", reply["detail"].lower())

    def test_an_unknown_asset_is_not_found(self):
        self.log_in()
        reply = self.send({"action": "CHECK_IN", "asset_id": "AST-P999999", "expected_version": 1})
        self.assertEqual((reply["status"], reply["code"]), ("ERROR", "NOT_FOUND"))

    def test_a_missing_field_is_a_bad_request(self):
        self.log_in()
        reply = self.send({"action": "REGISTER", "request_id": "r1", "asset": {"description": "only this"}})
        self.assertEqual((reply["status"], reply["code"]), ("ERROR", "BAD_REQUEST"))

    def test_the_server_not_the_program_decides_what_a_user_may_do(self):
        self.log_in()
        stored = self.register_asset()
        self.log_in("worker", "worker password")

        for action in ("DELETE", "APPROVE"):
            with self.subTest(action):
                reply = self.send({"action": action, "asset_id": stored["asset_id"],
                                   "expected_version": stored["version"]})
                self.assertEqual((reply["status"], reply["code"]), ("ERROR", "FORBIDDEN"))
        self.assertEqual(len(self.send({"action": "LIST"})["assets"]), 1)

    def test_a_login_that_ended_gives_a_clear_message(self):
        self.log_in()
        web.sessions.clear()
        reply = self.send({"action": "LIST"})
        self.assertEqual(reply["code"], "UNAUTHORIZED")
        self.assertIn("log in again", reply["detail"])

    def test_no_asset_server_is_a_connection_error_the_window_already_handles(self):
        self.log_in()
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            common.SERVERS = [spare.getsockname()]
        with self.assertRaisesRegex(ConnectionError, "No asset server"):
            self.send({"action": "LIST"})

    def test_a_request_that_cannot_be_made_over_https_is_refused(self):
        with self.assertRaises(ValueError):
            self.send({"action": "PING"})

    # ---- The connection ----

    def test_a_certificate_from_an_unknown_authority_is_refused_and_nothing_is_sent(self):
        gui = self.gui
        self.certificate_patch.stop()  # back to the operating system's authorities only
        gui.session.opener = gui.new_https_connection()
        with mock.patch.object(web, "try_login", wraps=web.try_login) as login:
            with self.assertRaisesRegex(ConnectionError, "certificate"):
                self.log_in()
        login.assert_not_called()
        self.certificate_patch.start()

    def test_a_redirect_is_never_followed(self):
        handler = self.gui.NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 301, "Moved", {}, "http://elsewhere.example/"))

    def test_the_cookie_is_kept_between_requests_and_not_on_disk(self):
        self.log_in()
        names = os.listdir(os.getcwd())
        self.assertEqual(self.send({"action": "LIST"})["status"], "OK")
        self.assertEqual(os.listdir(os.getcwd()), names)

    # ---- Typing the address ----

    def test_https_addresses_are_tidied_and_checked(self):
        gui = self.gui
        self.assertEqual(gui.https_address(" https://Assets.Example.com/ "), "https://assets.example.com")
        self.assertEqual(gui.https_address("https://assets.example.com:8443"), "https://assets.example.com:8443")
        self.assertEqual(gui.https_address("HTTPS://10.0.0.5"), "https://10.0.0.5")
        for local in ("10.0.0.5", "assets.example.com:7000", ""):
            self.assertIsNone(gui.https_address(local))
        for bad in ("https://", "https://bad_name", "https://host/some/path", "https://host?x=1",
                    "https://user@host", "https://host:abc", "https://999.1.1"):
            with self.subTest(bad), self.assertRaises(ValueError):
                gui.https_address(bad)

    def test_plain_http_is_refused(self):
        with self.assertRaisesRegex(ValueError, "http://"):
            self.gui.https_address("http://assets.example.com")

    # ---- The login window ----

    def make_window(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except Exception as exc:
            raise unittest.SkipTest(f"No window can be opened here: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        done = mock.Mock()
        return root, self.gui.LoginWindow(root, done), done

    @staticmethod
    def wait_for(root, condition) -> None:
        end = time.monotonic() + 10
        while not condition() and time.monotonic() < end:
            root.update()
            time.sleep(0.02)

    def test_the_login_window_uses_https_mode_for_an_https_address(self):
        gui = self.gui
        vars(gui.session).clear()
        root, screen, done = self.make_window()
        screen.server_box.set(self.site)
        screen.username_box.insert(0, USERNAME)
        screen.password_box.insert(0, PASSWORD)

        screen.log_in()
        self.wait_for(root, lambda: done.called)

        done.assert_called_once()
        self.assertEqual((gui.session.username, gui.session.role), (USERNAME, "admin"))
        self.assertEqual(gui.session.web_url, self.site)
        self.assertEqual(self.send({"action": "LIST"})["status"], "OK")

    def test_the_check_button_says_the_site_answered(self):
        gui = self.gui
        vars(gui.session).clear()
        root, screen, _ = self.make_window()
        screen.server_box.set(self.site)

        screen.check()
        self.wait_for(root, lambda: "HTTPS" in screen.message.cget("text"))

        self.assertIn("answered over HTTPS", screen.message.cget("text"))

    def test_a_site_that_cannot_be_verified_is_reported_in_the_window(self):
        gui = self.gui
        vars(gui.session).clear()
        self.certificate_patch.stop()
        root, screen, done = self.make_window()
        screen.server_box.set(self.site)

        screen.check()
        self.wait_for(root, lambda: "Could not connect" in screen.message.cget("text"))
        self.certificate_patch.start()

        self.assertIn("certificate", screen.message.cget("text"))
        self.assertIsNone(gui.session.web_url)

    def test_a_downloaded_copy_starts_with_the_site_address_filled_in(self):
        with mock.patch.object(self.gui, "SITE_URL", "https://assets.example.com"):
            _, screen, _ = self.make_window()
            self.assertEqual(screen.server_box.get(), "https://assets.example.com")

    def test_a_local_network_address_leaves_https_mode(self):
        gui = self.gui
        root, screen, _ = self.make_window()
        screen.server_box.set("127.0.0.1:1")  # nothing listens here; only the mode matters

        screen.check()

        self.assertIsNone(gui.session.web_url)


if __name__ == "__main__":
    unittest.main()

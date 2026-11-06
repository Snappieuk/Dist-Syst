"""
Tests for the C# app's HTTPS mode (clients/app/WebNet.cs), on Windows only.

    python -m unittest -v tests.test_csharp_https

Net.cs and WebNet.cs are built with a small console program, so no window is needed. Windows
cannot be told to trust a test authority without changing the machine's certificate store, so
a complete login is not run here. What is tested is what matters for safety: the address rules,
that plain http:// is refused, and that a site whose certificate is not trusted is refused
before anything (such as a password) is sent. The window code and the translation of replies
are the same as gui.py's, which tests/test_gui_https.py runs against a real web.py.
"""

import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests import test_gui_https

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSC = os.path.join(os.environ.get("WINDIR", ""), "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")


@unittest.skipUnless(sys.platform == "win32" and os.path.exists(CSC), "needs the Windows C# compiler")
class CSharpHttpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.folder = folder.name
        cls.exe = os.path.join(folder.name, "harness.exe")
        sources = [os.path.join(ROOT, "clients", "app", "Net.cs"), os.path.join(ROOT, "clients", "app", "WebNet.cs"),
                   os.path.join(ROOT, "tests", "csharp_https_harness.cs")]
        build = subprocess.run([CSC, "/nologo", "/target:exe", f"/out:{cls.exe}", "/r:System.Web.Extensions.dll",
                                "/r:System.Web.dll", *sources], capture_output=True, text=True)
        if build.returncode != 0:
            raise AssertionError(f"The C# code does not build:\n{build.stdout}{build.stderr}")

    def run_harness(self, *args: str) -> str:
        done = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=60)
        return done.stdout.strip()

    def test_https_addresses_are_tidied(self):
        self.assertEqual(self.run_harness("address", " https://Assets.Example.com/ "), "https://assets.example.com")
        self.assertEqual(self.run_harness("address", "https://assets.example.com:8443"), "https://assets.example.com:8443")
        self.assertEqual(self.run_harness("address", "https://10.0.0.5"), "https://10.0.0.5")

    def test_local_network_addresses_are_not_https(self):
        for text in ("10.0.0.5", "assets.example.com:7000"):
            self.assertEqual(self.run_harness("address", text), "none")

    def test_bad_https_addresses_are_refused(self):
        for text in ("https://host/some/path", "https://host?x=1", "https://user@host", "https://bad_name",
                     "https://999.1.1"):
            with self.subTest(text):
                self.assertTrue(self.run_harness("address", text).startswith("error:"))

    def test_plain_http_is_refused(self):
        self.assertRegex(self.run_harness("address", "http://assets.example.com"), r"^error: Plain http://")

    def test_a_certificate_that_is_not_trusted_is_refused_before_anything_is_sent(self):
        ca_path, server_path = test_gui_https.make_test_certificates(self.folder)
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_path)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        answer = self.run_harness("login", f"https://localhost:{server.server_address[1]}")

        self.assertTrue(re.match(r"^error: The certificate of https://localhost:\d+ could not be verified", answer), answer)
        self.assertEqual(received, [])  # the request never got as far as the site


if __name__ == "__main__":
    unittest.main()

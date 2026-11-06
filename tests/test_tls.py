"""
Tests for the encryption of every connection to a server.

    python -m unittest -v tests.test_tls

These run real servers as separate programs on spare ports, with temporary data files.
They use the real tls.pem, creating it if it doesn't exist yet, as the other tests use
secret.key. A second certificate, for a server that must be refused, is temporary.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Optional
import common

# The project folder, one above this tests folder.
FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spare_address() -> tuple:
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()


class TlsTestCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.folder = temp_dir.name
        common.ensure_tls_file()

    def start_server(self, tls_file: Optional[str] = None) -> tuple:
        """Run server.py as its own program, alone, optionally with another certificate."""
        address = spare_address()
        code = (
            f"import common\ncommon.SERVERS = {[address]!r}\ncommon.LOG_FOLDER = {self.folder!r}\n"
            + (f"common.TLS_FILE = {tls_file!r}\n" if tls_file else "")
            + f"import server\nserver.ASSETS_FILE = {os.path.join(self.folder, 'assets.json')!r}\nserver.main()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=FOLDER, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)

        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(address, timeout=1).close()
                return address
            except OSError:
                self.assertLess(time.monotonic(), deadline, "the server never started")
                time.sleep(0.2)


class CertificateTest(TlsTestCase):
    def test_the_file_holds_a_key_and_a_self_signed_certificate(self):
        from cryptography import x509

        with open(common.TLS_FILE, "rb") as file:
            pem = file.read()
        certificate = x509.load_pem_x509_certificate(pem[pem.index(b"-----BEGIN CERTIFICATE"):])

        self.assertIn(b"BEGIN PRIVATE KEY", pem)
        self.assertEqual(certificate.issuer, certificate.subject)

    def test_an_existing_certificate_is_kept(self):
        with open(common.TLS_FILE, "rb") as file:
            before = file.read()

        common.ensure_tls_file()

        with open(common.TLS_FILE, "rb") as file:
            self.assertEqual(file.read(), before)

    def test_programs_starting_together_create_one_certificate(self):
        path = os.path.join(self.folder, "tls.pem")
        self.addCleanup(setattr, common, "TLS_FILE", common.TLS_FILE)
        common.TLS_FILE = path

        threads = [threading.Thread(target=common.ensure_tls_file) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with open(path, "rb") as file:
            self.assertEqual(file.read().count(b"BEGIN CERTIFICATE"), 1)


class EncryptionTest(TlsTestCase):
    def test_a_server_with_a_different_certificate_is_refused(self):
        impostor = os.path.join(self.folder, "impostor.pem")
        with open(impostor, "wb") as file:
            file.write(common.new_tls_pem())
        address = self.start_server(impostor)

        with self.assertRaises(OSError) as caught:
            common.request_from(address, {"action": "LIST"})
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(caught.exception))

        # To a client, such a server is no different from one that is down.
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = [address]
        with self.assertRaises(ConnectionError):
            common.send_request({"action": "LIST"})

    def test_the_real_server_is_trusted(self):
        address = self.start_server()
        self.assertEqual(common.request_from(address, {"action": "LIST"})["status"], "OK")

    def test_a_client_that_does_not_encrypt_gets_nothing(self):
        address = self.start_server()
        request = common.new_register_request({"description": "Plain laptop", "category": "IT",
                                               "location": "A", "status": common.IN_USE})
        request.update(auth=common.shared_secret(), protocol_version=common.PROTOCOL_VERSION)

        with socket.create_connection(address, timeout=5) as plain:
            common.send_message(plain, request)
            try:
                reply = plain.recv(100)
            except ConnectionError:
                reply = b""

        self.assertNotIn(b"OK", reply)
        self.assertEqual(common.request_from(address, {"action": "LIST"})["assets"], [])

    def test_nothing_readable_crosses_the_network(self):
        address = self.start_server()
        seen = bytearray()
        relay = self.start_relay(address, seen)

        reply = common.request_from(relay, common.new_register_request(
            {"description": "Confidential laptop", "category": "IT", "location": "A", "status": common.IN_USE}))

        self.assertEqual(reply["status"], "OK")
        self.assertGreater(len(seen), 0)
        for readable in (b"Confidential laptop", common.shared_secret().encode(), b"REGISTER", b"AST-P"):
            self.assertNotIn(readable, bytes(seen))

    def start_relay(self, target: tuple, seen: bytearray) -> tuple:
        """Pass one connection's bytes both ways between a client and `target`, keeping a
        copy of everything, as someone watching the network would see it."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)

        def pump(source, destination):
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    seen.extend(data)
                    destination.sendall(data)
            except OSError:
                pass
            finally:
                for end in (source, destination):
                    try:
                        end.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        def relay():
            client, _ = listener.accept()
            server = socket.create_connection(target)
            threading.Thread(target=pump, args=(client, server), daemon=True).start()
            pump(server, client)

        threading.Thread(target=relay, daemon=True).start()
        return listener.getsockname()


if __name__ == "__main__":
    unittest.main()

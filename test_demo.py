"""
Tests for the failover demonstration: stopping a server on request, and demo.py itself.

    python -m unittest -v test_demo

These run real servers as separate programs on spare ports, with temporary data files,
so your data is never touched.
"""

import contextlib
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import common
import demo

FOLDER = os.path.dirname(os.path.abspath(__file__))


def spare_address() -> tuple:
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()


class ServerTestCase(unittest.TestCase):
    """Starts real servers as separate programs."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.folder = temp_dir.name
        self.servers = [spare_address(), spare_address()]
        self.addCleanup(setattr, common, "SERVERS", common.SERVERS)
        common.SERVERS = self.servers

    def start(self, module: str) -> subprocess.Popen:
        code = (
            f"import common\ncommon.SERVERS = {self.servers!r}\n"
            f"import {module} as program\n"
            f"program.ASSETS_FILE = {os.path.join(self.folder, module + '.json')!r}\n"
            "program.main()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=FOLDER, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def wait_until_answering(self, address: tuple) -> None:
        deadline = time.monotonic() + 20
        while True:
            try:
                common.request_from(address, {"action": "PING"})
                return
            except OSError:
                if time.monotonic() > deadline:
                    self.fail(f"nothing answered on {address}")
                time.sleep(0.2)


class StopTest(ServerTestCase):
    def test_a_stop_request_ends_the_server_like_a_crash(self):
        primary = self.start("server")
        self.wait_until_answering(self.servers[0])

        reply = common.request_from(self.servers[0], {"action": "STOP"})

        self.assertEqual(reply["status"], "OK")
        self.assertEqual(primary.wait(timeout=10), 1)

    def test_a_stop_request_without_the_secret_is_refused(self):
        primary = self.start("server")
        self.wait_until_answering(self.servers[0])

        with socket.create_connection(self.servers[0], timeout=5) as sock:
            common.send_message(sock, {"action": "STOP"})
            reply = common.receive_message(sock)

        self.assertEqual(reply["code"], common.UNAUTHORIZED)
        time.sleep(0.5)
        self.assertIsNone(primary.poll())


class DemoTest(ServerTestCase):
    def run_quietly(self) -> tuple:
        output = io.StringIO()
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(output):
            result = demo.run_demo()
        return result, output.getvalue()

    def test_the_whole_demo_runs_and_leaves_no_assets_behind(self):
        primary = self.start("server")
        self.start("backup")
        self.wait_until_answering(self.servers[0])
        deadline = time.monotonic() + 20
        while demo.server_state(self.servers[1]) != "on standby" and time.monotonic() < deadline:
            time.sleep(0.2)

        def restart_when_stopped():
            # What start.py does, but sooner: once the backup has taken over and stored
            # the demo's second asset, rather than after a fixed 20 seconds.
            primary.wait()
            while not any(asset_id.startswith("AST-B") for asset_id in demo.held_by(self.servers[1])):
                time.sleep(0.2)
            self.start("server")

        threading.Thread(target=restart_when_stopped, daemon=True).start()

        result, output = self.run_quietly()

        self.assertTrue(result, output)
        self.assertIn("served by the primary", output)
        self.assertIn("served by the backup", output)
        self.assertNotIn("MISSING", output)
        self.assertEqual(primary.returncode, 1)
        self.assertEqual(demo.held_by(self.servers[0]), set())

    def test_it_explains_what_to_do_if_the_servers_are_not_running(self):
        result, output = self.run_quietly()

        self.assertFalse(result)
        self.assertIn("Start the primary and the backup first", output)


if __name__ == "__main__":
    unittest.main()

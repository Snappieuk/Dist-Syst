"""
Tests for configuring where the servers run.

    python -m unittest -v test_config

These never start a server. They check how the settings are read, including in real
processes, because environment variables are read when a program starts.
"""

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
import node

FOLDER = os.path.dirname(os.path.abspath(__file__))


def run_with(variables: dict, code: str) -> subprocess.CompletedProcess:
    """Run some Python in a new process with these environment variables set."""
    env = {**os.environ, **variables}
    for name, value in variables.items():
        if value is None:
            del env[name]
    return subprocess.run(
        [sys.executable, "-c", code], cwd=FOLDER, env=env, capture_output=True, text=True, timeout=30
    )


class ServerListTest(unittest.TestCase):
    def test_hostnames_and_ip_addresses_are_accepted(self):
        self.assertEqual(
            common.parse_servers("assets-1:65432, 10.0.0.2:65433"),
            [("assets-1", 65432), ("10.0.0.2", 65433)],
        )

    def test_a_single_server_is_accepted(self):
        self.assertEqual(common.parse_servers("assets-1:65432"), [("assets-1", 65432)])

    def test_bad_entries_are_named_in_the_error(self):
        for text in ("assets-1", "assets-1:", ":65432", "assets-1:port", "assets-1:0", "assets-1:70000", "a:1,"):
            with self.subTest(text):
                with self.assertRaisesRegex(ValueError, common.SERVERS_VARIABLE):
                    common.parse_servers(text)

    def test_more_than_two_servers_is_refused(self):
        with self.assertRaisesRegex(ValueError, "at most two"):
            common.parse_servers("a:1,b:2,c:3")

    def test_without_the_variable_everything_runs_on_this_machine(self):
        result = run_with({common.SERVERS_VARIABLE: None}, "import common; print(common.SERVERS)")
        self.assertEqual(result.stdout.strip(), repr(common.DEFAULT_SERVERS))

    def test_a_program_uses_the_list_it_is_given(self):
        result = run_with(
            {common.SERVERS_VARIABLE: "assets-1:65432,assets-2:65433"}, "import common; print(common.SERVERS)"
        )
        self.assertEqual(result.stdout.strip(), "[('assets-1', 65432), ('assets-2', 65433)]")


class HostnameTest(unittest.TestCase):
    def setUp(self):
        self.listener = node.create_listening_socket("127.0.0.1", 0)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]

    def test_a_server_found_by_name_answers_quickly(self):
        # localhost often has an IPv6 address too, which the servers don't listen on.
        def answer():
            conn, _ = self.listener.accept()
            with conn:
                common.receive_message(conn)
                common.send_message(conn, {"status": "OK"})

        threading.Thread(target=answer, daemon=True).start()

        started = time.monotonic()
        reply = common.request_from(("localhost", self.port), {"action": "PING"})

        self.assertEqual(reply["status"], "OK")
        self.assertLess(time.monotonic() - started, common.HEARTBEAT_TIMEOUT_SECONDS)

    def test_a_name_that_does_not_exist_counts_as_unreachable(self):
        with self.assertRaises(OSError):
            common.request_from(("assets-primary.invalid", self.port), {"action": "PING"})


class ListenAddressTest(unittest.TestCase):
    def test_a_server_normally_listens_on_its_own_entry(self):
        with mock.patch.dict(os.environ, {"ASSET_LISTEN_HOST": ""}):
            self.assertEqual(node.listening_address(("assets-1", 65432)), ("assets-1", 65432))

    def test_the_host_can_be_replaced_but_not_the_port(self):
        with mock.patch.dict(os.environ, {"ASSET_LISTEN_HOST": "0.0.0.0"}):
            self.assertEqual(node.listening_address(("assets-1", 65432)), ("0.0.0.0", 65432))

    def test_a_server_known_by_a_name_it_cannot_listen_on_still_starts(self):
        # Other machines would know this server as assets-primary.invalid, a name that
        # can't exist, so it can only start by listening on the address it is given.
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        code = (
            "import server\n"
            f"server.ASSETS_FILE = {os.path.join(temp_dir.name, 'assets.json')!r}\n"
            "server.main()\n"
        )
        env = {
            **os.environ,
            common.SERVERS_VARIABLE: f"assets-primary.invalid:{port}",
            "ASSET_LISTEN_HOST": "127.0.0.1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=FOLDER, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)

        deadline = time.monotonic() + 20
        while True:
            try:
                reply = common.request_from(("127.0.0.1", port), {"action": "PING"})
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    self.fail("the server did not start")
                time.sleep(0.2)

        self.assertEqual(reply["status"], "OK")


if __name__ == "__main__":
    unittest.main()

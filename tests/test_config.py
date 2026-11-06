"""
Tests for configuring where the servers run.

    python -m unittest -v tests.test_config

These never start a server. They check how the settings are read, including in real
processes, because environment variables are read when a program starts.
"""

import json
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

# The project folder, one above this tests folder.
FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

    def test_more_than_two_servers_are_allowed(self):
        self.assertEqual(common.parse_servers("a:1,b:2,c:3"), [("a", 1), ("b", 2), ("c", 3)])

    def test_more_servers_than_there_are_id_letters_are_refused(self):
        too_many = ",".join(f"host{number}:{1000 + number}" for number in range(common.MAX_SERVERS + 1))
        with self.assertRaisesRegex(ValueError, f"at most {common.MAX_SERVERS}"):
            common.parse_servers(too_many)

    def test_without_the_variable_everything_runs_on_this_machine(self):
        result = run_with({common.SERVERS_VARIABLE: None, "ASSET_CONFIG": os.path.join(FOLDER, "no-such-config.json")},
                          "import common; print(common.SERVERS)")
        self.assertEqual(result.stdout.strip(), repr(common.DEFAULT_SERVERS))

    def test_a_program_uses_the_list_it_is_given(self):
        result = run_with(
            {common.SERVERS_VARIABLE: "assets-1:65432,assets-2:65433"}, "import common; print(common.SERVERS)"
        )
        self.assertEqual(result.stdout.strip(), "[('assets-1', 65432), ('assets-2', 65433)]")


class ConfigFileTest(unittest.TestCase):
    """config.json is optional, is checked strictly, and environment variables beat it."""

    SHOW = "import common; print(common.SERVERS, common.WEB_ADDRESS, common.TIMEOUT_SECONDS, " \
           "common.HEARTBEAT_TIMEOUT_SECONDS, common.FAILOVER_WAIT_SECONDS)"

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = os.path.join(temp_dir.name, "config.json")

    def write(self, settings) -> None:
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(settings if isinstance(settings, str) else json.dumps(settings))

    def show(self, **variables) -> str:
        result = run_with({"ASSET_CONFIG": self.path, common.SERVERS_VARIABLE: None,
                           common.WEB_ADDRESS_VARIABLE: None, **variables}, self.SHOW)
        return (result.stdout + result.stderr).strip()

    def test_a_missing_file_gives_no_settings(self):
        self.assertEqual(common.load_config(self.path), {})

    def test_every_setting_is_read(self):
        self.write({"servers": ["assets-1:1001", "assets-2:1002"], "web_address": "0.0.0.0:9000",
                    "timeout_seconds": 7, "heartbeat_timeout_seconds": 0.5, "failover_wait_seconds": 30})

        self.assertEqual(self.show(), "[('assets-1', 1001), ('assets-2', 1002)] ('0.0.0.0', 9000) 7 0.5 30")

    def test_settings_left_out_keep_their_defaults(self):
        self.write({"timeout_seconds": 9})

        self.assertEqual(self.show(), f"{common.DEFAULT_SERVERS} {common.DEFAULT_WEB_ADDRESS} 9 1 15")

    def test_the_web_page_listens_on_every_network_by_default(self):
        self.assertEqual(common.DEFAULT_WEB_ADDRESS, ("0.0.0.0", 8080))

    def test_this_machine_connects_to_loopback_when_a_program_listens_everywhere(self):
        self.assertEqual(common.address_to_connect_to(("0.0.0.0", 8080)), ("127.0.0.1", 8080))
        self.assertEqual(common.address_to_connect_to(("::", 8080)), ("127.0.0.1", 8080))
        self.assertEqual(common.address_to_connect_to(("10.0.0.5", 8080)), ("10.0.0.5", 8080))

    def test_environment_variables_override_the_file(self):
        self.write({"servers": ["from-file:1001"], "web_address": "from-file:9000"})

        shown = self.show(**{common.SERVERS_VARIABLE: "from-env:2001", common.WEB_ADDRESS_VARIABLE: "from-env:9001"})

        self.assertEqual(shown, "[('from-env', 2001)] ('from-env', 9001) 5 1 15")

    def test_mistakes_are_reported_with_the_file_name(self):
        mistakes = {
            "not JSON": "{servers:",
            "not an object": [],
            "misspelt": {"timeout": 5},
            "servers not a list": {"servers": "a:1"},
            "empty servers": {"servers": []},
            "bad server": {"servers": ["no-port"]},
            "too many servers": {"servers": [f"host{number}:{1000 + number}" for number in range(26)]},
            "bad web address": {"web_address": "localhost"},
            "zero timeout": {"timeout_seconds": 0},
            "text timeout": {"failover_wait_seconds": "15"},
            "true timeout": {"heartbeat_timeout_seconds": True},
        }
        for name, settings in mistakes.items():
            with self.subTest(name):
                self.write(settings)
                with self.assertRaises(ValueError) as caught:
                    common.load_config(self.path)
                self.assertIn(self.path, str(caught.exception))

    def test_a_program_with_a_bad_file_stops_with_the_reason(self):
        self.write({"timeout": 5})
        self.assertIn("unknown settings: timeout", self.show())

    def test_the_example_file_matches_the_defaults(self):
        settings = common.load_config(os.path.join(FOLDER, "config.example.json"))

        self.assertEqual(settings["servers"], common.DEFAULT_SERVERS)
        self.assertEqual(settings["web_address"], common.DEFAULT_WEB_ADDRESS)
        self.assertEqual((settings["timeout_seconds"], settings["heartbeat_timeout_seconds"],
                          settings["failover_wait_seconds"]), (5, 1, 15))


class HostnameTest(unittest.TestCase):
    def setUp(self):
        self.listener = node.create_listening_socket("127.0.0.1", 0)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]

    def test_a_server_found_by_name_answers_quickly(self):
        # localhost often has an IPv6 address too, which the servers don't listen on.
        server_side = common.tls_context(server_side=True)
        common.tls_context(server_side=False)  # loaded now, so only the connection is timed

        def answer():
            conn, _ = self.listener.accept()
            with server_side.wrap_socket(conn, server_side=True) as tls:
                common.receive_message(tls)
                common.send_message(tls, {"status": "OK"})

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
            f"import common\ncommon.LOG_FOLDER = {temp_dir.name!r}\n"
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

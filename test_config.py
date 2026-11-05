"""
Tests for configuring where the servers run.

    python -m unittest -v test_config

These never start a server. They check how the settings are read, including in real
processes, because environment variables are read when a program starts.
"""

import os
import subprocess
import sys
import unittest

import common

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


if __name__ == "__main__":
    unittest.main()

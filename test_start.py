"""
Tests for the program that starts the whole system.

    python -m unittest -v test_start

These run short stand-in programs rather than the real servers, so they do not need the
system's ports and never touch your data.
"""

import subprocess
import sys
import unittest

import start


def run_for(seconds: float) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


class StartTest(unittest.TestCase):
    def setUp(self):
        self.processes = {}
        self.addCleanup(lambda: start.stop_all(self.processes))

    def test_all_the_programs_are_started(self):
        self.assertEqual(start.PROGRAMS, ("server.py", "backup.py", "web.py"))

    def test_nothing_is_reported_while_all_are_running(self):
        self.processes = {"one": run_for(30), "two": run_for(30)}
        self.assertIsNone(start.first_stopped(self.processes))

    def test_a_stopped_program_is_noticed(self):
        self.processes = {"running": run_for(30), "stopping": run_for(0)}
        self.assertEqual(start.wait_until_one_stops(self.processes), "stopping")

    def test_stopping_stops_everything_still_running(self):
        self.processes = {"one": run_for(30), "two": run_for(30), "finished": run_for(0)}
        self.processes["finished"].wait()

        start.stop_all(self.processes)

        self.assertTrue(all(process.poll() is not None for process in self.processes.values()))


if __name__ == "__main__":
    unittest.main()

"""
Tests for the program that starts the whole system.

    python -m unittest -v tests.test_start

These run short stand-in programs in place of the real servers and setup, so they do not need the system's ports and never touch your data.
"""

import contextlib
import io
import subprocess
import sys
import unittest
from unittest import mock

import start


def run_for(seconds: float) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


class StartTest(unittest.TestCase):
    def setUp(self):
        self.processes = {}
        self.addCleanup(lambda: start.stop_all(self.processes))

    def test_all_the_programs_are_started(self):
        self.assertEqual(start.PROGRAMS, ("server.py", "server.py --backup 1", "web.py"))

    def test_nothing_is_reported_while_all_are_running(self):
        self.processes = {"one": run_for(30), "two": run_for(30)}
        self.assertIsNone(start.first_stopped(self.processes))

    def test_a_program_that_fails_as_it_starts_stops_the_start(self):
        self.processes = {"running": run_for(30), "failing": run_for(0)}
        self.assertEqual(start.keep_running(self.processes), "failing")

    def test_a_program_that_stops_later_is_restarted_after_a_wait(self):
        self.processes = {"running": run_for(30), "stopped": run_for(0)}
        self.processes["stopped"].wait()
        stopped = self.processes["stopped"]
        long_ago = {"running": 0, "stopped": 0}  # both started well over START_FAILURE_SECONDS ago
        restart_at = {}

        with mock.patch.object(start, "start_program", side_effect=lambda _: run_for(30)), \
                self.assertLogs("start", "INFO") as logs:
            self.assertIsNone(start.check_programs(self.processes, long_ago, restart_at))
            self.assertIn("stopped", restart_at)
            self.assertIs(self.processes["stopped"], stopped)  # not restarted yet

            restart_at["stopped"] = 0  # the wait is over
            self.assertIsNone(start.check_programs(self.processes, long_ago, restart_at))

        self.assertIsNone(self.processes["stopped"].poll())  # running again
        self.assertEqual(restart_at, {})
        self.assertIn("stopped has stopped (exit code 0)", logs.output[0])
        self.assertIn("Restarting stopped", logs.output[1])

    def test_stopping_stops_everything_still_running(self):
        self.processes = {"one": run_for(30), "two": run_for(30), "finished": run_for(0)}
        self.processes["finished"].wait()

        start.stop_all(self.processes)

        self.assertTrue(all(process.poll() is not None for process in self.processes.values()))


class FirstStartTest(unittest.TestCase):
    def ensure(self, set_up: bool, at_keyboard: bool, setup_result: object = True) -> tuple:
        """Run the check with setup replaced, returning its result and whether setup ran."""
        effect = setup_result if isinstance(setup_result, type) else None
        with mock.patch.object(start.setup, "is_set_up", return_value=set_up), \
                mock.patch.object(start.setup, "can_ask_questions", return_value=at_keyboard), \
                mock.patch.object(start.setup, "run_setup", return_value=setup_result, side_effect=effect) as run_setup, \
                contextlib.redirect_stdout(io.StringIO()):
            return start.ensure_set_up(), run_setup.called

    def test_setup_runs_on_a_first_start(self):
        self.assertEqual(self.ensure(False, True), (True, True))

    def test_setup_is_skipped_once_it_is_done(self):
        self.assertEqual(self.ensure(True, True), (True, False))

    def test_with_no_keyboard_it_starts_without_asking(self):
        self.assertEqual(self.ensure(False, False), (True, False))

    def test_unfinished_setup_stops_the_start(self):
        self.assertEqual(self.ensure(False, True, False), (False, True))
        self.assertEqual(self.ensure(False, True, KeyboardInterrupt), (False, True))

    def test_nothing_is_started_if_setup_is_not_finished(self):
        with mock.patch.object(start, "ensure_set_up", return_value=False), \
                mock.patch.object(start, "start_programs") as start_programs:
            start.main()
        start_programs.assert_not_called()


if __name__ == "__main__":
    unittest.main()

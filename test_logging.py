"""
Tests for the log files.

    python -m unittest -v test_logging

Every log here is written to a temporary folder, so your real logs are never touched.
"""

import io
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import common

FOLDER = os.path.dirname(os.path.abspath(__file__))


def spare_address() -> tuple:
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()


class LoggingTestCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.folder = temp_dir.name
        self.logs = os.path.join(self.folder, "logs")

    def read_log(self, name: str) -> str:
        with open(os.path.join(self.logs, name), encoding="utf-8") as file:
            return file.read()

    def run_program(self, code: str, **kwargs) -> subprocess.Popen:
        """Run some Python as its own process, logging to the temporary folder."""
        code = f"import common\ncommon.LOG_FOLDER = {self.logs!r}\n" + code
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=FOLDER, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process


class LogFileTest(LoggingTestCase):
    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        audit = common.audit_logger
        saved = (root.handlers[:], root.level, sys.excepthook, common.LOG_FOLDER, audit.handlers[:])

        def restore():
            for handler in root.handlers + audit.handlers:
                handler.close()
            root.handlers[:], level, sys.excepthook, common.LOG_FOLDER, audit.handlers[:] = saved
            root.setLevel(level)

        self.addCleanup(restore)
        common.LOG_FOLDER = self.logs

        # The console handler writes to whatever sys.stderr is when it is created, so this
        # keeps the log lines out of the test output.
        quiet = mock.patch("sys.stderr", io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_each_line_says_when_and_which_program(self):
        common.configure_logging("primary")
        logging.getLogger("test").info("hello")

        line = self.read_log("primary.log").splitlines()[-1]

        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO \[primary\] \[MainThread\] hello$")

    def test_setting_it_up_twice_does_not_log_lines_twice(self):
        common.configure_logging("primary")
        common.configure_logging("primary")
        logging.getLogger("test").info("once")

        self.assertEqual(self.read_log("primary.log").count("once"), 1)

    def test_a_full_file_is_replaced_and_only_a_few_old_ones_are_kept(self):
        self.addCleanup(setattr, common, "LOG_FILE_BYTES", common.LOG_FILE_BYTES)
        common.LOG_FILE_BYTES = 1000
        common.configure_logging("primary")

        for number in range(500):
            logging.getLogger("test").info("line %d", number)

        files = sorted(name for name in os.listdir(self.logs) if name.startswith("primary.log"))
        self.assertEqual(files, ["primary.log"] + [f"primary.log.{n}" for n in range(1, common.LOG_FILE_COPIES + 1)])
        self.assertIn("line 499", self.read_log("primary.log"))

    def test_audit_records_go_only_to_the_audit_file(self):
        common.configure_logging("web")
        common.audit("LOGIN", user="alex", address="127.0.0.1")
        logging.getLogger("test").info("an everyday message")

        audit_lines = self.read_log("web-audit.log").splitlines()
        self.assertEqual(len(audit_lines), 1)
        record = json.loads(audit_lines[0])
        self.assertEqual((record["action"], record["user"]), ("LOGIN", "alex"))
        self.assertRegex(record["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("LOGIN", self.read_log("web.log"))
        self.assertNotIn("everyday", self.read_log("web-audit.log"))

    def test_a_log_file_that_cannot_be_opened_does_not_stop_the_program(self):
        with open(self.logs, "w"):
            pass  # a file where the folder should be

        common.configure_logging("primary")
        logging.getLogger("test").info("still running")


class ProgramLogTest(LoggingTestCase):
    def test_the_two_servers_write_separate_files(self):
        servers = [spare_address(), spare_address()]
        for module in ("server", "backup"):
            self.run_program(
                f"common.SERVERS = {servers!r}\nimport {module} as program\n"
                f"program.ASSETS_FILE = {os.path.join(self.folder, module + '.json')!r}\nprogram.main()\n"
            )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if "In sync" in self.read_log("backup.log") and "in sync" in self.read_log("primary.log"):
                    break
            except OSError:
                pass
            time.sleep(0.2)

        # A change made on the primary is in its audit file, but not the backup's, which
        # only applied a copy.
        reply = common.request_from(servers[0], common.new_register_request(
            {"description": "Audited laptop", "category": "IT", "location": "A", "status": common.IN_USE}))
        self.assertEqual(reply["status"], "OK")
        records = [json.loads(line) for line in self.read_log("primary-audit.log").splitlines()]
        self.assertEqual([(r["action"], r["asset_id"], r["server"]) for r in records],
                         [("REGISTER", reply["stored"]["asset_id"], "primary")])
        self.assertEqual(self.read_log("backup-audit.log"), "")

        primary, backup = self.read_log("primary.log"), self.read_log("backup.log")
        self.assertIn("[primary]", primary)
        self.assertNotIn("[backup]", primary)
        self.assertIn("[backup]", backup)
        self.assertNotIn("[primary]", backup)

    def test_a_crash_is_recorded_in_the_file(self):
        process = self.run_program('common.configure_logging("primary")\nraise RuntimeError("something broke")\n')
        process.wait(timeout=30)

        text = self.read_log("primary.log")
        self.assertIn("CRITICAL", text)
        self.assertIn("RuntimeError: something broke", text)


if __name__ == "__main__":
    unittest.main()

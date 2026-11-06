"""
Starts the whole system with one command, and opens the web page.

    python start.py

The first time, this runs setup, which asks you to create your web account.

Every server in the server list and the web interface still run as separate processes, exactly as if
each had been started in its own terminal. They share this window, so each log line
says which one wrote it. Press Ctrl+C to stop them all.

If one of them stops after starting properly, for example because demo.py stopped it,
the others carry on and it is restarted after RESTART_SECONDS, the way a cluster manager
such as Kubernetes restarts a failed server. One that fails as it starts, usually because
its port is in use, stops everything instead, so it can't be restarted over and over.
"""

import logging     # records starts, stops and restarts
import os          # os.path finds the programs
import socket      # checks when the web interface is ready
import subprocess  # runs each program as its own process
import sys         # sys.executable is the Python running this script
import time        # waits between checks
from typing import Optional  # type hints only
import webbrowser  # opens the web page

import setup
from common import SERVERS, address_to_connect_to, configure_logging
from web import WEB_ADDRESS

logger = logging.getLogger("start")

# Each program, with any options it is started with: one server for each address in the
# server list, then the web interface. Every server is server.py, told by --backup which
# entry in the list it is.
PROGRAMS = ("server.py", *(f"server.py --backup {index}" for index in range(1, len(SERVERS))), "web.py")

FOLDER = os.path.dirname(os.path.abspath(__file__))

# How long to wait for the web interface before opening the browser anyway.
WEB_START_SECONDS = 10

# How long a stopped program stays down before it is restarted. This is long enough for
# the backup to take over and for a client to make a change on it, which demo.py shows.
RESTART_SECONDS = 20

# A program that stops sooner than this after starting failed as it started. A port
# that is already in use stops a program well within this, while a demo can stop a
# server only a few seconds after the system starts.
START_FAILURE_SECONDS = 2


def ensure_set_up() -> bool:
    """Run first-time setup if it's needed, returning whether it's fine to start.

    If there is nobody at a keyboard to ask, it carries on, and web.py creates an
    account with a random password and prints it instead.
    """
    if setup.is_set_up() or not setup.can_ask_questions():
        return True

    try:
        if setup.run_setup():
            print()
            return True
    except (EOFError, KeyboardInterrupt):
        pass
    print("\nSetup was not finished, so nothing was started. Run this again to retry.")
    return False


def start_program(name: str) -> subprocess.Popen:
    """Start one program as its own process. `name` is the file and any options, such as
    "server.py --backup 1"."""
    file, *options = name.split()
    return subprocess.Popen([sys.executable, os.path.join(FOLDER, file), *options], cwd=FOLDER)


def start_programs(programs: tuple) -> dict:
    """Start each program as its own process, returning them by name."""
    return {name: start_program(name) for name in programs}


def wait_for_web(processes: dict) -> None:
    """Wait until the web interface accepts connections, or a program has stopped."""
    deadline = time.monotonic() + WEB_START_SECONDS
    while time.monotonic() < deadline and first_stopped(processes) is None:
        try:
            socket.create_connection(address_to_connect_to(WEB_ADDRESS), timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)


def first_stopped(processes: dict) -> object:
    """Return the name of a program that has stopped, or None if all are running."""
    for name, process in processes.items():
        if process.poll() is not None:
            return name
    return None


def check_programs(processes: dict, started: dict, restart_at: dict) -> Optional[str]:
    """Check every program once, returning the name of one that failed as it started.

    A program that stopped later is given a restart time in `restart_at`, and restarted
    once that time has passed. `started` holds when each program last started.
    """
    now = time.monotonic()
    for name, process in list(processes.items()):
        if name in restart_at:
            if now >= restart_at[name]:
                logger.info("Restarting %s.", name)
                processes[name] = start_program(name)
                started[name] = now
                del restart_at[name]
        elif process.poll() is not None:
            if now - started[name] < START_FAILURE_SECONDS:
                return name
            logger.warning("%s has stopped (exit code %s). The others carry on, and it will be "
                           "restarted in %s seconds.", name, process.returncode, RESTART_SECONDS)
            restart_at[name] = now + RESTART_SECONDS
    return None


def keep_running(processes: dict) -> str:
    """Keep the programs running until one fails as it starts, and return its name."""
    started = {name: time.monotonic() for name in processes}
    restart_at = {}
    while True:
        failed = check_programs(processes, started, restart_at)
        if failed is not None:
            return failed
        time.sleep(0.5)


def stop_all(processes: dict) -> None:
    """Stop every program still running, forcing any that do not stop in time."""
    for process in processes.values():
        if process.poll() is None:
            process.terminate()

    for process in processes.values():
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> None:
    if not ensure_set_up():
        return

    # Each program keeps its own log. This one, logs/start.log, records when the system
    # started and stopped, and every restart.
    configure_logging("start")
    logger.info("Starting %s.", ", ".join(PROGRAMS))
    processes = start_programs(PROGRAMS)
    try:
        wait_for_web(processes)
        if first_stopped(processes) is None:
            # Listening on 0.0.0.0 means everywhere, so this machine's browser uses 127.0.0.1.
            webbrowser.open("http://%s:%s" % address_to_connect_to(WEB_ADDRESS))

        # A program failing as it starts most likely means the system is already running
        # and its port is in use, so restarting it wouldn't help.
        stopped = keep_running(processes)
        logger.error("%s could not start, so stopping the others. Check the messages above.", stopped)
        stop_all(processes)
        try:
            # Keep the window open to read the error, if this was started by double-clicking.
            input("Press Enter to close...")
        except EOFError:
            pass  # no keyboard attached
    except KeyboardInterrupt:
        logger.info("Ctrl+C pressed, so stopping everything.")
    finally:
        stop_all(processes)
        logger.info("Everything has stopped.")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

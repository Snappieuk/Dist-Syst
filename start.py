"""
Starts the whole system with one command, and opens the web page.

    python start.py

The first time, this runs setup, which asks you to create your web account.

The primary, backup and web interface still run as separate processes, exactly as if
each had been started in its own terminal. They share this window, so each log line
says which one wrote it. Press Ctrl+C to stop them all.

To stop just one of them, for example to demonstrate a failover, start them in separate
terminals instead (see the README).
"""

import os          # os.path finds the programs
import socket      # checks when the web interface is ready
import subprocess  # runs each program as its own process
import sys         # sys.executable is the Python running this script
import time        # waits between checks
import webbrowser  # opens the web page

import setup
from web import WEB_ADDRESS

PROGRAMS = ("server.py", "backup.py", "web.py")

FOLDER = os.path.dirname(os.path.abspath(__file__))

# How long to wait for the web interface before opening the browser anyway.
WEB_START_SECONDS = 10


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


def start_programs(programs: tuple) -> dict:
    """Start each program as its own process, returning them by name."""
    return {
        name: subprocess.Popen([sys.executable, os.path.join(FOLDER, name)], cwd=FOLDER)
        for name in programs
    }


def wait_for_web(processes: dict) -> None:
    """Wait until the web interface accepts connections, or a program has stopped."""
    deadline = time.monotonic() + WEB_START_SECONDS
    while time.monotonic() < deadline and first_stopped(processes) is None:
        try:
            socket.create_connection(WEB_ADDRESS, timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)


def first_stopped(processes: dict) -> object:
    """Return the name of a program that has stopped, or None if all are running."""
    for name, process in processes.items():
        if process.poll() is not None:
            return name
    return None


def wait_until_one_stops(processes: dict) -> str:
    """Wait until any program stops, and return its name."""
    while True:
        stopped = first_stopped(processes)
        if stopped is not None:
            return stopped
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

    processes = start_programs(PROGRAMS)
    try:
        wait_for_web(processes)
        if first_stopped(processes) is None:
            webbrowser.open(f"http://{WEB_ADDRESS[0]}:{WEB_ADDRESS[1]}")

        # They should run until stopped, so one stopping means something is wrong, most
        # likely that the system is already running and its port is in use.
        stopped = wait_until_one_stops(processes)
        print(f"\n{stopped} has stopped, so stopping the others. Check the messages above.")
        stop_all(processes)
        try:
            # Keep the window open to read the error, if this was started by double-clicking.
            input("Press Enter to close...")
        except EOFError:
            pass  # no keyboard attached
    except KeyboardInterrupt:
        print("\nStopping everything...")
    finally:
        stop_all(processes)


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

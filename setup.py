"""
First-time setup and full reset of the Asset Management System.

    python setup.py
    python setup.py --reset

Setup creates the secret the programs share, and asks you to create the first web account.
start.py runs it by itself the first time, so you rarely need to run it directly.

Reset permanently deletes every asset, account, secret and log on this machine, leaving
the system as if it had just been downloaded. It stops any of the system running on this
machine first (after you confirm). It doesn't touch the code or the git history.
"""

import json    # reads how many assets a reset would delete
import os      # checks which files exist, and deletes them
import signal  # ends programs on systems other than Windows
import socket  # checks whether any part of the system is running
import subprocess  # lists processes and ends them
import sys     # sys.argv holds the --reset option; checks whether there is someone to ask
import time    # waits for stopped programs to let go of their ports

import common
import server
import web

# How many times to ask for the first account's details before giving up.
ACCOUNT_ATTEMPTS = 3

# What must be typed to confirm a reset. Having to type a word rather than just pressing y
# makes it much harder to delete everything by accident.
RESET_WORD = "RESET"

# How long to wait when checking whether a program is running.
PORT_CHECK_SECONDS = 1

# How long to wait for stopped programs to finish stopping.
STOP_WAIT_SECONDS = 10


def can_ask_questions() -> bool:
    """Check that someone is at a keyboard. Waiting for an answer that can never come would
    hang the program, for example when it is started by another program.

    On Windows, isatty() is also true when input comes from the null device, so Windows
    is asked directly whether it is a real console.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if os.name != "nt":
        return True

    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno()))
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD())))


def has_account() -> bool:
    """Check for an account, or an older password that web.py will convert to one."""
    return os.path.exists(web.USERS_FILE) or os.path.exists(web.PASSWORD_FILE)


def is_set_up() -> bool:
    return os.path.exists(common.SECRET_FILE) and os.path.exists(common.TLS_FILE) and has_account()


def run_setup() -> bool:
    """Create whatever is missing, returning whether the system is now set up."""
    common.shared_secret()
    common.ensure_tls_file()  # the certificate that encrypts every connection

    if has_account():
        return True

    print("Create your account for the web page.")
    print(f"The password must be at least {web.MINIMUM_PASSWORD_LENGTH} characters, and won't show as you type.\n")
    for _ in range(ACCOUNT_ATTEMPTS):
        if web.create_account_from_prompt():
            return True
    return False


def servers_data_files() -> list:
    """Each server's data file, one for every entry in the server list."""
    return [server.data_file(index) for index in range(len(common.SERVERS))]


def generated_files() -> list:
    """Every file the system creates as it runs. A reset deletes only these, by exact name,
    so that it can never remove anything else."""
    data_and_accounts = servers_data_files() + [web.USERS_FILE, web.PASSWORD_FILE]
    logs = []
    log_names = [server.name_of(index) for index in range(len(common.SERVERS))] + ["web", "start"]
    for name in log_names:
        for current in (common.log_path(name), common.audit_log_path(name)):
            logs += [current] + [f"{current}.{number}" for number in range(1, common.LOG_FILE_COPIES + 1)]
    return (data_and_accounts + [common.SECRET_FILE, common.TLS_FILE]
            + [path + ".tmp" for path in data_and_accounts] + logs)


def is_running(address: tuple) -> bool:
    try:
        socket.create_connection(address, timeout=PORT_CHECK_SECONDS).close()
        return True
    except OSError:
        return False


def running_programs() -> list:
    """Return the name of each part of the system that is currently running."""
    addresses = {"server.py": common.SERVERS[0], "web.py": common.address_to_connect_to(web.WEB_ADDRESS)}
    for index in range(1, len(common.SERVERS)):
        addresses[f"server.py --backup {index}"] = common.SERVERS[index]
    return [name for name, address in addresses.items() if is_running(address)]


def process_table() -> dict:
    """Return every process on this machine as {pid: (parent pid, command line)}."""
    table = {}
    if os.name == "nt":
        command = ["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_Process | ForEach-Object "
                   "{ \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }"]
        separator = "|"
    else:
        command = ["ps", "-eo", "pid=,ppid=,args="]
        separator = None  # ps pads with spaces
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in output.splitlines():
        parts = line.strip().split(separator, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def listening_pids(port: int) -> set:
    """Return the process IDs listening on a TCP port on this machine."""
    pids = set()
    if os.name == "nt":
        command = ["netstat", "-ano", "-p", "TCP"]
    else:
        command = ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"]
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return pids
    for line in output.splitlines():
        parts = line.split()
        if os.name != "nt":
            pids.update(int(part) for part in parts if part.isdigit())
        elif len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].rsplit(":", 1)[-1] == str(port):
            pids.add(int(parts[4]))
    return pids


def stop_process(pid: int) -> None:
    """Ask the operating system to end one process. Failing is fine: the caller checks
    afterwards whether the programs really stopped."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass


def stop_programs() -> bool:
    """Stop everything that is running on this machine, returning whether it all stopped.

    Each program is found by the port it listens on. If start.py started it, start.py is
    stopped first, because it restarts any program that stops. A server on another
    machine can't be stopped from here, so that makes this return False.
    """
    table = process_table()
    ports = [address[1] for address in common.SERVERS] + [web.WEB_ADDRESS[1]]
    programs = set()
    starters = []
    for port in ports:
        for pid in listening_pids(port):
            programs.add(pid)
            # Walk up through the parents looking for a start.py.
            parent = table.get(pid, (None, ""))[0]
            while parent in table and parent not in starters:
                command = table[parent][1].replace("\\", "/").split()
                if any(part.strip('"').endswith("start.py") for part in command):
                    starters.append(parent)
                parent = table[parent][0]

    for pid in starters + sorted(programs):
        if pid != os.getpid():
            stop_process(pid)

    # They can take a moment to let go of their ports.
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while running_programs():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def count_assets(path: str) -> int:
    """Return how many assets a data file holds, or 0 if it can't be read. A damaged file
    must not stop a reset, which may be just what is needed to recover from it."""
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return 0
    assets = data.get("assets", []) if isinstance(data, dict) else data
    return len(assets) if isinstance(assets, list) else 0


def reset() -> bool:
    """Delete everything the system has generated, once the user confirms. Returns whether
    anything was deleted.

    Anything running is stopped first, after the same confirmation. A running server holds
    its data in memory, so it would simply save it all again, or give it to the other
    server when they reconcile.
    """
    running = running_programs()

    existing = [path for path in generated_files() if os.path.exists(path)]
    if not existing:
        print("Nothing to reset: the system is already bare.")
        return False

    print("This will permanently delete:")
    for path in existing:
        print(f"  {os.path.basename(path)}")
    assets = max(count_assets(path) for path in servers_data_files())
    print(f"\nThat includes {assets} asset(s), every web account, the shared secret, the certificate and the logs.")
    if running:
        print(f"\nThese are running, and will be stopped first: {', '.join(running)}")
    print("It can't be undone.")

    if input(f"Type {RESET_WORD} to continue: ").strip() != RESET_WORD:
        print("Nothing was deleted.")
        return False

    if running:
        print("Stopping them...")
        if not stop_programs():
            print(f"Nothing was deleted, because these are still running: {', '.join(running_programs())}")
            print("Stop them yourself (a server on another machine has to be stopped there), then run this again.")
            return False

    failed = []
    for path in existing:
        try:
            os.remove(path)
        except OSError as exc:
            failed.append(f"{os.path.basename(path)} ({exc})")

    # Each rmdir only succeeds if nothing else was put in the folder.
    for folder in (common.LOG_FOLDER, common.DATA_FOLDER):
        try:
            os.rmdir(folder)
        except OSError:
            pass

    if failed:
        print("These could not be deleted, so the reset is not complete:")
        for item in failed:
            print(f"  {item}")
    else:
        print("\nReset complete. To set the system up again, run: python start.py")
    return True


def main() -> None:
    if "--reset" in sys.argv:
        try:
            reset()
        except (EOFError, KeyboardInterrupt):
            print("\nNothing was deleted.")
        return

    if is_set_up():
        print("Already set up. Start the system with: python start.py")
        print("To change an account's password, run: python web.py --set-password")
        return

    try:
        finished = run_setup()
    except (EOFError, KeyboardInterrupt):
        finished = False

    if finished:
        print("\nSetup complete. Start the system with: python start.py")
    else:
        print("\nSetup was not finished. Run it again with: python setup.py")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()

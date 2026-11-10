"""
Resets the system to a fresh install, then optionally sets it up again.

    python reset.py          (asks you to type RESET first)
    python reset.py --yes    (no confirmation, for scripts)

Deletes every file the system creates, and nothing else: both servers' asset data,
the audit logs, every account, the token-signing key and the TLS certificate and
key. The code is never touched. Stop both servers first; the reset refuses to run
while either is still answering, because a running server would simply save its
data again.

Afterwards it offers to create a new certificate and admin account straight away,
so the servers can be started again at once.

If you run the servers on two machines, reset each of them.
"""

import getpass
import os
import socket
import sys

import certificate
import common
import users

CONFIRM_WORD = "RESET"


def generated_files(config: dict) -> list:
    """Every file the system creates, by exact name, so a reset can't delete anything else."""
    names = (
        list(config["data_files"].values())
        + list(config["audit_files"].values())
        + [config["users_file"], config["secret_file"], config["certificate_file"], config["private_key_file"]]
    )
    paths = [common.config_path(config, name) for name in names]
    return paths + [path + ".tmp" for path in paths]


def running_servers(config: dict) -> list:
    """The roles of any servers still accepting connections."""
    running = []
    for role, server in config["servers"].items():
        try:
            socket.create_connection((server["host"], server["port"]), timeout=1).close()
            running.append(role)
        except OSError:
            pass
    return running


def reset(config: dict, confirm: bool = True) -> bool:
    """Delete every generated file. Returns whether the reset went ahead."""
    running = running_servers(config)
    if running:
        print(f"Stop the {' and '.join(running)} server(s) first (Ctrl+C in their windows), then try again.")
        return False

    existing = [path for path in generated_files(config) if os.path.exists(path)]
    if not existing:
        print("Nothing to reset: the system is already a fresh install.")
        return True

    print("This permanently deletes:")
    for path in existing:
        print(f"  {os.path.basename(path)}")
    print("That is every asset, every account, the audit logs and the certificate. It can't be undone.")
    if confirm and input(f"Type {CONFIRM_WORD} to continue: ").strip() != CONFIRM_WORD:
        print("Nothing was deleted.")
        return False

    failed = []
    for path in existing:
        try:
            os.remove(path)
        except OSError as exc:
            failed.append(f"{os.path.basename(path)} ({exc})")
    if failed:
        print("Could not delete: " + ", ".join(failed))
        return False
    print("Reset complete.")
    return True


def set_up_again(config: dict) -> bool:
    """Create a new certificate and admin account. Returns whether it worked."""
    certificate.create_certificate(config)
    print(f"Created a new {config['certificate_file']} and {config['private_key_file']}.")
    for _ in range(3):
        password = getpass.getpass("Choose a password for the admin account: ")
        if password != getpass.getpass("Type it again: "):
            print("The passwords did not match.")
            continue
        try:
            users.add_user(config, "admin", password, "admin")
        except ValueError as exc:
            print(exc)
            continue
        print("Created the admin account. Start the servers with: python server.py  and  python server.py backup")
        return True
    print("No account was created. Run: python users.py add admin admin")
    return False


def main(arguments: list) -> None:
    config = common.load_config()
    if not reset(config, confirm="--yes" not in arguments):
        return
    answer = input("Set up again now (new certificate and admin account)? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        set_up_again(config)
    else:
        print("To set up later, run: python certificate.py  then  python users.py add admin admin")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except EOFError:
        pass  # no keyboard to answer
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

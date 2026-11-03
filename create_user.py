"""
Create or replace a user account in users.json.

    python create_user.py alice admin
    python create_user.py bob viewer
    python create_user.py alice admin --replace

The password is prompted for rather than taken from the command line. WHY:
command-line arguments are saved in shell history and visible to other programs
in the process list, which would defeat the point of hashing it.

Restart the server afterwards, as it loads the accounts when it starts.
"""

import argparse  # reads the username and role from the command line
import getpass   # reads a password without showing it on the screen
import sys       # sys.exit() reports failure to the operating system

from auth import ROLES, USERS_FILE, hash_password, load_users, save_users

MIN_PASSWORD_LENGTH = 8


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or replace a user account.")
    parser.add_argument("username")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("--replace", action="store_true",
                        help="overwrite the account if it already exists")
    args = parser.parse_args()

    username = args.username.strip()
    if not username:
        sys.exit("The username must not be empty.")

    try:
        users = load_users(USERS_FILE)
    except FileNotFoundError:
        users = {}  # the normal case when creating the first account
    except ValueError as exc:
        # Do not carry on and overwrite accounts that might be recoverable.
        sys.exit(f"Could not read the existing accounts: {exc}")

    # Refusing by default protects against changing someone's password by mistake.
    if username in users and not args.replace:
        sys.exit(f"User {username!r} already exists. Use --replace to overwrite it.")

    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("The passwords do not match.")

    users[username] = {**hash_password(password), "role": args.role}
    save_users(USERS_FILE, users)
    print(f"Saved {args.role} account {username!r} in {USERS_FILE}.")
    print("Restart the server for this to take effect.")


if __name__ == "__main__":
    main()

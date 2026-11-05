"""
First-time setup of the Asset Management System.

    python setup.py

This creates the secret the programs share, and asks you to choose the web password.
start.py runs this by itself the first time, so you rarely need to run it directly.
"""

import os  # checks which files already exist
import sys  # checks whether there is someone to ask

import common
import web

# How many times to ask for a password before giving up.
PASSWORD_ATTEMPTS = 3


def can_ask_questions() -> bool:
    """Check that someone is at a keyboard. Waiting for an answer that can never come would
    hang the program, for example when it is started by another program."""
    return sys.stdin is not None and sys.stdin.isatty()


def is_set_up() -> bool:
    return os.path.exists(common.SECRET_FILE) and os.path.exists(web.PASSWORD_FILE)


def run_setup() -> bool:
    """Create whatever is missing, returning whether the system is now set up."""
    common.shared_secret()

    if os.path.exists(web.PASSWORD_FILE):
        return True

    print("Choose a password for the web page.")
    print(f"It must be at least {web.MINIMUM_PASSWORD_LENGTH} characters, and won't show as you type.\n")
    for _ in range(PASSWORD_ATTEMPTS):
        if web.set_password_from_prompt():
            return True
    return False


def main() -> None:
    if is_set_up():
        print("Already set up. Start the system with: python start.py")
        print("To change the web password, run: python web.py --set-password")
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

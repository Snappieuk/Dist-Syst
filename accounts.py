"""
Logins for programs that cannot hold the shared secret, such as gui.py.

The command-line clients and web.py prove themselves with the shared secret in
data/secret.key. A GUI handed to people on other computers must not carry that file,
so it logs in with a username and password instead, and gets a token to send with each
later request. The accounts are the same ones the web page uses (data/web_users.json).

The token is "username:expiry:signature". The signature is an HMAC of the first two
parts, made with the shared secret, so only a server of this system can make one, and
the token can't be edited to change the user or the expiry. Because it is checked rather
than looked up, a token made by the primary is also accepted by the backup, which is what
lets a GUI fail over without logging in again. Each request also looks the account up
again, so a deleted account or a changed role takes effect at once.

Accounts are read from this machine's own data folder, so every server machine needs a
copy of web_users.json (as it needs a copy of secret.key).
"""

import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any  # type hints only
import common

# The same file web.py keeps its accounts in.
USERS_FILE = os.path.join(common.DATA_FOLDER, "web_users.json")

# How long a login lasts, and the roles, as on the web page.
TOKEN_SECONDS = 8 * 60 * 60
ADMIN = "admin"

# The cost web.py uses for new passwords, used here to make a missing account as slow as a wrong one.
HASH_ITERATIONS = 600_000

# Failed logins in a row before logins are refused for a while, as on the web page.
FAILED_LOGIN_LIMIT = 5
LOCKOUT_SECONDS = 60

failed_logins = 0
locked_until = 0.0
login_lock = threading.Lock()  # protects the two values above


class LoginError(Exception):
    """A login that was refused. The message is safe to show the user."""


def read_users() -> dict:
    """The stored accounts, or none if there is no file."""
    try:
        with open(USERS_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


def role_of(record: dict) -> str:
    """An account's role. One saved before roles were added counts as an admin."""
    return record.get("role", ADMIN)


def hash_password(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8", "surrogatepass"), bytes.fromhex(salt), iterations
    ).hex()


def sign(text: str) -> str:
    return hmac.new(common.shared_secret().encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def login(username: str, password: str) -> tuple:
    """Check a username and password, returning (token, username, role) or raising
    LoginError. An unknown username gets the same message, and does the same slow
    hashing, as a wrong password, so neither reveals which usernames exist."""
    global failed_logins, locked_until

    with login_lock:
        if time.monotonic() < locked_until:
            raise LoginError("Too many failed logins. Please wait a minute and try again.")

    username = username.strip().lower()
    record = read_users().get(username)
    if record is None:
        # Hash anyway, so a missing account takes as long as a wrong password.
        hash_password(password, "00" * 16, HASH_ITERATIONS)
        matched = False
    else:
        attempt = hash_password(password, record["salt"], record["iterations"])
        matched = hmac.compare_digest(attempt, record["hash"])

    if not matched:
        with login_lock:
            failed_logins += 1
            if failed_logins >= FAILED_LOGIN_LIMIT:
                failed_logins = 0
                locked_until = time.monotonic() + LOCKOUT_SECONDS
        raise LoginError("That username or password is not correct")

    with login_lock:
        failed_logins = 0
    expires = int(time.time()) + TOKEN_SECONDS
    return f"{username}:{expires}:{sign(f'{username}:{expires}')}", username, role_of(record or {})


def check_token(token: Any) -> tuple:
    """Return (username, role) for a current token, or raise LoginError."""
    try:
        username, expires, signature = token.split(":")
        valid = hmac.compare_digest(signature, sign(f"{username}:{expires}"))
        current = valid and int(expires) > time.time()
    except (AttributeError, ValueError, TypeError):  # not text, or not in three parts
        current = False
    record = read_users().get(username) if current else None
    if record is None:
        raise LoginError("Your login has expired or is not valid. Please log in again.")
    return username, role_of(record)

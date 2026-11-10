"""
User accounts, passwords and login tokens.

    python users.py add USERNAME admin    (create an administrator, or reset a password)
    python users.py add USERNAME user     (create an ordinary user)
    python users.py list                  (show every account and its role)

Accounts live in the users file named in config.json. Only a salted hash of each
password is stored, never the password itself. Roles:
    admin  may do everything, including deleting assets
    user   may register, list, search and edit assets, but not delete them

When someone logs in, the server gives them a session token that is valid for
session_seconds (config.json). The token is signed with the key in secret.key, so
it can't be forged or altered, and any server holding the same key can check it.
"""

import base64    # makes the token safe to send as text
import getpass   # asks for a password without showing it
import hashlib   # hashes passwords
import hmac      # signs tokens and compares secrets in constant time
import json      # the users file and the token contents are JSON
import os        # creates files that only their owner can read
import secrets   # generates salts and the signing key
import sys       # reads the command line
import time      # sets and checks token expiry

import common

ROLES = ("admin", "user")
HASH_ITERATIONS = 600_000      # PBKDF2 rounds: slow on purpose, to make guessing expensive
MINIMUM_PASSWORD_LENGTH = 8


class TokenError(ValueError):
    """A token is missing, malformed, forged or expired."""


# ---- Passwords ----------------------------------------------------------------

def hash_password(password: str, salt: str = None, iterations: int = HASH_ITERATIONS) -> dict:
    """Return a salted PBKDF2-SHA256 hash of a password. A random salt means two
    identical passwords have different hashes, so precomputed tables are useless."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return {"salt": salt, "iterations": iterations, "hash": digest.hex()}


def password_matches(record: dict, password: str) -> bool:
    attempt = hash_password(password, record["salt"], record["iterations"])
    return hmac.compare_digest(attempt["hash"], record["hash"])


# ---- The users file -----------------------------------------------------------

def load_users(config: dict) -> dict:
    """Return every account, by username. An absent file means no accounts yet."""
    try:
        with open(common.config_path(config, config["users_file"]), encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


def write_private_file(path: str, text: str) -> None:
    """Write a file atomically, readable only by its owner on Linux and macOS."""
    temp_path = path + ".tmp"
    with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def add_user(config: dict, username: str, password: str, role: str, iterations: int = HASH_ITERATIONS) -> None:
    """Create an account, or replace its password and role if it exists.
    Raises ValueError if the username, password or role is not acceptable."""
    username = username.strip().lower()
    if not username.isidentifier() or len(username) > 32:
        raise ValueError("A username must be 1-32 letters, digits or underscores, starting with a letter")
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise ValueError(f"A password must be at least {MINIMUM_PASSWORD_LENGTH} characters")
    if role not in ROLES:
        raise ValueError(f"The role must be one of: {', '.join(ROLES)}")
    ensure_secret(config)
    users = load_users(config)
    users[username] = {**hash_password(password, iterations=iterations), "role": role}
    write_private_file(common.config_path(config, config["users_file"]), json.dumps(users, indent=2))


# ---- Signed tokens ------------------------------------------------------------

def ensure_secret(config: dict) -> None:
    """Create the token-signing key if there isn't one yet."""
    path = common.config_path(config, config["secret_file"])
    if not os.path.exists(path):
        write_private_file(path, secrets.token_hex(32))


def read_secret(config: dict) -> bytes:
    with open(common.config_path(config, config["secret_file"]), encoding="utf-8") as file:
        return file.read().strip().encode("utf-8")


def server_key(secret: bytes) -> str:
    """The key the two servers prove themselves to each other with. It is derived from
    the token-signing key, so it can't be used as a token and reveals nothing about it."""
    return hmac.new(secret, b"server-to-server", hashlib.sha256).hexdigest()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(username: str, secret: bytes, lifetime_seconds: int) -> tuple:
    """Return a signed token for `username` and the Unix time it expires."""
    expires = int(time.time()) + lifetime_seconds
    body = _encode(json.dumps({"user": username, "expires": expires}).encode("utf-8"))
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}", expires


def read_token(token: object, secret: bytes) -> str:
    """Return the username a token was issued to, or raise TokenError."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise TokenError("Please log in: the request has no valid session token")
    body, signature = token.split(".")
    expected = hmac.new(secret, body.encode("ascii", "replace"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise TokenError("The session token is not valid. Please log in again.")
    try:
        claims = json.loads(_decode(body))
        username, expires = claims["user"], float(claims["expires"])
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError("The session token is not valid. Please log in again.") from exc
    if expires <= time.time():
        raise TokenError("The session has expired. Please log in again.")
    return username


# ---- Command line -------------------------------------------------------------

def main(arguments: list) -> None:
    config = common.load_config()
    if arguments[:1] == ["list"]:
        for username, record in sorted(load_users(config).items()):
            print(f"{username} ({record['role']})")
        return
    if len(arguments) != 3 or arguments[0] != "add" or arguments[2] not in ROLES:
        print(__doc__)
        return
    password = getpass.getpass(f"Password for {arguments[1]}: ")
    if password != getpass.getpass("Type it again: "):
        print("Nothing changed: the passwords did not match.")
        return
    try:
        add_user(config, arguments[1], password, arguments[2])
    except ValueError as exc:
        print(f"Nothing changed: {exc}")
        return
    print(f"Saved {arguments[1].strip().lower()} as {arguments[2]}. Running servers pick this up straight away.")


if __name__ == "__main__":
    main(sys.argv[1:])

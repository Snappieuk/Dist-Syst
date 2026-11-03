"""
Password storage and user accounts.

Nothing here knows about networking, so the security rules can be read and
tested on their own.
"""

import hashlib  # pbkdf2_hmac is the password hashing function
import hmac     # compare_digest compares secrets without leaking timing information
import json     # users are stored as JSON, like the assets
import os       # os.replace() gives an atomic save, as in store.py
import secrets  # cryptographically strong randomness, unlike the random module
import threading  # protects the sessions shared between client threads
import time     # sessions expire after a fixed time
from typing import Dict, Optional

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_VIEWER)

# Stored next to this script, so the same accounts are found from any folder.
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

# WHY SO MANY ITERATIONS: if users.json is ever stolen, an attacker can try
# passwords offline as fast as their hardware allows. Each iteration makes every
# guess more expensive. A login pauses for a moment, which a person barely
# notices, but billions of guesses become impractical. OWASP recommends at least
# 600,000 for PBKDF2 with SHA-256.
PBKDF2_ITERATIONS = 600000

SALT_BYTES = 16

# 32 random bytes cannot realistically be guessed.
TOKEN_BYTES = 32

SESSION_TTL_SECONDS = 3600  # one hour


def hash_password(password: str, salt: Optional[bytes] = None,
                  iterations: int = PBKDF2_ITERATIONS) -> dict:
    """Return a record that can verify `password` without containing it.

    WHY A SALT: a random value mixed into each hash means two users with the same
    password get different hashes, so an attacker cannot spot shared passwords or
    use a precomputed table of common password hashes.

    The iteration count is stored with the hash so it can be raised in the future
    without breaking existing accounts.
    """
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": iterations}


def verify_password(password: str, record: dict) -> bool:
    """Check a password by hashing it the same way and comparing the results."""
    candidate = hash_password(password, bytes.fromhex(record["salt"]), record["iterations"])

    # compare_digest takes the same time wherever the values first differ. An
    # ordinary == stops at the first mismatch, and that timing difference can
    # reveal information to an attacker measuring many attempts.
    return hmac.compare_digest(candidate["hash"], record["hash"])


def load_users(path: str) -> Dict[str, dict]:
    """Read and check the users file. A missing file raises FileNotFoundError."""
    with open(path, "r", encoding="utf-8") as file:
        users = json.load(file)

    required = {"salt", "hash", "iterations", "role"}
    if not isinstance(users, dict) or not all(
        isinstance(user, dict) and required <= user.keys() and user["role"] in ROLES
        for user in users.values()
    ):
        raise ValueError(f"{path} does not contain valid user records")

    return users


def save_users(path: str, users: Dict[str, dict]) -> None:
    """Write the users file atomically, for the reasons explained in store.py."""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(users, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


class UserStore:
    """The accounts the server accepts, loaded once when it starts."""

    def __init__(self, path: str) -> None:
        self._users = load_users(path)

        # A server nobody can log in to is a setup mistake, so report it early.
        if not self._users:
            raise ValueError(f"{path} contains no users")

        # See authenticate() for why this exists.
        self._dummy_record = hash_password(secrets.token_hex(16))

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Return the user's role if the login is correct, otherwise None.

        An unknown username still costs one full password hash. WHY: if unknown
        names were rejected instantly and real ones took noticeably longer, an
        attacker could work out which usernames exist just by timing responses.
        """
        user = self._users.get(username)
        if user is None:
            verify_password(password, self._dummy_record)
            return None

        return user["role"] if verify_password(password, user) else None


class Session:
    """Who a token belongs to, and when it stops being valid."""

    def __init__(self, username: str, role: str, expires: float) -> None:
        self.username = username
        self.role = role
        self.expires = expires


class SessionManager:
    """Issues and checks the tokens clients use after logging in.

    WHY TOKENS: each request uses a new connection, so the server needs a way to
    recognise a user between them. Sending the password every time would mean
    keeping it in client memory and running the slow hash on every request.

    LIMITATION: sessions are held in memory, so users must log in again after a
    server restart. This also means tokens are never written to disk.
    """

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: Dict[str, Session] = {}

        # Client threads create and read sessions at the same time.
        self._lock = threading.Lock()

    def create(self, username: str, role: str) -> str:
        """Start a session and return its token."""
        # secrets, not random: a token acts as a temporary password, so it must
        # be impossible to guess. The random module's output can be predicted.
        token = secrets.token_hex(TOKEN_BYTES)
        now = time.monotonic()

        with self._lock:
            # Clear out old sessions so memory use cannot grow without limit.
            for old_token in [t for t, s in self._sessions.items() if s.expires <= now]:
                del self._sessions[old_token]

            self._sessions[token] = Session(username, role, now + self._ttl_seconds)

        return token

    def lookup(self, token: object) -> Optional[Session]:
        """Return the session for a token, or None if it is not valid now."""
        # The token comes from the network, so it could be any JSON type, and
        # some of those cannot be used as dictionary keys.
        if not isinstance(token, str):
            return None

        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None

            # A limited lifetime reduces the damage if a token is ever stolen.
            if session.expires <= time.monotonic():
                del self._sessions[token]
                return None

            return session

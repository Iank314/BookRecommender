"""SQLite-backed user accounts and login sessions."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "library.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# PBKDF2 cost. High enough to slow brute force, cheap enough for a login request.
_PBKDF2_ROUNDS = 200_000


class UsernameTakenError(Exception):
    """Raised when registering a username that already exists."""


def _resolve_db_path() -> Path:
    env = os.environ.get("BOOKREC_DB_PATH")
    return Path(env) if env else _DEFAULT_DB_PATH


def _hash_password(password: str, salt: str | None = None) -> str:
    """Return a 'salt$hexdigest' string. Generates a salt when none is given."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS
    )
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(_hash_password(password, salt), stored)


class UserStore:
    """Thread-safe SQLite store for user accounts and session tokens."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _resolve_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def create_user(self, username: str, password: str) -> str:
        """Create an account and return its user_id. Raises UsernameTakenError."""
        user_id = secrets.token_hex(16)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO users (user_id, username, password_hash) "
                    "VALUES (?, ?, ?)",
                    (user_id, username, _hash_password(password)),
                )
        except sqlite3.IntegrityError as exc:
            raise UsernameTakenError(username) from exc
        return user_id

    def verify_credentials(self, username: str, password: str) -> str | None:
        """Return the user_id if username/password match, else None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, password_hash FROM users "
                "WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        if row and _verify_password(password, row["password_hash"]):
            return row["user_id"]
        return None

    def get_username(self, user_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row["username"] if row else None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id) VALUES (?, ?)",
                (token, user_id),
            )
        return token

    def user_for_session(self, token: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE token = ?", (token,)
            ).fetchone()
        return row["user_id"] if row else None

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

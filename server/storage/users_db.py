"""SQLite-backed user accounts and login sessions."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from server.storage._base import SQLiteStore

# PBKDF2 cost. High enough to slow brute force, cheap enough for a login request.
_PBKDF2_ROUNDS = 200_000

# Server-side session lifetime. This is the one that matters: a cookie's
# max_age is a request to the browser, not a constraint on us, so without a
# check here a token copied off a machine stays valid forever and "log out
# everywhere" only exists as a side effect of a password reset. Matched to the
# cookie's 1-year max_age (COOKIE_MAX_AGE in app.py) so the two expire
# together — a session the browser has already dropped is dead weight anyway.
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 365


class UsernameTakenError(Exception):
    """Raised when registering a username that already exists."""


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


class UserStore(SQLiteStore):
    """Thread-safe SQLite store for user accounts and session tokens."""

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

    def _migrate(self, conn: sqlite3.Connection) -> None:
        # Migration for DBs created before the admin flag existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )

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

    def set_password(self, username: str, new_password: str) -> bool:
        """Reset a user's password (admin CLI path — scripts/reset_password.py).
        Also revokes every login session for the account, so anyone holding a
        stolen session is logged out when the legitimate owner resets."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ? COLLATE NOCASE",
                (_hash_password(new_password), username),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "DELETE FROM sessions WHERE user_id = "
                "(SELECT user_id FROM users WHERE username = ? COLLATE NOCASE)",
                (username,),
            )
            return True

    # ------------------------------------------------------------------ #
    # Admin — granted via scripts/make_admin.py only, never from the web,
    # so a compromised session can't escalate itself.
    # ------------------------------------------------------------------ #
    def is_admin(self, user_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_admin FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row and row["is_admin"])

    def set_admin(self, username: str, flag: bool) -> bool:
        """Grant/revoke admin by username. False if no such account."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET is_admin = ? WHERE username = ? COLLATE NOCASE",
                (1 if flag else 0, username),
            )
            return cur.rowcount > 0

    def list_accounts(self) -> list[dict]:
        """All accounts (no password hashes), newest first — admin stats only."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, username, created_at, is_admin
                FROM users ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            {"user_id": r["user_id"], "username": r["username"],
             "created_at": r["created_at"], "is_admin": bool(r["is_admin"])}
            for r in rows
        ]

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id) VALUES (?, ?)",
                (token, user_id),
            )
        return token

    def user_for_session(self, token: str, now: int | None = None) -> str | None:
        """Resolve a session token, or None if it's unknown or expired.

        The expiry is enforced here rather than left to the cookie, because a
        token that has escaped the browser it was issued to is exactly the case
        the cookie's max_age cannot cover. Expired rows are left for
        prune_sessions to sweep — deleting on a read path would turn every
        page load into a write.
        """
        cutoff = (now if now is not None else int(time.time())) - SESSION_MAX_AGE_SECONDS
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE token = ? AND created_at >= ?",
                (token, cutoff),
            ).fetchone()
        return row["user_id"] if row else None

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def prune_sessions(self, now: int | None = None) -> int:
        """Delete expired sessions; return the number removed.

        Expiry is already enforced on read, so this only reclaims disk — but
        the table is insert-only apart from logout, and every login adds a row
        that nothing else ever removes.
        """
        cutoff = (now if now is not None else int(time.time())) - SESSION_MAX_AGE_SECONDS
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
            return cur.rowcount

    def count_expired_sessions(self, now: int | None = None) -> int:
        """Expired sessions still on disk — backs the --dry-run preview, using
        the same predicate prune_sessions deletes on so the two can't drift."""
        cutoff = (now if now is not None else int(time.time())) - SESSION_MAX_AGE_SECONDS
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE created_at < ?", (cutoff,)
            ).fetchone()
        return row[0]

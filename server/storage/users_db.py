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

# Guest accounts. A guest is a real users row with no credentials, created so
# somebody can try the library, sections and recommendations without signing
# up. Two things keep that from being a back door into the account system:
# every guest username starts with GUEST_USERNAME_PREFIX (which registration
# refuses, so nobody can mint a lookalike), and the password hash is stored as
# a value _verify_password can never match, so `guest-1a2b` is not an account
# anyone can log into even knowing the name.
GUEST_USERNAME_PREFIX = "guest-"

# How long a guest's data survives. Shorter than a session by design: this is
# scratch data for someone who hasn't committed to an account, and it costs
# real rows in library.db until scripts/prune_guests.py sweeps it. Long enough
# that closing the laptop for a fortnight doesn't lose your shelf.
GUEST_MAX_AGE_SECONDS = 60 * 60 * 24 * 30

# A password hash no password can produce. _hash_password always emits
# "<salt>$<hexdigest>", so a value with no "$" makes _verify_password's split
# raise and return False for every input — including this literal itself.
_UNUSABLE_PASSWORD = "!guest-no-login"


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
        # Migrations for DBs created before the admin and guest flags existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        if "is_guest" not in cols:
            # Defaulting to 0 is the safe direction: every pre-existing row is
            # a real signed-up account, and the guest pruner only ever deletes
            # rows with this set.
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_guest INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_guest_created "
                "ON users(is_guest, created_at)"
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
                # is_guest = 0 as well as the unusable hash: two independent
                # reasons a guest row can't be logged into, so neither one
                # being wrong on its own opens the door.
                "SELECT user_id, password_hash FROM users "
                "WHERE username = ? COLLATE NOCASE AND is_guest = 0",
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
                SELECT user_id, username, created_at, is_admin, is_guest
                FROM users ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            {"user_id": r["user_id"], "username": r["username"],
             "created_at": r["created_at"], "is_admin": bool(r["is_admin"]),
             "is_guest": bool(r["is_guest"])}
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Guests — credential-less accounts for trying the app before signing up.
    # ------------------------------------------------------------------ #
    def create_guest(self) -> tuple[str, str]:
        """Create a guest account and return its (user_id, username).

        The username is generated rather than chosen so it can't collide with
        anything a person would pick, and it carries GUEST_USERNAME_PREFIX so
        every later read can tell what it is without a join.
        """
        for _ in range(5):
            user_id = secrets.token_hex(16)
            username = f"{GUEST_USERNAME_PREFIX}{secrets.token_hex(4)}"
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO users "
                        "(user_id, username, password_hash, is_guest) "
                        "VALUES (?, ?, ?, 1)",
                        (user_id, username, _UNUSABLE_PASSWORD),
                    )
            except sqlite3.IntegrityError:
                # 4 random bytes collide at a rate worth retrying for and not
                # worth widening the name for; the loop is the cheap fix.
                continue
            return user_id, username
        raise RuntimeError("Could not allocate a unique guest username.")

    def is_guest(self, user_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_guest FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row and row["is_guest"])

    def convert_guest(self, user_id: str, username: str, password: str) -> bool:
        """Turn a guest into a real account in place, keeping its user_id.

        Keeping the id is the whole point: every library entry, section and
        feedback row is keyed by user_id, so signing up carries across what the
        guest collected without copying a single row. Returns False if
        `user_id` isn't a guest (already converted, or pruned out from under
        the session); raises UsernameTakenError if the name is spoken for.
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE users SET username = ?, password_hash = ?, "
                    "is_guest = 0, created_at = strftime('%s', 'now') "
                    "WHERE user_id = ? AND is_guest = 1",
                    (username, _hash_password(password), user_id),
                )
                return cur.rowcount > 0
        except sqlite3.IntegrityError as exc:
            raise UsernameTakenError(username) from exc

    def stale_guest_ids(self, now: int | None = None) -> list[str]:
        """Guest user_ids past GUEST_MAX_AGE_SECONDS — what prune_guests sweeps.

        Returned rather than deleted in one step because a guest's books,
        sections and feedback live in other stores' tables; the caller clears
        those first, then calls delete_user. `created_at` is reset on
        conversion, so a guest who signs up on day 29 starts its account life
        fresh and is never in this list.
        """
        cutoff = (now if now is not None else int(time.time())) - GUEST_MAX_AGE_SECONDS
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM users "
                "WHERE is_guest = 1 AND created_at < ?",
                (cutoff,),
            ).fetchall()
        return [r["user_id"] for r in rows]

    def delete_user(self, user_id: str) -> bool:
        """Delete an account and its sessions. Guest cleanup only — there is
        deliberately no web path to this, and no caller for a real account."""
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            return cur.rowcount > 0

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

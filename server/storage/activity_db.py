"""SQLite-backed activity log: one row per tracked request.

Powers the admin stats panel — without this, searches and recommendation runs
leave no trace, so "how many people were on today?" is unanswerable. Events
are deliberately minimal: a kind, an optional user_id (search and similar are
anonymous endpoints — user_id is filled only when a session cookie resolves),
and a timestamp. No queries, titles, or IPs are stored.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "library.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    kind    TEXT NOT NULL,            -- 'search' | 'similar' | 'recommend'
    user_id TEXT,                     -- NULL for anonymous requests
    at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(at);
CREATE INDEX IF NOT EXISTS idx_activity_kind_at ON activity_log(kind, at);
"""


def _resolve_db_path() -> Path:
    env = os.environ.get("BOOKREC_DB_PATH")
    return Path(env) if env else _DEFAULT_DB_PATH


class ActivityStore:
    """Thread-safe append-mostly event log."""

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

    def record(self, kind: str, user_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO activity_log (kind, user_id) VALUES (?, ?)",
                (kind, user_id),
            )

    def prune_before(self, cutoff: int) -> int:
        """Delete events older than the given unix timestamp; return rows removed.

        The table is append-mostly and grows one row per tracked request forever,
        so without periodic pruning it's an unbounded disk leak. Run from the
        `scripts.prune_activity` CLI (cron-friendly). Stats windows are 24h/7d/
        all-time, so a generous retention (e.g. a year) keeps every reported
        figure intact while capping growth.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM activity_log WHERE at < ?", (cutoff,))
            return cur.rowcount

    def count_before(self, cutoff: int) -> int:
        """Count events older than `cutoff` without deleting them.

        Backs the `--dry-run` preview in `scripts.prune_activity` so the number
        shown to the operator is computed with the exact predicate that
        `prune_before` deletes on — the two can't drift apart.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM activity_log WHERE at < ?", (cutoff,)
            ).fetchone()
        return row[0]

    def counts_since(self, since: int) -> dict[str, int]:
        """{kind: count} for events at/after the given unix timestamp."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM activity_log "
                "WHERE at >= ? GROUP BY kind",
                (since,),
            ).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def active_users_since(self, since: int) -> int:
        """Distinct logged-in users with any event at/after the timestamp."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM activity_log "
                "WHERE at >= ? AND user_id IS NOT NULL",
                (since,),
            ).fetchone()
        return row[0]

    def anonymous_events_since(self, since: int) -> int:
        """Events with no session — visitors who searched without an account."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM activity_log "
                "WHERE at >= ? AND user_id IS NULL",
                (since,),
            ).fetchone()
        return row[0]

    def last_seen_by_user(self) -> dict[str, int]:
        """{user_id: most recent event timestamp} for logged-in activity."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, MAX(at) AS last FROM activity_log "
                "WHERE user_id IS NOT NULL GROUP BY user_id"
            ).fetchall()
        return {r["user_id"]: r["last"] for r in rows}

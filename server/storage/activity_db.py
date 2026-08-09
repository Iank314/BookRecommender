"""SQLite-backed activity log: one row per tracked request.

Powers the admin stats panel — without this, searches and recommendation runs
leave no trace, so "how many people were on today?" is unanswerable. Events
are deliberately minimal: a kind, an optional user_id (search and similar are
anonymous endpoints — user_id is filled only when a session cookie resolves),
a timestamp, and — on page views only — the referring *host*. No queries,
titles, paths, or IPs are stored: the referrer is reduced to a bare hostname
before it gets here (see `_normalize_referrer` in server/app.py), because some
sites put the visitor's search terms in the referrer URL.
"""

from __future__ import annotations

import sqlite3

from server.storage._base import SQLiteStore


class ActivityStore(SQLiteStore):
    """Thread-safe append-mostly event log."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS activity_log (
        kind     TEXT NOT NULL,           -- 'search' | 'similar' | 'recommend'
                                          -- | 'visit' | 'crawl'
        user_id  TEXT,                    -- NULL for anonymous requests
        at       INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        referrer TEXT                     -- host only; NULL = direct or n/a
    );
    CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(at);
    CREATE INDEX IF NOT EXISTS idx_activity_kind_at ON activity_log(kind, at);
    """

    def _migrate(self, conn: sqlite3.Connection) -> None:
        # DBs created before referrer capture existed — CREATE TABLE IF NOT
        # EXISTS won't add a column to a table that's already there.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(activity_log)")}
        if "referrer" not in cols:
            conn.execute("ALTER TABLE activity_log ADD COLUMN referrer TEXT")

    def record(
        self,
        kind: str,
        user_id: str | None = None,
        referrer: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO activity_log (kind, user_id, referrer) "
                "VALUES (?, ?, ?)",
                (kind, user_id, referrer),
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

    def referrer_report(
        self, now: int, kind: str = "visit", limit: int = 25
    ) -> dict:
        """Traffic sources, all three windows measured in a single pass.

        Returns {"direct": {window: n}, "sources": [{host, **{window: n}}],
        "referred_total": {window: n}, "sources_total": n}.

        `referred_total` and `sources_total` cover *every* referrer, not just
        the `limit` rows in `sources`, so the caller can say how much it is
        not showing. Ordering by recent activity means a high-volume source
        that has gone quiet drops off the list entirely; without a residual
        the table would read as the complete picture while omitting the
        largest referrer on the site.

        Measuring every window in one query is a correctness requirement, not
        an optimisation. Querying each window separately and taking each one's
        top N lets a host make the 24h list while missing the all-time list,
        which renders as "7 visits today, 0 ever" — arithmetic that cannot
        happen, on precisely the row an operator is staring at when a link
        starts sending traffic. One GROUP BY makes the three counts three
        facets of the same row, so they can't disagree.

        Ordered by the 7-day count first because the question this answers is
        "where is traffic coming from *now*": a source that just started
        sending belongs at the top, not buried under an all-time leader that
        has since gone quiet.

        Scoped to one kind, also for correctness: only page views carry a
        referrer, so counting NULLs across every kind would file every search
        and recommendation run under "direct".
        """
        day, week = now - 86_400, now - 7 * 86_400
        # SQLite scores a comparison as 1/0, so SUM over it counts matches.
        # COALESCE covers the direct query aggregating over zero rows.
        windows = (
            "COALESCE(SUM(at >= ?), 0) AS last_24h, "
            "COALESCE(SUM(at >= ?), 0) AS last_7d, "
            "COUNT(*) AS all_time"
        )
        with self._connect() as conn:
            direct = conn.execute(
                f"SELECT {windows} FROM activity_log "
                "WHERE kind = ? AND referrer IS NULL",
                (day, week, kind),
            ).fetchone()
            referred = conn.execute(
                f"SELECT {windows} FROM activity_log "
                "WHERE kind = ? AND referrer IS NOT NULL",
                (day, week, kind),
            ).fetchone()
            sources_total = conn.execute(
                "SELECT COUNT(DISTINCT referrer) FROM activity_log "
                "WHERE kind = ? AND referrer IS NOT NULL",
                (kind,),
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT referrer AS host, {windows} FROM activity_log "
                "WHERE kind = ? AND referrer IS NOT NULL GROUP BY referrer "
                # Host tie-break keeps the ordering stable for equal counts.
                "ORDER BY last_7d DESC, all_time DESC, host ASC LIMIT ?",
                (day, week, kind, limit),
            ).fetchall()
        keys = ("last_24h", "last_7d", "all_time")
        return {
            "direct": {k: direct[k] for k in keys},
            "referred_total": {k: referred[k] for k in keys},
            "sources_total": sources_total,
            "sources": [
                {"host": r["host"], **{k: r[k] for k in keys}} for r in rows
            ],
        }

    def last_seen_by_user(self) -> dict[str, int]:
        """{user_id: most recent event timestamp} for logged-in activity."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, MAX(at) AS last FROM activity_log "
                "WHERE user_id IS NOT NULL GROUP BY user_id"
            ).fetchall()
        return {r["user_id"]: r["last"] for r in rows}

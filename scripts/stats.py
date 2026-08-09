"""Operator stats: who's using the app, from the live SQLite database.

Read-only — safe to run while the server is up. Points at ./data/library.db
by default (the same file the Docker bind mount uses); override with
BOOKREC_DB_PATH.

Usage:
    python -m scripts.stats
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path(os.environ.get("BOOKREC_DB_PATH")
          or Path(__file__).resolve().parent.parent / "data" / "library.db")

DAY = 86400


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"No database at {DB} — has the app run yet?")
    # mode=ro: never write (or create) anything from the stats script.
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    now = int(time.time())

    def one(sql: str, *params) -> int:
        return conn.execute(sql, params).fetchone()[0]

    print(f"Database: {DB}\n")

    total = one("SELECT COUNT(*) FROM users")
    day = one("SELECT COUNT(*) FROM users WHERE created_at >= ?", now - DAY)
    week = one("SELECT COUNT(*) FROM users WHERE created_at >= ?", now - 7 * DAY)
    print(f"Registered accounts:  {total}   (+{day} last 24h, +{week} last 7d)")

    sessions = one("SELECT COUNT(*) FROM sessions")
    session_users = one("SELECT COUNT(DISTINCT user_id) FROM sessions")
    print(f"Login sessions:       {sessions} across {session_users} users "
          "(1-year cookies — 'ever logged in', not 'online now')")

    libs = one("SELECT COUNT(DISTINCT user_id) FROM library_entries")
    books = one("SELECT COUNT(*) FROM library_entries")
    print(f"Libraries:            {libs} users hold {books} saved books")

    active_day = one(
        "SELECT COUNT(DISTINCT user_id) FROM library_entries WHERE added_at >= ?",
        now - DAY)
    active_week = one(
        "SELECT COUNT(DISTINCT user_id) FROM library_entries WHERE added_at >= ?",
        now - 7 * DAY)
    print(f"Active savers:        {active_day} last 24h, {active_week} last 7d "
          "(users who saved a book — a narrower bar than a page view)")

    # Traffic. Read-only connection, so an activity_log from before referrer
    # capture (or before the table existed at all) can't be migrated here —
    # degrade to a note rather than crashing the whole report.
    try:
        views_day = one("SELECT COUNT(*) FROM activity_log "
                        "WHERE kind = 'visit' AND at >= ?", now - DAY)
        views_week = one("SELECT COUNT(*) FROM activity_log "
                         "WHERE kind = 'visit' AND at >= ?", now - 7 * DAY)
        views_all = one("SELECT COUNT(*) FROM activity_log WHERE kind = 'visit'")
        crawls_week = one("SELECT COUNT(*) FROM activity_log "
                          "WHERE kind = 'crawl' AND at >= ?", now - 7 * DAY)
        print(f"Page views:           {views_day} last 24h, {views_week} last 7d, "
              f"{views_all} all time  ({crawls_week} crawler hits last 7d)")

        direct = one("SELECT COUNT(*) FROM activity_log WHERE kind = 'visit' "
                     "AND referrer IS NULL AND at >= ?", now - 7 * DAY)
        sources = conn.execute(
            "SELECT referrer, COUNT(*) AS n FROM activity_log "
            "WHERE kind = 'visit' AND referrer IS NOT NULL AND at >= ? "
            "GROUP BY referrer ORDER BY n DESC LIMIT 15",
            (now - 7 * DAY,)).fetchall()
        print("\nTraffic sources (last 7d):")
        print(f"  - {'Direct / no referrer':<28} {direct}")
        for r in sources:
            print(f"  - {r['referrer']:<28} {r['n']}")
    except sqlite3.OperationalError:
        print("Page views:           not tracked in this database yet "
              "(pre-dates referrer capture)")

    print("\nNewest accounts:")
    rows = conn.execute(
        """
        SELECT u.username, u.created_at,
               (SELECT COUNT(*) FROM library_entries le WHERE le.user_id = u.user_id) AS books
        FROM users u ORDER BY u.created_at DESC LIMIT 10
        """).fetchall()
    for r in rows:
        age_days = (now - r["created_at"]) / DAY
        when = f"{age_days:.1f}d ago" if age_days >= 1 else f"{(now - r['created_at']) / 3600:.1f}h ago"
        print(f"  - {r['username']:<24} registered {when:<10} | {r['books']} books saved")

    conn.close()


if __name__ == "__main__":
    main()

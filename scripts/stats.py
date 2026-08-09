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

    # Traffic. This duplicates ActivityStore.referrer_report rather than
    # calling it, because constructing a store opens the DB read-write and
    # runs migrations — the one thing this script promises not to do. The cost
    # is that the two can drift on ordering or limit; keep them in step, or
    # trust the admin panel over this when they disagree.
    #
    # Read-only also means an activity_log from before referrer capture (or
    # before the table existed at all) can't be migrated here. Every query runs
    # before anything is printed: printing as we go would let a later failure
    # emit "Page views: 0" and "not tracked yet" together.
    traffic = None
    try:
        traffic = {
            "views": [
                one("SELECT COUNT(*) FROM activity_log "
                    "WHERE kind = 'visit' AND at >= ?", now - DAY),
                one("SELECT COUNT(*) FROM activity_log "
                    "WHERE kind = 'visit' AND at >= ?", now - 7 * DAY),
                one("SELECT COUNT(*) FROM activity_log WHERE kind = 'visit'"),
            ],
            "crawls_week": one("SELECT COUNT(*) FROM activity_log "
                               "WHERE kind = 'crawl' AND at >= ?", now - 7 * DAY),
            "direct": one("SELECT COUNT(*) FROM activity_log WHERE kind = 'visit' "
                          "AND referrer IS NULL AND at >= ?", now - 7 * DAY),
            "sources": conn.execute(
                "SELECT referrer, COUNT(*) AS n FROM activity_log "
                "WHERE kind = 'visit' AND referrer IS NOT NULL AND at >= ? "
                "GROUP BY referrer ORDER BY n DESC, referrer ASC LIMIT 15",
                (now - 7 * DAY,)).fetchall(),
        }
    except sqlite3.OperationalError:
        pass

    if traffic is None:
        print("Page views:           not tracked in this database yet "
              "(pre-dates referrer capture)")
    else:
        day_v, week_v, all_v = traffic["views"]
        print(f"Page views:           {day_v} last 24h, {week_v} last 7d, "
              f"{all_v} all time  ({traffic['crawls_week']} crawler hits last 7d)")
        print("\nTraffic sources (last 7d):")
        print(f"  - {'Direct / no referrer':<28} {traffic['direct']}")
        for r in traffic["sources"]:
            print(f"  - {r['referrer']:<28} {r['n']}")

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

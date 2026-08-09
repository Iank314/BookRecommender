"""Referrer capture: normalising a Referer header down to a host, classifying
crawlers, and aggregating the result for the admin panel.

The point of the feature is answering "did that traffic come from Google or
from Reddit?", so the tests care most about the two ways that question gets a
wrong answer: a source fragmenting across spellings of the same host, and
in-site navigation or crawler sweeps inflating the numbers.
"""

import sqlite3
import time
from pathlib import Path

import pytest

import server.app as app
from server.storage.activity_db import ActivityStore


@pytest.fixture
def activity(tmp_path: Path) -> ActivityStore:
    return ActivityStore(db_path=tmp_path / "activity_test.db")


# ---- normalising the header --------------------------------------------------

def test_no_referrer_is_direct():
    assert app._normalize_referrer(None) is None
    assert app._normalize_referrer("") is None


def test_keeps_only_the_host():
    # Paths and queries never get stored — some sites put the visitor's search
    # terms in the referrer URL.
    got = app._normalize_referrer("https://www.google.com/search?q=books+like+dune")
    assert got == "google.com"


def test_www_is_stripped_so_one_source_is_one_row():
    bare = app._normalize_referrer("https://reddit.com/r/books")
    prefixed = app._normalize_referrer("https://www.reddit.com/r/books")
    assert bare == prefixed == "reddit.com"


def test_host_is_lowercased():
    assert app._normalize_referrer("https://Reddit.COM/r/books") == "reddit.com"


def test_subdomains_are_kept_distinct():
    # old.reddit.com and reddit.com are the same community but different
    # surfaces; collapsing them would hide which one actually sends traffic.
    assert app._normalize_referrer("https://old.reddit.com/r/books") == "old.reddit.com"


def test_self_referral_is_internal_not_direct():
    got = app._normalize_referrer("https://iansbookrecs.com/", "iansbookrecs.com")
    assert got == app.REFERRER_INTERNAL


def test_self_referral_matches_the_canonical_host_too():
    # Behind a proxy the request host may not be what BASE_URL says; either
    # match counts as internal.
    canonical = app.urlparse(app.BASE_URL).hostname
    assert app._normalize_referrer(f"https://{canonical}/books-like") == \
        app.REFERRER_INTERNAL


@pytest.mark.parametrize("junk", [
    "not a url",
    "http://[oops",          # unmatched bracket — urlparse raises on this
    "://",
    "javascript:void(0)",
])
def test_referrer_with_no_host_is_direct(junk):
    # This runs on the request path; a junk header must never 500 a page load.
    assert app._normalize_referrer(junk) is None


@pytest.mark.parametrize("raw", [
    "https://пример.рф/page",              # IDN the client didn't punycode
    "https://host_with_underscores/",
])
def test_unparseable_host_is_not_counted_as_direct(raw):
    # It came from somewhere. Filing it under "direct" is the same conflation
    # the (internal) sentinel exists to prevent.
    assert app._normalize_referrer(raw) == app.REFERRER_UNKNOWN


def test_neither_sentinel_is_forgeable():
    # Parentheses fail the hostname check, so a crafted Referer can't mint an
    # (internal) row — the worst it can do is land in the junk bucket.
    for sentinel in (app.REFERRER_INTERNAL, app.REFERRER_UNKNOWN):
        assert app._normalize_referrer(f"https://{sentinel}/") == \
            app.REFERRER_UNKNOWN


# ---- crawler classification --------------------------------------------------

@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "facebookexternalhit/1.1",
    "curl/8.4.0",
    "python-requests/2.31.0",
    "",
    None,
])
def test_bots_are_recognised(ua):
    assert app._is_bot_ua(ua) is True


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
])
def test_real_browsers_are_not_bots(ua):
    assert app._is_bot_ua(ua) is False


# ---- aggregation -------------------------------------------------------------

# Rows are stamped by SQLite's own clock, so the reference point has to track
# it — a hardcoded constant would put every window on the wrong side of "now"
# and silently zero the counts these tests are asserting on.
NOW = int(time.time())


def _visits(store: ActivityStore, host: str | None, n: int, at: int | None = None):
    """Record n page views from `host`, optionally backdated to `at`.

    Backdating is `at > ?` (strict), so rows already pushed back to exactly
    this timestamp stay put and repeated calls stay idempotent.
    """
    for _ in range(n):
        store.record("visit", None, host)
    if at is not None:
        conn = sqlite3.connect(store.db_path)
        conn.execute("UPDATE activity_log SET at = ? WHERE at > ?", (at, at))
        conn.commit()
        conn.close()


def test_report_splits_direct_from_referred(activity: ActivityStore):
    _visits(activity, None, 2)
    _visits(activity, "google.com", 1)
    out = activity.referrer_report(NOW)
    assert out["direct"]["all_time"] == 2
    assert out["sources"] == [
        {"host": "google.com", "last_24h": 1, "last_7d": 1, "all_time": 1}
    ]


def test_report_orders_by_recent_activity(activity: ActivityStore):
    _visits(activity, "reddit.com", 3)
    _visits(activity, "google.com", 1)
    hosts = [s["host"] for s in activity.referrer_report(NOW)["sources"]]
    assert hosts == ["reddit.com", "google.com"]


def test_report_ignores_other_kinds(activity: ActivityStore):
    # The regression this guards: /search and friends store no referrer, so
    # counting NULLs across every kind would report each one as direct traffic.
    _visits(activity, "google.com", 1)
    activity.record("search", "u1")
    activity.record("recommend", "u1")
    activity.record("similar", None)
    out = activity.referrer_report(NOW)
    assert out["direct"]["all_time"] == 0
    assert [s["host"] for s in out["sources"]] == ["google.com"]


def test_crawler_hits_are_a_separate_kind(activity: ActivityStore):
    _visits(activity, "reddit.com", 1)
    activity.record("crawl", None, None)
    activity.record("crawl", None, None)
    assert activity.counts_since(0) == {"visit": 1, "crawl": 2}
    # Crawls must not land in the human page-view report.
    assert activity.referrer_report(NOW)["direct"]["all_time"] == 0
    assert activity.referrer_report(NOW, kind="crawl")["direct"]["all_time"] == 2


def test_report_windows_cannot_contradict_each_other(activity: ActivityStore):
    """A source outside the all-time top N must not render as "n today, 0 ever".

    The bug this pins: fetching each window with its own LIMIT and merging by
    host let a spiking source miss the all-time list, so the panel showed an
    impossible row — and sorted it to the bottom by the very column it had
    zeroed. Eleven established hosts, then a newcomer that only spiked today.
    """
    for i in range(11):
        _visits(activity, f"host{i:02d}.example", 8, at=NOW - 30 * 86_400)
    _visits(activity, "reddit.com", 7)

    report = activity.referrer_report(NOW, limit=10)
    reddit = next(s for s in report["sources"] if s["host"] == "reddit.com")

    assert reddit["all_time"] >= reddit["last_7d"] >= reddit["last_24h"]
    assert reddit == {"host": "reddit.com", "last_24h": 7, "last_7d": 7,
                      "all_time": 7}
    # ...and it leads the table, because it's the only recent movement.
    assert report["sources"][0]["host"] == "reddit.com"


def test_report_windows_narrow_correctly(activity: ActivityStore):
    _visits(activity, "google.com", 4, at=NOW - 30 * 86_400)  # old
    _visits(activity, "google.com", 2)                        # today
    google = activity.referrer_report(NOW)["sources"][0]
    assert google == {"host": "google.com", "last_24h": 2, "last_7d": 2,
                      "all_time": 6}


def test_report_limit(activity: ActivityStore):
    for i in range(5):
        _visits(activity, f"host{i}.example", 1)
    assert len(activity.referrer_report(NOW, limit=3)["sources"]) == 3


def test_report_on_an_empty_log(activity: ActivityStore):
    # SUM() over zero rows is NULL in SQLite, not 0 — the panel must not
    # render "null" on a fresh deployment.
    assert activity.referrer_report(NOW) == {
        "direct": {"last_24h": 0, "last_7d": 0, "all_time": 0},
        "sources": [],
    }


# ---- route wiring ------------------------------------------------------------
# The helpers above can all be right while nothing is recorded, so these drive
# the real routes. They use a throwaway store: the module-level one points at
# the dev database.

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@pytest.fixture
def client(activity: ActivityStore, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app, "activity_store", activity)
    return TestClient(app.app)


def test_homepage_records_a_visit_with_its_referrer(client, activity):
    res = client.get("/", headers={
        "referer": "https://www.reddit.com/r/booksuggestions/comments/abc/",
        "user-agent": BROWSER_UA,
    })
    assert res.status_code == 200
    assert [s["host"] for s in activity.referrer_report(NOW)["sources"]] == \
        ["reddit.com"]


def test_head_request_is_not_a_visit(client, activity):
    # Uptime monitors poll the homepage on a schedule; counting those would
    # swamp every real number on the panel.
    client.head("/", headers={"user-agent": BROWSER_UA})
    assert activity.counts_since(0) == {}


def test_crawler_is_recorded_separately(client, activity):
    client.get("/", headers={
        "user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1; "
                      "+http://www.google.com/bot.html)",
    })
    assert activity.counts_since(0) == {"crawl": 1}


def test_public_page_records_its_referrer(client, activity):
    # A miss still counts: "Google is sending people to a slug that isn't
    # published" is exactly the kind of thing this is here to surface.
    res = client.get("/books-like/no-such-book-slug-xyz", headers={
        "referer": "https://www.google.com/",
        "user-agent": BROWSER_UA,
    })
    assert res.status_code == 404
    assert [s["host"] for s in activity.referrer_report(NOW)["sources"]] == \
        ["google.com"]


# (The matching case for the JSON endpoints — that a /search row never lands
# in the referrer report — is covered at the store level by
# test_report_ignores_other_kinds. Driving /search here would mean live
# provider calls on every test run.)


def test_a_failing_session_store_does_not_break_the_page(client, monkeypatch):
    """Stats plumbing must never 500 a page that would otherwise render.

    The bug this pins: _soft_user_id() and _referrer_host() are passed as
    *arguments* to _record_activity, so they are evaluated before the call and
    outside its try/except. _soft_user_id does a SQLite read, and "database is
    locked" under concurrent writes is a live failure mode — one that now
    reaches the app shell and every public page rather than a JSON endpoint.
    """
    calls = []

    class ExplodingUserStore:
        def user_for_session(self, token):
            calls.append(token)
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app, "user_store", ExplodingUserStore())
    client.cookies.set("bookrec_session", "some-token")
    res = client.get("/", headers={"user-agent": BROWSER_UA})

    assert res.status_code == 200
    # Without this the test passes vacuously: no cookie means no lookup, and
    # the raising branch is never reached.
    assert calls == ["some-token"]


def test_a_malformed_host_header_does_not_break_the_page(client, activity):
    # Starlette rebuilds the URL from the Host header, so request.url.hostname
    # raises ValueError on an unmatched bracket. Caddy shields production by
    # matching hostnames first, but the tunnel and direct-to-uvicorn paths
    # don't. The visit should still be recorded, just without self-referral
    # detection.
    res = client.get("/", headers={"host": "[oops", "user-agent": BROWSER_UA,
                                   "referer": "https://news.ycombinator.com/"})
    assert res.status_code == 200
    assert [s["host"] for s in activity.referrer_report(NOW)["sources"]] == \
        ["news.ycombinator.com"]


def test_referrer_column_migration(tmp_path: Path):
    # An activity_log written before referrer capture must gain the column
    # without losing the events already in it.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE activity_log (
            kind    TEXT NOT NULL,
            user_id TEXT,
            at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        )
        """
    )
    conn.execute("INSERT INTO activity_log (kind, user_id) VALUES ('search', 'u1')")
    conn.commit()
    conn.close()

    store = ActivityStore(db_path=db)
    assert store.counts_since(0) == {"search": 1}
    store.record("visit", None, "google.com")
    assert [s["host"] for s in store.referrer_report(NOW)["sources"]] == \
        ["google.com"]

"""Referrer capture: normalising a Referer header down to a host, classifying
crawlers, and aggregating the result for the admin panel.

The point of the feature is answering "did that traffic come from Google or
from Reddit?", so the tests care most about the two ways that question gets a
wrong answer: a source fragmenting across spellings of the same host, and
in-site navigation or crawler sweeps inflating the numbers.
"""

import sqlite3
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


def test_internal_sentinel_cannot_collide_with_a_real_host():
    # Parentheses are not legal in a hostname, which is why the sentinel uses
    # them — no referrer can ever normalise onto it.
    assert app._normalize_referrer(f"https://{app.REFERRER_INTERNAL}/") != \
        app.REFERRER_INTERNAL


@pytest.mark.parametrize("junk", [
    "not a url",
    "http://[oops",          # unmatched bracket — urlparse raises on this
    "://",
    "javascript:void(0)",
    "https://host_with_underscores/",
])
def test_malformed_referrer_degrades_to_direct(junk):
    # This runs on the request path; a junk header must never 500 a page load.
    assert app._normalize_referrer(junk) is None


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

def test_breakdown_splits_direct_from_referred(activity: ActivityStore):
    activity.record("visit", None, None)
    activity.record("visit", None, None)
    activity.record("visit", None, "google.com")
    out = activity.referrer_breakdown_since(0)
    assert out["direct"] == 2
    assert out["top"] == [{"host": "google.com", "count": 1}]


def test_breakdown_orders_by_count(activity: ActivityStore):
    for _ in range(3):
        activity.record("visit", None, "reddit.com")
    activity.record("visit", None, "google.com")
    hosts = [r["host"] for r in activity.referrer_breakdown_since(0)["top"]]
    assert hosts == ["reddit.com", "google.com"]


def test_breakdown_ignores_other_kinds(activity: ActivityStore):
    # The regression this guards: /search and friends store no referrer, so
    # counting NULLs across every kind would report each one as direct traffic.
    activity.record("visit", None, "google.com")
    activity.record("search", "u1")
    activity.record("recommend", "u1")
    activity.record("similar", None)
    out = activity.referrer_breakdown_since(0)
    assert out["direct"] == 0
    assert out["top"] == [{"host": "google.com", "count": 1}]


def test_crawler_hits_are_a_separate_kind(activity: ActivityStore):
    activity.record("visit", None, "reddit.com")
    activity.record("crawl", None, None)
    activity.record("crawl", None, None)
    assert activity.counts_since(0) == {"visit": 1, "crawl": 2}
    # Crawls must not land in the human page-view breakdown.
    assert activity.referrer_breakdown_since(0)["direct"] == 0
    assert activity.referrer_breakdown_since(0, kind="crawl")["direct"] == 2


def test_breakdown_respects_the_window(activity: ActivityStore):
    activity.record("visit", None, "google.com")
    future = 2_000_000_000_000
    assert activity.referrer_breakdown_since(future) == {"direct": 0, "top": []}


def test_breakdown_limit(activity: ActivityStore):
    for i in range(5):
        activity.record("visit", None, f"host{i}.example")
    assert len(activity.referrer_breakdown_since(0, limit=3)["top"]) == 3


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
    assert activity.referrer_breakdown_since(0)["top"] == [
        {"host": "reddit.com", "count": 1}
    ]


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
    assert activity.referrer_breakdown_since(0)["top"] == [
        {"host": "google.com", "count": 1}
    ]


# (The matching case for the JSON endpoints — that a /search row never lands
# in the referrer breakdown — is covered at the store level by
# test_breakdown_ignores_other_kinds. Driving /search here would mean live
# provider calls on every test run.)


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
    assert store.referrer_breakdown_since(0)["top"] == [
        {"host": "google.com", "count": 1}
    ]

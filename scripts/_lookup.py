"""Resolve a book title to the best provider record.

Both offline tools need this and need it to behave identically: the explain
tool tunes scoring against whatever source it picks, and the SEO generator
publishes pages built from that same pick. If they resolved titles differently,
`explain_similar "Mistborn"` would be debugging a different book than the one
on /books-like/mistborn.

Lives in scripts/ rather than server/ because it depends on server.app's
scorer — importing it from inside the fetcher package would be a cycle.
"""

from __future__ import annotations

import re

from server.app import _score_title
from server.fetcher.fetcher import GOOGLE_ENDPOINT, OPENLIB_ENDPOINT, Fetcher
from server.models.book import Books


def _author_matches(book: Books, wanted: str) -> bool:
    """Whether `book` is by the named author, matched on surname.

    Surname only, because providers disagree on everything else: "J.K.
    Rowling" / "J. K. Rowling" / "Rowling, J. K." are the same person, and
    initials are where the formatting differs. Collisions on a bare surname
    are possible but harmless here — the title still has to match too.
    """
    surname = re.sub(r"[^a-z]", "", wanted.lower().split()[-1])
    if not surname:
        return False
    return any(surname in re.sub(r"[^a-z]", "", a.lower()) for a in book.authors)


def find_source(
    title: str, author: str | None = None, *, quiet: bool = False
) -> Books | None:
    """Best title match across both providers, preferring richer records —
    a source with tags and a real description gives the scorer more to work
    with, mirroring what a user clicking "Find Similar" on a result sees.

    `author` disambiguates. Plenty of famous titles are not unique: searching
    "The Fifth Season" finds a Vermont detective novel before Jemisin's, and a
    public page built on the wrong book is worse than no page. When an author
    is given, records that don't match it are rejected outright rather than
    ranked lower — returning None so the caller skips is the safe failure.
    """
    best, best_score = None, 0.0
    query_lower = title.lower().strip()
    for endpoint in (OPENLIB_ENDPOINT, GOOGLE_ENDPOINT):
        try:
            if endpoint == OPENLIB_ENDPOINT:
                books, _ = Fetcher(source=endpoint).fetch_page(
                    title, batch_size=40, category="title")
            else:
                books, _ = Fetcher(source=endpoint).fetch_google_page(
                    title, max_results=20, category="title")
        except Exception as exc:
            if not quiet:
                print(f"  (provider {endpoint} failed: {exc})")
            continue
        for b in books:
            score = _score_title(b, query_lower)
            if score <= 0:
                continue
            if author and not _author_matches(b, author):
                continue
            # Tiebreak toward records with tags + a real description.
            score += min(len(b.tags), 5) + min(len(b.description) / 500.0, 2.0)
            if score > best_score:
                best, best_score = b, score
    return best

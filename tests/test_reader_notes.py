"""Tests for reader-note sanitization of Open Library descriptions.

Open Library's `description` and `first_sentence` fields are community-editable.
Someone pasted a signed review into The Long Earth's work description — "Terry
Pratchett, other than lending his name to this book, wasn't a part of it. ...
just not to my liking.  gmb 3/15/20" — and the enrichment path adopted it as
the book's blurb, so it showed up (and got scored) as a recommendation's
description. _looks_like_reader_note detects that class of text and both OL
ingestion points (Fetcher._from_openlib_doc, Fetcher.fetch_work_detail) drop it.
"""

import server.fetcher.fetcher as fetcher
from server.fetcher.fetcher import Fetcher, _looks_like_reader_note

# The exact text from Open Library that triggered the bug.
LONG_EARTH_NOTE = (
    "Terry Pratchett, other than lending his name to this book, wasn't a part "
    "of it.  No humor and dark reading.  Mr. Baxter should have published it "
    "under his own name, he can write, just not to my liking.    gmb 3/15/20"
)


# ---- _looks_like_reader_note: positives --------------------------------------

def test_the_reported_long_earth_note_is_flagged():
    assert _looks_like_reader_note(LONG_EARTH_NOTE) is True


def test_trailing_signature_variants_flagged():
    for note in [
        "A decent read overall. gmb 3/15/20",
        "Loved the ending.  jd 12-1-99",
        "Skip it.   3/15/2020",
        "Not worth the hype. abc 1.2.2021",
    ]:
        assert _looks_like_reader_note(note) is True, note


def test_unsigned_opinion_flagged():
    assert _looks_like_reader_note(
        "Well written but just not for me, honestly.") is True
    assert _looks_like_reader_note(
        "I couldn't finish this one; too slow.") is True


# ---- _looks_like_reader_note: negatives (must not eat real blurbs) -----------

def test_real_blurb_not_flagged():
    blurb = (
        "In a world of magic and steel, a young orphan discovers she can bend "
        "the fabric of reality itself, and must master her power before a "
        "shadow empire claims the last free city."
    )
    assert _looks_like_reader_note(blurb) is False


def test_publication_date_sentence_not_flagged():
    # "First published in 1954." ends in a lone year, not an m/d/y signature.
    assert _looks_like_reader_note(
        "An epic of the Third Age. First published in 1954.") is False


def test_year_range_and_event_dates_not_flagged():
    # A year range and a bare "9/11" (two components, not a full date) must not
    # trip the signature regex.
    assert _looks_like_reader_note(
        "A sweeping history of the war, 1939-1945.") is False
    assert _looks_like_reader_note(
        "A gripping account of the events of 9/11 and their aftermath.") is False


def test_empty_is_not_a_note():
    assert _looks_like_reader_note("") is False
    assert _looks_like_reader_note("   ") is False


# ---- ingestion point 1: _from_openlib_doc (first_sentence) -------------------

def test_from_openlib_doc_drops_reader_note_first_sentence():
    doc = {
        "key": "/works/OL16769202W",
        "title": "The Long Earth",
        "author_name": ["Terry Pratchett", "Stephen Baxter"],
        "first_sentence": LONG_EARTH_NOTE,
        "subject": ["Science Fiction", "Time travel"],
        "first_publish_year": 2012,
    }
    book = Fetcher._from_openlib_doc(doc)
    assert "to my liking" not in book.description
    assert "gmb" not in book.description
    # It falls back to the synthesized-from-fields description instead.
    assert "Science Fiction" in book.description


# ---- ingestion point 2: fetch_work_detail (work description) -----------------

def test_fetch_work_detail_drops_reader_note_description(monkeypatch):
    def fake_get_json(url, params, **kwargs):
        return {"description": LONG_EARTH_NOTE,
                "subjects": ["Science Fiction", "Fiction", "Time travel"]}

    monkeypatch.setattr(fetcher, "_get_json", fake_get_json)
    desc, subjects = Fetcher(source=fetcher.OPENLIB_ENDPOINT).fetch_work_detail(
        "/works/OL16769202W")
    assert desc == ""                       # the note is dropped
    assert subjects == ["Science Fiction", "Fiction", "Time travel"]  # tags kept


def test_fetch_work_detail_keeps_a_real_blurb(monkeypatch):
    real = ("Joshua and Lobsang venture across a chain of parallel Earths in "
            "this genre-bending science fiction adventure.")

    def fake_get_json(url, params, **kwargs):
        return {"description": {"value": real}, "subjects": ["Science Fiction"]}

    monkeypatch.setattr(fetcher, "_get_json", fake_get_json)
    desc, subjects = Fetcher(source=fetcher.OPENLIB_ENDPOINT).fetch_work_detail(
        "/works/OL16769202W")
    assert desc == real
    assert subjects == ["Science Fiction"]

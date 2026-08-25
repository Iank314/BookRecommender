"""Tests for the published-year search filter and cover-thumbnail mapping."""

from server.app import _publish_year, _year_in_range
from server.fetcher.fetcher import Fetcher
from server.models.book import Books


def _bk(metadata: dict) -> Books:
    return Books(id="t", title="T", authors=[], description="",
                 tags=[], metadata=metadata)


# ---- _publish_year -----------------------------------------------------------

def test_year_from_ol_int():
    assert _publish_year(_bk({"publish_year": 1954})) == 1954


def test_year_from_gb_date_string():
    assert _publish_year(_bk({"publishedDate": "2005-03-01"})) == 2005
    assert _publish_year(_bk({"publishedDate": "1999"})) == 1999


def test_year_unknown():
    assert _publish_year(_bk({})) is None
    assert _publish_year(_bk({"publishedDate": "n.d."})) is None


# ---- _year_in_range ----------------------------------------------------------

def test_no_filter_passes_everything():
    assert _year_in_range(_bk({}), None, None) is True


def test_range_bounds_inclusive():
    book = _bk({"publish_year": 1990})
    assert _year_in_range(book, 1990, 2000) is True
    assert _year_in_range(book, 1991, 2000) is False
    assert _year_in_range(book, 1980, 1990) is True
    assert _year_in_range(book, 1980, 1989) is False


def test_open_ended_bounds():
    book = _bk({"publish_year": 2020})
    assert _year_in_range(book, 2000, None) is True
    assert _year_in_range(book, None, 2010) is False


def test_unknown_year_excluded_when_filtering():
    # The point of the filter is curation — a book that can't prove its year
    # doesn't make the cut.
    assert _year_in_range(_bk({}), 1990, 2000) is False


# ---- cover thumbnails --------------------------------------------------------

def test_google_thumbnail_forced_https():
    item = {"id": "x", "volumeInfo": {
        "title": "T",
        "imageLinks": {"thumbnail": "http://books.google.com/cover.jpg"},
    }}
    book = Fetcher._from_google_item(item)
    assert book.metadata["thumbnail"] == "https://books.google.com/cover.jpg"


def test_google_no_imagelinks_is_none():
    book = Fetcher._from_google_item({"id": "x", "volumeInfo": {"title": "T"}})
    assert book.metadata["thumbnail"] is None


def test_openlibrary_cover_url_from_cover_i():
    book = Fetcher._from_openlib_doc({"key": "/works/OL1W", "title": "T",
                                      "cover_i": 12345})
    assert book.metadata["thumbnail"] == "https://covers.openlibrary.org/b/id/12345-M.jpg"


def test_openlibrary_no_cover_is_none():
    book = Fetcher._from_openlib_doc({"key": "/works/OL1W", "title": "T"})
    assert book.metadata["thumbnail"] is None


# ---- search result quality tie-break -----------------------------------------
# Every exact title match scores 100, and the sort is stable, so the order
# among them was insertion order. Google Books is fetched first, so an
# authorless GB stub titled "The Hunger Games" outranked Suzanne Collins --
# and /similar takes whatever /search ranked first as its source, which sent
# the novel's recommendations into books about Napoleon.

from server.app import _record_quality, _score_book  # noqa: E402


def _rec(title="The Hunger Games", authors=(), tags=(), desc="",
         ratings=0, want=0) -> Books:
    return Books(id="x", title=title, authors=list(authors), description=desc,
                 tags=list(tags),
                 metadata={"ratings_count": ratings, "want_to_read_count": want,
                           "edition_count": 1, "already_read_count": 0})


NOVEL = _rec(authors=["Suzanne Collins"],
             tags=["Dystopian", "Young adult fiction"],
             desc="Katniss volunteers in place of her sister and is sent into "
                  "an arena where children fight to the death on live television.",
             ratings=9000, want=20000)
AUTHORLESS = _rec(tags=["Contests"], desc="A book.")
FILM_TIE_IN = _rec(authors=["Kate Egan"],
                   tags=["Motion pictures", "Film adaptations"], desc="A book.")
STUDY_GUIDE = _rec(authors=["Spark Publishing"],
                   tags=["Criticism and interpretation", "Study guides"],
                   desc="A book.")


def test_the_real_book_outranks_its_lookalikes():
    for other in (AUTHORLESS, FILM_TIE_IN, STUDY_GUIDE):
        assert _record_quality(NOVEL) > _record_quality(other)


def test_an_authorless_record_is_penalised():
    with_author = _rec(authors=["Suzanne Collins"], tags=["Contests"], desc="A book.")
    assert _record_quality(with_author) > _record_quality(AUTHORLESS)


def test_editions_about_a_book_rank_below_it():
    # A study guide and a film tie-in both have authors and tags; what marks
    # them is being *about* the work.
    plain = _rec(authors=["Someone"], tags=["Dystopian"], desc="A book.")
    assert _record_quality(plain) > _record_quality(STUDY_GUIDE)
    assert _record_quality(plain) > _record_quality(FILM_TIE_IN)


def test_quality_never_overrides_relevance():
    """It is a tie-break only -- a great record for the wrong book must lose.

    The sort key is (relevance, quality), so this holds by construction; the
    test pins it because reversing those would silently rerank every search.
    """
    q = "the hunger games"
    wrong_book = _rec(title="Gardening Basics", authors=["A Gardener"],
                      tags=["Gardening"], desc="How to grow vegetables well.",
                      ratings=5000, want=9000)
    assert _score_book(NOVEL, q, "general") > _score_book(wrong_book, q, "general")
    assert _record_quality(wrong_book) > _record_quality(AUTHORLESS)
    # ...yet sorted by (relevance, quality) the right book still wins.
    ranked = sorted([NOVEL, wrong_book],
                    key=lambda b: (_score_book(b, q, "general"), _record_quality(b)),
                    reverse=True)
    assert ranked[0] is NOVEL

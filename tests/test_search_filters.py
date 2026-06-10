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

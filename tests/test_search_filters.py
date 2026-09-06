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


# ---- cross-provider duplicate merging ----------------------------------------
# Regression: the two catalogues describe the same book with disjoint
# strengths -- Google Books has the blurb, Open Library has the readership --
# and the dedup dropped whichever arrived second. Google Books is fetched
# first, so the half always lost was the popularity half, which is the very
# signal _record_quality uses to tell a novel from the books about it.
#
# Measured live before the fix: "The Hobbit" returned a stage play first,
# because Tolkien's Open Library record (481 editions, 3901 want-to-read) was
# fetched, collided on `title||author`, and was discarded. Same for Circe
# (Madeline Miller ranked 6th), Dune and The Hunger Games.

from server.app import (  # noqa: E402
    _book_popularity, _dedup_key, _genre_atoms, _merge_duplicate, _real_genres,
)


def _gb(title="The Hobbit", authors=("J.R.R. Tolkien",), tags=("Fiction",),
        desc="A hobbit leaves home.") -> Books:
    return Books(id="gb_1", title=title, authors=list(authors), description=desc,
                 tags=list(tags), metadata={"pageCount": 300, "language": "en",
                                            "source": "google_books"})


def _ol(title="The Hobbit", authors=("J.R.R. Tolkien",),
        tags=("Fantasy fiction",), desc="") -> Books:
    return Books(id="ol_/works/OL1W", title=title, authors=list(authors),
                 description=desc, tags=list(tags),
                 metadata={"edition_count": 481, "want_to_read_count": 3901,
                           "ratings_count": 100, "source": "open_library"})


def test_the_two_providers_collide_on_the_same_dedup_key():
    """The premise: casing differences do not save the duplicate."""
    assert _dedup_key(_gb(authors=["J.r.r. Tolkien"])) == _dedup_key(_ol())


def test_merging_recovers_the_popularity_the_dedup_used_to_discard():
    keep = _gb()
    assert _book_popularity(keep) == 0.0        # GB carries no readership
    assert _merge_duplicate(keep, _ol()) is True
    assert keep.metadata["edition_count"] == 481
    assert keep.metadata["want_to_read_count"] == 3901
    assert _book_popularity(keep) > 0.8


def test_merging_recovers_a_real_genre_from_the_other_catalogue():
    keep = _gb(tags=["Fiction"])                # generic only -> no genre credit
    assert not _real_genres(set(_genre_atoms(keep.tags)[0]))
    _merge_duplicate(keep, _ol(tags=["Fantasy fiction"]))
    assert _real_genres(set(_genre_atoms(keep.tags)[0])) == {"fantasy"}


def test_merging_never_overwrites_what_a_provider_actually_said():
    keep = _gb(desc="A long and genuine publisher blurb about the novel.")
    keep.metadata["edition_count"] = 7          # already known -- must survive
    _merge_duplicate(keep, _ol(desc="stub"))
    assert keep.metadata["edition_count"] == 7
    assert keep.description.startswith("A long and genuine")


def test_a_longer_description_from_either_side_wins():
    keep = _gb(desc="short")
    _merge_duplicate(keep, _ol(desc="a considerably longer real description"))
    assert keep.description == "a considerably longer real description"


def test_merging_lets_the_novel_beat_an_adaptation_that_shares_its_title():
    """The end-to-end shape of the live bug, on _record_quality alone."""
    play = Books(id="gb_2", title="The Hobbit", authors=["Ruth Perry"],
                 description="A dramatisation in two acts for young players.",
                 tags=["Drama"], metadata={"source": "google_books"})
    novel = _gb()
    assert _record_quality(play) > _record_quality(novel)   # the bug
    _merge_duplicate(novel, _ol())
    assert _record_quality(novel) > _record_quality(play)   # the fix


# ---- merge safety ------------------------------------------------------------
# Three ways the merge could hurt production, found by fuzzing it directly.

from server.app import _MERGED_TAG_CAP, SimilarRequest  # noqa: E402


def test_a_null_description_from_google_books_does_not_crash_the_search():
    """Google Books returns `"description": null` for some volumes and
    _from_google_item passes it straight through, so a bare len() here would
    500 the entire search rather than skip one record."""
    keep = _gb(desc=None)
    assert _merge_duplicate(keep, _ol(desc="a real blurb")) is True
    assert keep.description == "a real blurb"
    assert _merge_duplicate(_gb(), _ol(desc=None)) in (True, False)  # must not raise


def test_a_non_string_tag_does_not_crash_the_search():
    # Open Library subjects are community-edited and reach _from_openlib_doc
    # without coercion, so a non-string can arrive here.
    assert _merge_duplicate(_gb(tags=[7]), _ol(tags=["Fiction"])) in (True, False)
    assert _merge_duplicate(_gb(), _ol(tags=[123, None])) in (True, False)


def test_tag_union_is_bounded():
    """Unbounded, this breaks three endpoints rather than merely bloating.

    One dedup key can absorb many duplicates across Google Books' 120 records
    and Open Library's batches. SimilarRequest, SaveBookRequest and
    FeedbackRequest all cap `tags` at 50, and the frontend posts a search
    result straight back to them -- so an over-tagged record would 422 Find
    Similar, Save and thumbs-up on exactly the most popular books.
    """
    keep = _gb(tags=["Fiction"])
    for i in range(500):
        _merge_duplicate(keep, _ol(tags=[f"Subject {i}", f"Other {i}"]))
    assert len(keep.tags) <= _MERGED_TAG_CAP
    assert _MERGED_TAG_CAP < 50
    SimilarRequest(title="T", tags=keep.tags)  # must validate, not raise


def test_merging_is_idempotent():
    """Re-merging the same duplicate must be a no-op, or repeated passes drift."""
    keep, dup = _gb(desc="short"), _ol(desc="a longer real description")
    assert _merge_duplicate(keep, dup) is True
    snapshot = (list(keep.tags), keep.description, dict(keep.metadata))
    assert _merge_duplicate(keep, dup) is False
    assert (list(keep.tags), keep.description, dict(keep.metadata)) == snapshot


# ---- series-author consensus -------------------------------------------------
# When many records share a title, the author appearing across most of them is
# almost always the one who wrote it: catalogues file every edition and volume
# under the real author, while adaptations and study guides appear once each.
#
# Measured over 9 ambiguous titles with the merge in place: 7/9 correct #1 at
# bonus 0.0, 8/9 at 0.5, 9/9 at both 1.0 and 2.0, nothing regressing anywhere.

from server.app import (  # noqa: E402
    SERIES_CONSENSUS_BONUS, _consensus_bonus, _series_author_consensus,
)


def _e(title, author):
    return [Books(id=title + author, title=title, authors=[author], description="d",
                  tags=[], metadata={}), 100.0]


def test_consensus_elects_the_author_owning_most_records():
    entries = [_e("The Hobbit", "J. R. R. Tolkien"),
               _e("The Hobbit", "J.r.r. Tolkien"),
               _e("The Hobbit", "Ruth Perry")]
    assert _series_author_consensus(entries) == {"the hobbit": "tolkien"}


def test_grouping_is_by_series_not_exact_title():
    """The reason this works where exact-title grouping failed.

    Guiltythree's records are titled "Shadow Slave" and "Shadow Slave, Book
    1/2/3" -- four one-record groups under exact-title matching, so no
    consensus at all. _split_series folds them into one.
    """
    entries = [_e("Shadow Slave", "Guiltythree"),
               _e("Shadow Slave, Book 1", "Guiltythree"),
               _e("Shadow Slave, Book 2", "Guiltythree"),
               _e("SHADOW SLAVE", "D. I. Telbat")]
    assert _series_author_consensus(entries) == {"shadow slave": "guiltythree"}


def test_one_record_is_not_a_consensus():
    assert _series_author_consensus([_e("Solo", "Only Author")]) == {}


def test_a_tie_is_not_evidence():
    entries = [_e("Twin", "Alice Ackroyd"), _e("Twin", "Bob Bishop")]
    assert _series_author_consensus(entries) == {}


def test_the_bonus_reaches_only_the_owning_author():
    cons = {"the hobbit": "tolkien"}
    tolkien = Books(id="a", title="The Hobbit", authors=["J. R. R. Tolkien"],
                    description="", tags=[], metadata={})
    perry = Books(id="b", title="The Hobbit", authors=["Ruth Perry"],
                  description="", tags=[], metadata={})
    assert _consensus_bonus(tolkien, cons) == SERIES_CONSENSUS_BONUS
    assert _consensus_bonus(perry, cons) == 0.0
    assert _consensus_bonus(tolkien, {}) == 0.0


def test_consensus_survives_records_with_no_usable_author():
    entries = [_e("Ghost", "A"),                       # initial only, too short
               Books(id="x", title="Ghost", authors=[], description="", tags=[],
                     metadata={}) and [Books(id="x", title="Ghost", authors=[],
                                             description="", tags=[], metadata={}), 100.0],
               _e("Ghost", "Real Author"), _e("Ghost", "Real Author")]
    assert _series_author_consensus(entries) == {"ghost": "author"}


def test_a_null_author_does_not_crash_consensus():
    """Providers occasionally emit a null in the author list. _dedup_key would
    fail first today, but this is a new call site and the guard is free."""
    null_author = [Books(id="n", title="Ghost", authors=[None], description="",
                         tags=[], metadata={}), 100.0]
    assert _series_author_consensus([null_author, _e("Ghost", "Real Author"),
                                     _e("Ghost", "Real Author")]) == {"ghost": "author"}
    assert _consensus_bonus(null_author[0], {"ghost": "author"}) == 0.0


def test_catalogue_and_plain_author_forms_are_the_same_person():
    """Open Library files "Rowling, J. K."; Google Books says "J. K. Rowling".
    Counting them separately would split the plurality they should form."""
    entries = [_e("T", "Rowling, J. K."), _e("T", "J. K. Rowling"), _e("T", "Someone Else")]
    assert _series_author_consensus(entries) == {"t": "rowling"}

"""Tests for the "is this candidate worth recommending at all" filters.

Regression context: generating 124 public recommendation pages exposed that
Find Similar was returning tie-in merchandise and topically-related nonfiction
for some of the most popular books in the catalogue. Harry Potter recommended
a lenticular poster book, a Hogwarts journal and a biography of J. K. Rowling;
The Hunger Games recommended "Human starvation and its consequences" and four
more famine studies; Warbreaker recommended occult how-to manuals.

Three distinct causes, one test module:
  1. `_is_about_source` tokenised titles with str.split(), so punctuation kept
     "(harry" and "potter!" from ever matching, and its 2-word threshold could
     never catch a one-word title like "J.K. Rowling".
  2. Nothing recognised merchandise formats at all.
  3. Candidate pools were fetched using Open Library subject facets — "severe
     poverty", "military education" — which are catalogue metadata, not genres.
"""

from server.app import (
    _infer_genre_from_content,
    _author_surname,
    _is_about_source,
    _is_merchandise,
    _similar_genre_queries,
    _title_words,
)
from server.models.book import Books


def _bk(title: str, authors: list[str] | None = None) -> Books:
    return Books(id="c", title=title, authors=authors or ["Someone Else"],
                 description="", tags=[], metadata={})


# ------------------------------------------------------------- tokenising

def test_title_words_strips_punctuation():
    # str.split() left "(harry" and "potter)" glued to their brackets, so the
    # merch filter never matched them.
    assert _title_words("Lenticular Poster Book (Harry Potter)") == {
        "lenticular", "poster", "book", "harry", "potter",
    }
    assert "potter" in _title_words("We love Harry Potter!")


def test_title_words_keeps_internal_apostrophes():
    assert "sorcerer's" in _title_words("Harry Potter and the Sorcerer's Stone")


# --------------------------------------------------------------- surnames

def test_author_surname_handles_both_provider_formats():
    # Providers use display order and catalogue order interchangeably; taking
    # the last word unconditionally turns "Rowling, J. K." into "k".
    assert _author_surname("J. K. Rowling") == "rowling"
    assert _author_surname("Rowling, J. K.") == "rowling"
    assert _author_surname("Sanderson, Brandon") == "sanderson"
    assert _author_surname("") == ""


# ---------------------------------------------------------- merchandise

def test_merchandise_formats_are_rejected():
    for title in [
        "Lenticular Poster Book (Harry Potter)",
        "The Official Harry Potter Coloring Book",
        "Harry Potter 2024 Calendar",
        "SparkNotes: Frankenstein",
        "Frankenstein: A Study Guide",
        "A Summary of Sapiens",
        "Behind the Scenes of the Movie",
    ]:
        assert _is_merchandise(title) is True, title


def test_ordinary_novels_are_not_merchandise():
    for title in [
        "The Fellowship of the Ring",
        "Mistborn: The Final Empire",
        "A Game of Thrones",
        "The Hunger Games",
        "Station Eleven",
    ]:
        assert _is_merchandise(title) is False, title


def test_real_novels_whose_titles_look_like_merchandise_survive():
    # The filter is a blacklist, so every word on it is a bet that no novel is
    # called that. These are the bets that were wrong and got reverted — keep
    # them here so re-adding a tempting word fails loudly instead of quietly
    # blacklisting a bestseller.
    for title in [
        "The Notebook",                      # Nicholas Sparks
        "The Art of Racing in the Rain",     # Garth Stein
        "The Art of War",                    # Sun Tzu
        "Journal of a Solitude",             # May Sarton
        "The Puzzle Box",
        "Trivia Night",
    ]:
        assert _is_merchandise(title) is False, title


# ------------------------------------------------------- about-the-source

def test_tie_in_titles_are_filtered():
    filter_words = {"harry", "potter", "sorcerer's", "stone"}
    assert _is_about_source(_bk("We love Harry Potter!"), filter_words) is True
    assert _is_about_source(
        _bk("Lenticular Poster Book (Harry Potter)"), filter_words) is True


def test_author_biographies_are_filtered_on_one_word():
    # "J.K. Rowling" by Colleen A. Sexton — a single surname match is enough,
    # because a novel almost never carries its own author's surname as a title.
    assert _is_about_source(
        _bk("J.K. Rowling", ["Colleen A. Sexton"]),
        filter_words={"harry", "potter"}, author_surnames={"rowling"},
    ) is True


def test_a_book_by_that_author_is_not_a_biography_of_them():
    # The same-surname rule must not eat the author's own work.
    assert _is_about_source(
        _bk("Rowling's Casual Vacancy", ["J. K. Rowling"]),
        filter_words=set(), author_surnames={"rowling"},
    ) is False


def test_unrelated_books_survive_the_filter():
    filter_words = {"harry", "potter", "sorcerer's", "stone"}
    for title in ["The Stone Sky", "A Wizard of Earthsea", "The Name of the Wind"]:
        assert _is_about_source(_bk(title), filter_words) is False, title


def test_stopwords_must_not_count_as_evidence():
    # "the" is three characters, so it used to survive into the source's title
    # words; combined with any second match it dropped legitimate candidates.
    # This asserts the *filter's* behaviour given a stopword-free word set,
    # which is what _gather_similar_candidates now builds.
    assert _is_about_source(_bk("The Stone Sky"), {"harry", "potter", "stone"}) is False


# ------------------------------------------------------- candidate queries

def test_facet_tags_never_become_subject_queries():
    # The Hunger Games' real Open Library tags. Searching these returns famine
    # studies; [] sends the caller to derive a genre from the blurb instead.
    assert _similar_genre_queries(
        ["Severe poverty", "Effects of war", "Oppression", "Self-sacrifice"],
        set(), set(),
    ) == []


def test_real_genres_still_become_queries():
    assert _similar_genre_queries(["Epic Fantasy", "Severe poverty"], set(), set()) \
        == ["Epic Fantasy"]


def test_genre_synonyms_are_recognised_as_real_genres():
    # The query builder keeps original case and doesn't fold, so the core-genre
    # check has to fold before comparing or "Sci-Fi" would look like a facet.
    assert _similar_genre_queries(["Sci-Fi"], set(), set()) == ["Sci-Fi"]


# ------------------------------------------------------- genre inference
# Held-out measurement: 14 of 37 books carried no recognised genre atom, and
# every book that returned zero recommendations was in that group. Inference
# reads the story vocabulary — including the facets, which is usually where
# the evidence hides — when nothing names a genre outright.

def test_facet_tags_are_read_as_genre_evidence():
    # The Fellowship of the Ring's real Open Library tags. Nothing here names
    # a genre; everything here says "fantasy".
    assert _infer_genre_from_content("Elves Dwarves evil fear hope") == "Fantasy"


def test_inference_covers_genres_beyond_fantasy():
    # Guards against the whole table being tuned on the fantasy titles this
    # work started from.
    cases = [
        ("brainwashing psychiatric hospital patients united states marshals", "Thriller"),
        ("spaceship alien planet colony", "Science Fiction"),
        ("haunted house ghost demon", "Horror"),
        ("detective murder homicide investigation", "Mystery"),
        ("wedding bride marriage lovers", "Romance"),
        ("dungeon respawn mana guild loot", "LitRPG"),
    ]
    for text, expected in cases:
        assert _infer_genre_from_content(text) == expected, text


def test_a_single_stray_marker_does_not_retag_a_book():
    # Two distinct markers are required precisely so one incidental word can't
    # turn a memoir into a fantasy novel.
    assert _infer_genre_from_content(
        "A memoir about my grandmother's magic touch in the kitchen") is None
    assert _infer_genre_from_content("A quiet novel about family life in Ohio") is None
    assert _infer_genre_from_content("") is None


def test_inference_folds_plurals_like_the_tokeniser():
    # Markers are folded at import; if that ever stops matching _text_tokens'
    # folding, "dwarves" would silently stop matching "dwarve".
    assert _infer_genre_from_content("elf dwarf") == "Fantasy"
    assert _infer_genre_from_content("elves dwarves") == "Fantasy"


# ------------------------------------------------------- candidate pool size
# Measured: for sources with thin metadata, a deeper candidate pool was the
# only lever that helped. Shutter Island went 2 recommendations (top 12%) at
# batch 300 to 7 (26%) at 700 and 10 at 1000; Mistborn, already healthy, was
# unchanged. Raising the enrichment cap instead changed nothing anywhere.

def test_similar_uses_a_deeper_pool_than_the_library_default():
    # /library/recommend scores against a whole library, so it has signal to
    # spare; /similar has one book and needs the wider net.
    from server.app import SIMILAR_OL_BATCH, _fetch_genre_candidates
    import inspect

    library_default = inspect.signature(_fetch_genre_candidates).parameters["ol_batch"].default
    assert SIMILAR_OL_BATCH > library_default


def test_offline_generation_reaches_further_than_the_live_endpoint():
    # Nobody waits on a page built once a week, so the generator can afford a
    # pool the request path can't.
    from server.app import SEO_OL_BATCH, SIMILAR_OL_BATCH

    assert SEO_OL_BATCH > SIMILAR_OL_BATCH


def test_batch_size_is_threaded_through_to_the_fetch():
    # Guards the wiring: _similar_core -> _gather_similar_candidates -> fetch.
    # A default that stopped being passed would silently revert the pool size.
    import inspect
    from server.app import (
        SIMILAR_OL_BATCH, _gather_similar_candidates, _similar_core,
    )

    for fn in (_gather_similar_candidates, _similar_core):
        assert inspect.signature(fn).parameters["ol_batch"].default == SIMILAR_OL_BATCH

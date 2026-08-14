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


# ---------------------------------------- the whitelist must never empty a pool
# Production regression: CORE_GENRES was curated from a fantasy-heavy sample and
# then used as a hard gate on the live query path. Searching "Harry potter"
# returns a record tagged "children's stories" — a real genre the whitelist
# happened to omit — so the query list came back empty, the pool fell back to a
# generic "fiction" search, and Find Similar returned nothing for the site's
# most popular query. The whitelist is a preference now, not a gate.

def test_unrecognised_genres_still_produce_a_query_when_facets_are_allowed():
    for tag in ["Children's stories", "Some Genre Nobody Whitelisted",
                "Sea stories", "Bildungsromans"]:
        assert _similar_genre_queries([tag], set(), set(), allow_facets=True), tag


def test_whitelisted_genres_still_win_over_facets():
    # Ordering is the whole point: a real genre must be preferred, and only
    # when there is none do the facets get used.
    assert _similar_genre_queries(
        ["Severe poverty", "Epic Fantasy"], set(), set(), allow_facets=True,
    ) == ["Epic Fantasy"]


def test_facets_are_still_withheld_by_default():
    # The caller tries deriving a genre from the text before resorting to
    # facets, so the default must stay strict or that ordering collapses.
    assert _similar_genre_queries(["Severe poverty"], set(), set()) == []


def test_childrens_stories_is_recognised_as_a_genre():
    from server.seo import CORE_GENRES
    assert "children's stories" in CORE_GENRES
    assert _similar_genre_queries(["Children's stories"], set(), set()) \
        == ["Children's stories"]


# ------------------------------------------------------------- author caps
# Production: Find Similar on Harry Potter returned six Enid Blyton titles.
# Every one matched the source's "children's adventure stories" tag correctly;
# the list was still useless, because one prolific author owns that subject in
# Open Library. The cap existed but was wired only to the page generator.

def _authored(book_id, author):
    return Books(id=book_id, title=f"Book {book_id}", authors=[author],
                 description="d", tags=[], metadata={})


def test_one_author_cannot_take_the_whole_list():
    from server.app import SIMILAR_AUTHOR_CAP, _cap_by_author

    ranked = [(_authored(str(i), "Enid Blyton"), 0.5) for i in range(6)]
    ranked += [(_authored("x", "Diana Wynne Jones"), 0.4),
               (_authored("y", "Eva Ibbotson"), 0.3)]
    kept = _cap_by_author(ranked, SIMILAR_AUTHOR_CAP, top_n=20)
    blyton = [b for b, _ in kept if b.authors == ["Enid Blyton"]]
    assert len(blyton) == SIMILAR_AUTHOR_CAP
    assert {b.authors[0] for b, _ in kept} == {
        "Enid Blyton", "Diana Wynne Jones", "Eva Ibbotson"}


def test_the_sources_own_author_is_exempt_from_the_cap():
    # Clicking Find Similar on Mistborn and getting more Sanderson is the
    # feature working, not the bug — only *other* authors get capped.
    from server.app import _cap_by_author, _norm_title

    ranked = [(_authored(str(i), "Brandon Sanderson"), 0.5) for i in range(5)]
    kept = _cap_by_author(ranked, 2, top_n=20,
                          exempt={_norm_title("Brandon Sanderson")})
    assert len(kept) == 5


def test_cap_does_not_backfill_with_the_author_it_just_capped():
    # Backfilling to reach top_n undoes the cap entirely when the pool is
    # dominated by one author, which is exactly when it's needed.
    from server.app import _cap_by_author

    ranked = [(_authored(str(i), "Enid Blyton"), 0.5) for i in range(10)]
    assert len(_cap_by_author(ranked, 2, top_n=20)) == 2


def test_inference_adds_the_genre_a_catalogue_shelf_omits():
    # Harry Potter's record carries children's/juvenile tags and no fantasy
    # atom, so the pool was children's adventure only. The blurb says wizard
    # and witchcraft; inference is what puts fantasy back in the queries.
    assert _infer_genre_from_content(
        "Harry Potter and the Sorcerer's Stone An orphaned boy discovers he "
        "is a wizard and attends a school of witchcraft, studying spells and "
        "magic. Children's stories Juvenile fiction"
    ) == "Fantasy"


# ------------------------------------- audience-only sources must not fall back
# Production: searching "Harry potter" returns a record whose only atom is
# "children's stories". _real_genres correctly rejects that as an audience
# marker — but the legacy fallback then scored it *as* a genre, handing the
# whole list back to Enid Blyton, who owns that subject in Open Library. The
# safety net reintroduced the bug it was meant to catch.

def test_audience_only_source_scores_no_genre_agreement():
    from server.app import _similar_genre_score

    audience_only = {"children's stories"}
    blyton = {"children's adventure stories", "children's stories"}
    assert _similar_genre_score(blyton, audience_only) == 0.0


def test_a_genuinely_unrecognised_source_still_falls_back():
    # The fallback is still wanted for genres the vocabulary doesn't know —
    # it just must not treat an audience marker as one of them.
    from server.app import _similar_genre_score

    unknown = {"bildungsromans", "sea stories"}
    assert _similar_genre_score({"sea stories"}, unknown) > 0


def test_a_tagged_but_genreless_source_counts_as_sparse():
    # The enrichment trigger keyed on "no tags or short description", so a
    # record with one useless tag and a real blurb skipped enrichment entirely
    # while a sibling record for the same book was tagged Fantasy.
    from server.app import _genre_atoms, _real_genres

    genreless = ["Children's stories, English"]
    assert not _real_genres(set(_genre_atoms(genreless)[0]))
    assert _real_genres(set(_genre_atoms(["Fantasy"])[0])) == {"fantasy"}


# ------------------------------------------- borrowing a genre from a sibling
# Open Library lists the same book twice: a "Harry Potter" record tagged only
# "Children's stories" and a "Harry Potter and the Sorcerer's Stone" record
# tagged "Fantasy". Enrichment compared series keys for equality, so the
# genreless record could never borrow from its own sibling, and Find Similar
# on it returned nothing at all.

def test_a_short_title_matches_its_full_titled_sibling():
    from server.app import _names_same_work

    assert _names_same_work("harry potter and the sorcerers stone", "harry potter")
    assert _names_same_work("harry potter", "harry potter")


def test_prefix_matching_needs_a_distinctive_title():
    from server.app import _names_same_work

    # "It" or "Dune" would otherwise prefix-match half the catalogue.
    assert not _names_same_work("it ends with us", "it")
    assert not _names_same_work("dune messiah", "dune")


def test_a_prefix_must_land_on_a_word_boundary():
    from server.app import _names_same_work

    # "the hobbits" is a different work from "the hobbit".
    assert not _names_same_work("the hobbits of the shire", "the hobbit")
    assert _names_same_work("the hobbit there and back again", "the hobbit")


def test_a_companion_volume_is_not_the_same_work():
    """A book *about* a book prefix-matches it perfectly.

    "The Hunger Games and Philosophy" starts with "the hunger games", so the
    sibling rule accepted it and the novel borrowed its `philosophy` tags —
    Find Similar on The Hunger Games returned Kant, Nietzsche and Lenin.
    """
    from server.app import _names_same_work

    src = "the hunger games"
    for companion in ("the hunger games and philosophy",
                      "the hunger games companion",
                      "the hunger games study guide",
                      "the hunger games sparknotes",
                      "the hunger games summary and analysis",
                      "the hunger games unofficial cookbook"):
        assert not _names_same_work(companion, src), companion


def test_the_companion_guard_only_inspects_the_added_words():
    # Someone whose source genuinely is a study guide must still be able to
    # enrich from another copy of that same study guide.
    from server.app import _names_same_work

    guide = "the hunger games study guide"
    assert _names_same_work(guide, guide)
    # And a real edition is still a sibling.
    assert _names_same_work("the hunger games trilogy boxset", "the hunger games")


def test_prefix_siblings_need_agreeing_authors(monkeypatch):
    """A longer title is only the same work if the authors positively agree.

    An exact title match can be taken on trust; a longer one cannot, because
    that is the shape of a companion, a sequel, or an unrelated book that
    happens to start the same way. The Hunger Games arrived from /search with
    no author, so nothing contradicted the companion volume.
    """
    import server.app as app
    from server.models.book import Books

    sibling = Books(id="ol_/works/OL9W", title="The Hunger Games And The Sequel",
                    authors=["Suzanne Collins"], description="x" * 200,
                    tags=["Dystopian"], metadata={})

    class _FakeFetcher:
        def __init__(self, source=None, **kw): pass
        def fetch_google_page(self, *a, **k): return [], 0
        def fetch_page(self, *a, **k): return [sibling], 1
        def fetch_work_detail(self, key): return ("", [])

    monkeypatch.setattr(app, "Fetcher", _FakeFetcher)

    # Authorless source: the longer-titled sibling must be refused.
    anon = Books(id="ol_/works/OL1W", title="The Hunger Games", authors=[],
                 description="", tags=["Contests"], metadata={})
    app._enrich_source_by_title_lookup(anon)
    assert "Dystopian" not in anon.tags

    # Same source with its author known: now the sibling is trusted.
    named = Books(id="ol_/works/OL1W", title="The Hunger Games",
                  authors=["Suzanne Collins"], description="", tags=["Contests"],
                  metadata={})
    app._enrich_source_by_title_lookup(named)
    assert "Dystopian" in named.tags

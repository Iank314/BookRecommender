"""Tests for _score_similar_candidates — the "Find Similar" scoring math.

Regression context: /similar previously scored with a single token-set F1 over
title + tags + description lumped together, plus an additive popularity boost.
Two failure modes drove the rewrite:
  1. A candidate sharing only genre-tag strings (every candidate is fetched by
     genre, so most do) scored as if its text matched the source.
  2. The additive popularity boost (sim + (1-sim)*0.1*pop) nearly doubled the
     score of popular-but-irrelevant books at the low-similarity end.
The new scorer mirrors /library/recommend: IDF-weighted F1 over title +
description only, blended with genre-atom overlap (description-weighted,
W_GENRE/W_DESC = 0.4/0.6), popularity as a ≤5% multiplicative tiebreaker.
"""

import pytest

from server.app import _score_similar_candidates, _similar_genre_queries
from server.models.book import Books


def _bk(id: str, title: str, description: str = "", tags: list[str] | None = None,
        metadata: dict | None = None) -> Books:
    return Books(
        id=id, title=title, authors=[], description=description,
        tags=tags or [], metadata=metadata or {},
    )


SOURCE = _bk(
    "src", "The Crystal Sword",
    description="A young orphan discovers a crystal sword and battles the "
                "necromancer king across the frozen wastes of a dying kingdom.",
    tags=["Fantasy", "Epic Fantasy"],
)


def test_description_match_outranks_tag_only_match():
    # Shares real story vocabulary with the source.
    text_match = _bk(
        "a", "Blade of Winter",
        description="An orphan wields an enchanted sword against the "
                    "necromancer armies in a frozen dying kingdom.",
        tags=["Fantasy"],
    )
    # Shares only the genre tags — description is about something else entirely.
    tag_only = _bk(
        "b", "The Baker's Daughter",
        description="A heartwarming romance set in a small village bakery.",
        tags=["Fantasy", "Epic Fantasy"],
    )
    scored = _score_similar_candidates(SOURCE, [tag_only, text_match])
    ranked = [b.id for b, _ in scored]
    assert ranked and ranked[0] == "a"
    # The tag-only book shares no source text, so it must not rank at all.
    assert "b" not in ranked


def test_popularity_is_a_tiebreaker_not_a_ranking_signal():
    weak_match_popular = _bk(
        "pop", "Famous Classic",
        description="A sword appears briefly in this story of a kingdom.",
        tags=["Fantasy"],
        metadata={"edition_count": 500, "ratings_count": 100000,
                  "ratings_average": 4.5, "want_to_read_count": 50000},
    )
    strong_match_obscure = _bk(
        "obs", "Forgotten Debut",
        description="An orphan discovers a crystal sword and battles the "
                    "necromancer king in the frozen wastes of a dying kingdom.",
        tags=["Fantasy", "Epic Fantasy"],
        metadata={},
    )
    scored = _score_similar_candidates(SOURCE, [weak_match_popular, strong_match_obscure])
    ranked = [b.id for b, _ in scored]
    assert ranked[0] == "obs", "popularity must not outrank a genuine text match"


def test_genre_overlap_breaks_text_ties():
    in_genre = _bk(
        "g1", "Echoes of the Necromancer",
        description="The necromancer king rises in the frozen kingdom.",
        tags=["Epic Fantasy"],
    )
    off_genre = _bk(
        "g2", "Necromancy Through the Ages",
        description="The necromancer king rises in the frozen kingdom.",
        tags=["Occultism"],
    )
    scored = _score_similar_candidates(SOURCE, [off_genre, in_genre])
    ranked = [b.id for b, _ in scored]
    assert ranked[0] == "g1"


def test_tagless_candidate_scored_on_description_alone():
    tagless = _bk(
        "t", "The Shattered Blade",
        description="An orphan and a crystal sword stand against the "
                    "necromancer king of the dying kingdom.",
    )
    scored = _score_similar_candidates(SOURCE, [tagless])
    assert [b.id for b, _ in scored] == ["t"]


def test_empty_source_returns_nothing():
    empty_source = _bk("src", "")
    cand = _bk("c", "Some Book", description="Words about things.", tags=["Fantasy"])
    assert _score_similar_candidates(empty_source, [cand]) == []


def test_no_candidates_returns_empty():
    assert _score_similar_candidates(SOURCE, []) == []


# ---- _similar_genre_queries --------------------------------------------------
# Regression: /similar's query builder was a drifted copy of _genre_atoms'
# tag splitting that skipped the facet handling — an enriched source whose OL
# subjects included "series:Dungeon Crawler Carl" burned one of the five
# subject-search slots on it. Both paths now share _tag_atoms.

def test_facet_tags_do_not_become_queries():
    queries = _similar_genre_queries(
        ["series:Dungeon Crawler Carl", "person:Carl", "genre:LitRPG"],
        title_words={"dungeon", "crawler", "carl"}, author_words=set(),
    )
    assert queries == ["LitRPG"]


def test_trailing_period_stripped_from_queries():
    # OL subjects like "Fantasy fiction." previously went out period and all.
    queries = _similar_genre_queries(
        ["Fantasy fiction."], title_words=set(), author_words=set(),
    )
    assert queries == ["Fantasy fiction"]


def test_only_real_genres_become_queries():
    assert _similar_genre_queries(["Fiction", "Epic Fantasy"], set(), set()) == ["Epic Fantasy"]


def test_generic_and_facet_tags_produce_no_queries():
    # Returning [] is the caller's signal to derive a genre from the title and
    # description, which beats both options available here: "Fiction" as a
    # subject search returns random classics, and a facet search returns books
    # about the facet — The Hunger Games' ["severe poverty", "effects of war"]
    # fetched a pool of famine studies.
    assert _similar_genre_queries(["Fiction"], set(), set()) == []
    assert _similar_genre_queries(["Severe poverty", "Effects of war"], set(), set()) == []


def test_title_and_author_atoms_are_skipped():
    queries = _similar_genre_queries(
        ["Harry Potter", "Rowling", "Fantasy"],
        title_words={"harry", "potter"}, author_words={"rowling"},
    )
    assert queries == ["Fantasy"]


# ---------------------------------------------------------------------------
# Sources with no usable description
#
# Providers frequently return a "description" that is publication boilerplate
# ("First published in 2000. | By ...") — a handful of tokens, none of them
# about the story. Two rules used to conspire against that case: the scorer
# required a candidate to share description tokens with the source, and the
# blend weighted description above genre. Together they scored an entire
# candidate pool at zero.
#
# These use synthetic books on purpose. The bug was found on specific titles,
# but pinning the tests to those titles would fix the thresholds to the five
# books that happened to be looked at.
# ---------------------------------------------------------------------------

from server.app import _blend_genre_desc, _MIN_SOURCE_TEXT_TOKENS  # noqa: E402

BOILERPLATE_SOURCE = _bk(
    "src2", "The Silent Tower",
    description="First published in 1998.",   # under the token threshold
    tags=["Fantasy"],
)


def test_blend_uses_genre_alone_when_the_source_has_no_text():
    # Otherwise a perfect genre match is multiplied down by a meaningless
    # zero description score.
    assert _blend_genre_desc(1.0, 0.0, has_genres=True, source_has_text=False) == 1.0


def test_blend_uses_description_alone_when_the_candidate_has_no_genres():
    assert _blend_genre_desc(0.0, 0.42, has_genres=False) == 0.42


def test_blend_is_weighted_when_both_signals_exist():
    blended = _blend_genre_desc(1.0, 1.0, has_genres=True, source_has_text=True)
    assert blended == 1.0
    partial = _blend_genre_desc(1.0, 0.0, has_genres=True, source_has_text=True)
    assert 0.0 < partial < 1.0   # genre alone can't reach the top when text exists


def test_textless_source_still_ranks_genre_matches():
    # The regression: every candidate scored 0 and the list came back empty.
    same_genre = _bk("a", "Tower of Ash", description="A mage climbs a tower.",
                     tags=["Fantasy"])
    other_genre = _bk("b", "Quarterly Returns",
                      description="A study of corporate accounting practice.",
                      tags=["Business & Economics"])
    scored = _score_similar_candidates(BOILERPLATE_SOURCE, [same_genre, other_genre])
    assert scored, "a source with boilerplate text must still rank on genre"
    assert scored[0][0].id == "a"


def test_textless_source_ignores_candidates_with_no_genre_either():
    # With neither signal on either side there is nothing to rank on, so an
    # honest empty beats an arbitrary ordering.
    untagged = _bk("c", "Something", description="A book about things.")
    assert _score_similar_candidates(BOILERPLATE_SOURCE, [untagged]) == []


def test_a_real_blurb_still_requires_shared_description():
    # The genre-only path must not leak into sources that do have text, or
    # every book fetched by genre would rank equally.
    genre_only = _bk("d", "Unrelated Tale",
                     description="Corporate accounting in the modern firm.",
                     tags=["Fantasy", "Epic Fantasy"])
    scored = _score_similar_candidates(SOURCE, [genre_only])
    assert scored == []


def test_token_threshold_separates_boilerplate_from_a_real_blurb():
    from server.app import _text_tokens

    assert len(_text_tokens(BOILERPLATE_SOURCE)) < _MIN_SOURCE_TEXT_TOKENS
    assert len(_text_tokens(SOURCE)) > _MIN_SOURCE_TEXT_TOKENS


# ---------------------------------------------------------------------------
# Similarity is about content, not titles
#
# Production regression: Find Similar on "Harry Potter" returned "Harry the
# Dirty Dog" and "Harry by the Sea" — children's books about a dog, matched
# purely on a shared first name. Titles are mostly proper nouns, and rare ones
# at that, so IDF weighted them heavily. A recommendation has to come from what
# a book is about.
# ---------------------------------------------------------------------------

from server.app import _content_tokens  # noqa: E402


def test_content_tokens_ignore_the_title():
    book = _bk("x", "Harry Potter and the Sorcerer's Stone",
               description="A young wizard attends a school of magic.")
    tokens = _content_tokens(book)
    assert "potter" not in tokens
    assert "harry" not in tokens
    assert {"wizard", "school", "magic"} <= tokens


def test_content_tokens_fall_back_to_the_title_when_there_is_no_blurb():
    # Some signal beats none; _has_recommendable_content drops most of these
    # before scoring anyway.
    assert "dragon" in _content_tokens(_bk("y", "Dragons of Autumn Twilight"))  # folded


def test_a_shared_name_in_the_title_is_not_similarity():
    source = _bk(
        "src", "Harry Potter and the Sorcerer's Stone",
        description="An orphaned boy discovers he is a wizard and attends "
                    "a school of witchcraft, where he studies spells and magic.",
        tags=["Juvenile Fiction"],
    )
    dog_book = _bk(
        "dog", "Harry the Dirty Dog",
        description="A white dog with black spots runs away from home and "
                    "gets so dirty his family does not recognise him.",
        tags=["Juvenile Fiction"],
    )
    magic_book = _bk(
        "magic", "The Worst Witch",
        description="A clumsy young witch attends a school of witchcraft, "
                    "struggling with her spells and magic lessons.",
        tags=["Juvenile Fiction"],
    )
    ranked = [b.id for b, _ in _score_similar_candidates(source, [dog_book, magic_book])]
    assert ranked and ranked[0] == "magic", "content must outrank a shared name"
    assert "dog" not in ranked, "a shared first name is not a reason to recommend"


# ---------------------------------------------------------------------------
# Genre agreement: coverage of the source, not fraction of the candidate
#
# Measured on The Hobbit before this change: A Wizard of Earthsea scored 0.25
# on genre and The Very Hungry Caterpillar 0.33. Both matched exactly one atom;
# the picture book won because it had fewer tags to divide by. The old measure
# penalised books for being well catalogued, and treated "children's fiction"
# — an audience, not a genre — as a reason two books are alike.
# ---------------------------------------------------------------------------

from server.app import _real_genres, _similar_genre_score  # noqa: E402

HOBBIT_PROFILE = {"children's fiction", "comic books", "fantasy",
                  "middle earth (imaginary place)", "strips"}


def test_catalogue_noise_is_excluded_from_the_source_profile():
    # Only "fantasy" describes what kind of book it is.
    assert _real_genres(HOBBIT_PROFILE) == {"fantasy"}


def test_audience_atoms_are_not_genres():
    assert _real_genres({"children's fiction", "juvenile fiction"}) == set()
    assert _real_genres({"fantasy", "children's fiction"}) == {"fantasy"}


def test_a_genre_match_outranks_an_audience_match():
    earthsea = {"fantasy", "magic", "magic in fiction"}
    caterpillar = {"caterpillars", "children's fiction", "toy and movable books"}
    assert _similar_genre_score(earthsea, HOBBIT_PROFILE) > \
        _similar_genre_score(caterpillar, HOBBIT_PROFILE)


def test_extra_tags_no_longer_penalise_a_candidate():
    # The whole inversion: same genre match, more tags, used to score lower.
    sparse = {"fantasy"}
    rich = {"fantasy", "magic", "dragons", "quests", "wizards"}
    assert _similar_genre_score(rich, HOBBIT_PROFILE) == \
        _similar_genre_score(sparse, HOBBIT_PROFILE)


# ---- subgenre / parent-genre subsumption -------------------------------------
# Genre agreement is set overlap, which has no notion of "high fantasy is a kind
# of fantasy". Measured before the fix: every pair below scored 0.000 in both
# directions. It was mostly latent while Open Library returned no subjects — the
# signal came from Google Books' coarse "Fiction / Fantasy" categories, i.e. the
# parent terms — and became the common case once the `fields` parameter started
# supplying precise subgenre atoms for every OL candidate.

SUBGENRE_PAIRS = [
    ("high fantasy", "fantasy"),
    ("epic fantasy", "fantasy"),
    ("dark fantasy", "fantasy"),
    ("urban fantasy", "fantasy"),
    ("space opera", "science fiction"),
    ("cyberpunk", "science fiction"),
    ("cozy mystery", "mystery"),
    ("detective", "mystery"),
    ("psychological thriller", "thriller"),
    ("gothic", "horror"),
    ("paranormal romance", "romance"),
]


@pytest.mark.parametrize("sub,parent", SUBGENRE_PAIRS)
def test_a_subgenre_source_gets_credit_from_its_parent(sub, parent):
    # Mistborn's OL record is `high fantasy`; Erikson's Malazan is `fantasy`.
    # Scoring that pair at zero left 1 of 472 candidates above the floor.
    assert _similar_genre_score({parent}, {sub}) > 0


@pytest.mark.parametrize("sub,parent", SUBGENRE_PAIRS)
def test_a_parent_source_gets_credit_from_its_subgenre(sub, parent):
    assert _similar_genre_score({sub}, {parent}) > 0


@pytest.mark.parametrize("sub,parent", SUBGENRE_PAIRS)
def test_an_exact_subgenre_match_still_outranks_a_parent_only_match(sub, parent):
    # Widening must not flatten the ranking: "high fantasy" is a better answer
    # for a high-fantasy source than plain "fantasy" is.
    assert _similar_genre_score({sub}, {sub}) >= _similar_genre_score({parent}, {sub})


def test_subsumption_does_not_merge_unrelated_genres():
    # The risk of a parent map is a wrong edge silently fusing two genres.
    assert _similar_genre_score({"romance"}, {"high fantasy"}) == 0.0
    assert _similar_genre_score({"cooking"}, {"space opera"}) == 0.0
    assert _similar_genre_score({"mystery"}, {"fantasy"}) == 0.0


def test_contested_subgenres_are_deliberately_not_mapped():
    # Left out on purpose — see the note on _GENRE_PARENTS. The Road is neither
    # science fiction nor a comfortable fit for either label.
    assert _similar_genre_score({"science fiction"}, {"dystopian"}) == 0.0
    assert _similar_genre_score({"romance"}, {"erotica"}) == 0.0


def test_exact_matches_are_unchanged_by_subsumption():
    for atoms in ({"fantasy"}, {"high fantasy"}, {"fantasy", "science fiction"}):
        assert _similar_genre_score(atoms, atoms) == pytest.approx(0.85)


# ---- audience: weak reward, strong penalty -----------------------------------
# Audience used to be a flat +0.15 for agreeing, which made a `young adult
# fiction` tag worth more than being the actual sequel: The Hobbit ranked
# Midnight Sun and three Holly Black novels above The Lord of the Rings,
# purely because LOTR's record carries no YA shelf.

HOBBIT_YA_SOURCE = {"fantasy", "young adult fiction"}


def test_an_untagged_candidate_is_not_treated_as_a_conflict():
    """_AUDIENCE_ATOMS lists only juvenile shelves -- there is no "adult" atom.

    So a missing audience tag cannot distinguish an adult book from an
    uncatalogued one, and must not be scored as disagreement.
    """
    from server.app import _audience_conflict
    assert not _audience_conflict({"fantasy"}, HOBBIT_YA_SOURCE)
    assert _similar_genre_score({"fantasy"}, HOBBIT_YA_SOURCE) == pytest.approx(0.85)


def test_a_juvenile_candidate_conflicts_with_an_adult_source():
    from server.app import _audience_conflict
    adult = {"fantasy"}
    assert _audience_conflict({"fantasy", "picture books"}, adult)
    assert _similar_genre_score({"fantasy", "picture books"}, adult) < \
        _similar_genre_score({"fantasy"}, adult)


def test_disagreeing_juvenile_shelves_still_conflict():
    from server.app import _audience_conflict
    assert _audience_conflict({"fantasy", "board books"}, HOBBIT_YA_SOURCE)


def test_audience_agreement_still_earns_something():
    # Removing the reward entirely would stop a children's source preferring
    # children's books at all.
    agree = _similar_genre_score({"fantasy", "young adult fiction"}, HOBBIT_YA_SOURCE)
    unknown = _similar_genre_score({"fantasy"}, HOBBIT_YA_SOURCE)
    assert agree > unknown


def test_a_description_match_outranks_an_audience_match():
    """The whole point: the nudge must be overturnable by real similarity.

    Values are the ones measured in production -- The Lord of the Rings scored
    desc F1 0.064 against The Hobbit, Midnight Sun 0.016.
    """
    from server.app import _blend_genre_desc
    ya = _similar_genre_score({"fantasy", "romance", "young adult fiction"},
                              HOBBIT_YA_SOURCE)
    lotr = _similar_genre_score({"fantasy"}, HOBBIT_YA_SOURCE)
    assert _blend_genre_desc(lotr, 0.064, has_genres=True, source_has_text=True) > \
        _blend_genre_desc(ya, 0.016, has_genres=True, source_has_text=True)


def test_sharing_only_an_audience_shelf_scores_nothing():
    # The flat payout gave a candidate with no genre overlap 0.15 outright --
    # above MIN_SIMILAR_SCORE on shelving alone.
    assert _similar_genre_score({"romance", "young adult fiction"},
                                HOBBIT_YA_SOURCE) == 0.0


# ---- genre specificity -------------------------------------------------------
# Genre agreement used to count atoms, and an atom is not a unit of evidence.
# Measured on Dungeon Crawler Carl, whose OL record carries `litrpg` and
# (wrongly) `graphic novel`: a LitRPG candidate and a Graphic Classics
# anthology each matched one of two profile atoms and scored the same, so
# Edgar Allan Poe collections outranked the book's own sequels.

def test_a_niche_genre_outweighs_a_broad_one():
    profile = {"litrpg", "graphic novel"}
    niche = _similar_genre_score({"litrpg"}, profile)
    broad = _similar_genre_score({"graphic novel"}, profile)
    assert niche > broad * 2, f"litrpg {niche:.3f} vs graphic novel {broad:.3f}"


def test_matching_every_stated_genre_still_scores_full():
    profile = {"litrpg", "graphic novel"}
    assert _similar_genre_score(profile, profile) == pytest.approx(0.85)


def test_gothic_outweighs_historical_for_a_gothic_source():
    # Mexican Gothic returned Regency romances: `historical` counted as much
    # as `gothic`, and (before `historical` became a synonym of `historical
    # fiction`) one candidate tag matched two profile atoms.
    profile = {"gothic", "historical fiction"}
    romance = _similar_genre_score({"historical fiction", "romance"}, profile)
    horror = _similar_genre_score({"gothic", "horror"}, profile)
    assert horror > romance * 2, f"gothic {horror:.3f} vs historical {romance:.3f}"


def test_historical_and_historical_fiction_are_one_atom():
    # Synonyms, not parent/child -- two atoms for one idea let a single
    # candidate tag match twice and saturate the coverage cap.
    from server.app import _genre_atoms
    assert _genre_atoms(["Historical"])[0] == _genre_atoms(["Historical fiction"])[0]


@pytest.mark.parametrize("cand,profile,expected", [
    ({"fantasy"}, {"fantasy"}, 0.85),                       # single-genre source
    ({"fantasy"}, {"fantasy", "science fiction"}, 0.425),   # one of two
    ({"high fantasy"}, {"high fantasy"}, 0.85),             # exact subgenre
    ({"fantasy"}, {"high fantasy"}, 0.425),                 # parent-only
    ({"romance"}, {"high fantasy"}, 0.0),                   # unrelated
])
def test_weighting_does_not_disturb_the_established_cases(cand, profile, expected):
    # Specificity weighting must not quietly re-tune everything that already
    # worked -- these are the cases earlier rounds were calibrated on.
    assert _similar_genre_score(cand, profile) == pytest.approx(expected)


def test_an_unknown_genre_gets_the_default_weight():
    from server.app import _genre_weight, _GENRE_WEIGHT_DEFAULT
    assert _genre_weight("some-genre-nobody-listed") == _GENRE_WEIGHT_DEFAULT
    assert _genre_weight("litrpg") > _genre_weight("fantasy") > _genre_weight("epic")


# ---- source description recovery ---------------------------------------------
# A source with no usable description makes genre 100% of the score instead of
# 34%, and genre is effectively binary — so every book sharing a genre ties at
# the same score and ranking collapses to a popularity nudge. In production
# that returned 1654 results for Mistborn, led by Le Petit Prince.

def test_title_lookup_follows_the_work_record_for_a_blurb(monkeypatch):
    """OL search records carry boilerplate; the real blurb is one fetch away.

    A source that resolved to a Google Books id never qualifies for
    _ensure_details' work-detail path, so when Google Books is down this is
    the only route to a description. Measured on Mistborn: every OL search
    sibling stopped at 76 characters, the work record holds 1436.
    """
    import server.app as app
    from server.models.book import Books

    boilerplate = "First published in 2012. | By Brandon Sanderson."
    sibling = Books(id="ol_/works/OL42W", title="Mistborn",
                    authors=["Brandon Sanderson"], description=boilerplate,
                    tags=["Fiction"], metadata={})

    class _FakeFetcher:
        def __init__(self, source=None, **kw): pass
        def fetch_google_page(self, *a, **k):
            raise RuntimeError("Google Books is 503ing")   # the production case
        def fetch_page(self, *a, **k):
            return [sibling], 1
        def fetch_work_detail(self, key):
            assert key == "/works/OL42W"
            return ("A thousand years ago the Lord Ruler seized control. " * 6,
                    ["Fantasy", "Epic"])

    monkeypatch.setattr(app, "Fetcher", _FakeFetcher)
    source = Books(id="gb_XYZ", title="Mistborn", authors=["Brandon Sanderson"],
                   description="Short blurb.", tags=["Fiction"], metadata={})
    app._enrich_source_by_title_lookup(source)

    assert len(source.description) > app.MIN_USABLE_DESC
    assert "Lord Ruler" in source.description
    assert "Fantasy" in source.tags


def test_title_lookup_skips_the_work_fetch_when_it_already_has_a_blurb(monkeypatch):
    # The extra request is a last resort, not a default -- it runs per source
    # on a provider that has blocked this app's IP before.
    import server.app as app
    from server.models.book import Books

    # Needs enough *distinct* content tokens to pass _has_usable_text --
    # _tokenize returns a set, so repeating a sentence adds nothing.
    real_blurb = ("A thousand years ago the Lord Ruler seized control of the "
                  "Final Empire, and ash now falls from a darkened sky.")
    sibling = Books(id="ol_/works/OL42W", title="Mistborn",
                    authors=["Brandon Sanderson"], description=real_blurb,
                    tags=["Fantasy"], metadata={})
    calls = []

    class _FakeFetcher:
        def __init__(self, source=None, **kw): pass
        def fetch_google_page(self, *a, **k):
            raise RuntimeError("no GB")
        def fetch_page(self, *a, **k):
            return [sibling], 1
        def fetch_work_detail(self, key):
            calls.append(key)
            return ("", [])

    monkeypatch.setattr(app, "Fetcher", _FakeFetcher)
    source = Books(id="gb_XYZ", title="Mistborn", authors=["Brandon Sanderson"],
                   description="", tags=[], metadata={})
    app._enrich_source_by_title_lookup(source)

    assert calls == [], "work detail fetched despite a usable blurb already found"
    assert source.description == real_blurb


def test_audience_still_counts_for_something():
    # A children's fantasy should beat an adult fantasy for a children's
    # source — just not beat it on audience alone.
    childrens_fantasy = {"fantasy", "children's fiction"}
    adult_fantasy = {"fantasy"}
    assert _similar_genre_score(childrens_fantasy, HOBBIT_PROFILE) > \
        _similar_genre_score(adult_fantasy, HOBBIT_PROFILE)
    # ...but audience alone must not reach a genre match.
    assert _similar_genre_score({"children's fiction"}, HOBBIT_PROFILE) < \
        _similar_genre_score(adult_fantasy, HOBBIT_PROFILE)


def test_partial_coverage_of_a_multi_genre_source():
    profile = {"fantasy", "mystery"}
    assert _similar_genre_score({"fantasy"}, profile) < \
        _similar_genre_score({"fantasy", "mystery"}, profile)


def test_falls_back_when_the_source_has_no_recognised_genre():
    # Otherwise every candidate would score zero for books whose genre the
    # vocabulary doesn't cover.
    facets_only = {"severe poverty", "effects of war"}
    assert _similar_genre_score({"severe poverty"}, facets_only) > 0


def test_library_recommend_scoring_is_untouched():
    # /similar is being trialled first; the library path must be unchanged.
    from server.app import _genre_score
    assert _genre_score({"fantasy", "magic", "dragons"}, {"fantasy"}) == 1 / 3


def test_a_noisy_profile_does_not_punish_a_correct_match():
    # Mistborn's profile is ["adventure", "fantasy", "mystery", "science
    # fiction"], two of which are wrong. Demanding coverage of all four scored
    # a correct fantasy match at 0.25 and cut the book's top result from 45%
    # to 21%. Two shared genres counts as full agreement.
    noisy = {"adventure", "fantasy", "mystery", "science fiction"}
    assert _similar_genre_score({"fantasy"}, noisy) == \
        pytest.approx(_similar_genre_score({"fantasy"}, {"fantasy", "mystery"}))


def test_matching_two_genres_still_beats_matching_one():
    profile = {"fantasy", "mystery", "romance"}
    assert _similar_genre_score({"fantasy", "mystery"}, profile) > \
        _similar_genre_score({"fantasy"}, profile)


def test_coverage_never_exceeds_one():
    profile = {"fantasy", "mystery", "romance", "horror"}
    assert _similar_genre_score(profile, profile) <= 1.0

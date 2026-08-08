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

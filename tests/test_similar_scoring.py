"""Tests for _score_similar_candidates — the "Find Similar" scoring math.

Regression context: /similar previously scored with a single token-set F1 over
title + tags + description lumped together, plus an additive popularity boost.
Two failure modes drove the rewrite:
  1. A candidate sharing only genre-tag strings (every candidate is fetched by
     genre, so most do) scored as if its text matched the source.
  2. The additive popularity boost (sim + (1-sim)*0.1*pop) nearly doubled the
     score of popular-but-irrelevant books at the low-similarity end.
The new scorer mirrors /library/recommend: IDF-weighted F1 over title +
description only, blended 50/50 with genre-atom overlap, popularity as a ≤5%
multiplicative tiebreaker.
"""

from server.app import _score_similar_candidates
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

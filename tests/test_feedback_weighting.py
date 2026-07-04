"""Tests for feedback re-weighting, the content-quality gate, and the
description-over-genre weighting in the recommendation scoring path.

Two production bugs (Ian Kaufman's library, July 2026) drove these:
  1. "The City Cantabile Choir Presents" — no author, no description, a lone
     "Epic" genre atom — rode into an all-fantasy library's recs on one lucky
     genre match (_genre_score gives a single matching atom a perfect 1.0) plus
     a coincidental title-token overlap. _has_recommendable_content drops it.
  2. Disliking two Warrior Cats books still surfaced a third ("Fire and Ice",
     same author): token overlap between sibling books is too thin to suppress
     a series, and the old dislike weight (0.5, floor 0.1) barely dented the
     score. _feedback_modifier now penalises a disliked author's other books
     hard.
Plus the explicit request that description outrank genre in scoring.
"""

import math

from server.app import (
    FB_AUTHOR_DISLIKE,
    FB_MOD_HI,
    FB_MOD_LO,
    _compute_token_idf,
    _feedback_authors,
    _feedback_modifier,
    _has_recommendable_content,
    _score_similar_candidates,
    _text_tokens,
)
from server.models.book import Books


def _bk(id, title="", description="", tags=None, authors=None, metadata=None):
    return Books(
        id=id, title=title, authors=authors or [], description=description,
        tags=tags or [], metadata=metadata or {},
    )


def _idf_for(books):
    return _compute_token_idf(books), math.log(len(books) + 1) + 1


# ---- content-quality gate ----------------------------------------------------

def test_no_author_no_description_is_junk():
    # The exact reported record: a title, a lone genre atom, nothing else.
    junk = _bk("j", "The City Cantabile Choir Presents", tags=["Epic"])
    assert _has_recommendable_content(junk) is False


def test_author_alone_is_recommendable():
    assert _has_recommendable_content(_bk("a", "T", authors=["Someone"])) is True


def test_description_alone_is_recommendable():
    assert _has_recommendable_content(_bk("a", "T", description="A tale.")) is True


def test_whitespace_only_author_does_not_count():
    assert _has_recommendable_content(_bk("a", "T", authors=["  "])) is False


def test_similar_scoring_drops_contentless_junk():
    # Source fantasy book + a junk "Epic" record with no author/description
    # whose title shares a token with the source, and a real look-alike.
    source = _bk(
        "src", "The Epic Crystal Sword",
        description="An orphan wields a crystal sword against the necromancer king.",
        tags=["Fantasy", "Epic Fantasy"])
    junk = _bk("junk", "The City Cantabile Choir Presents Epic", tags=["Epic"])
    real = _bk(
        "real", "Blade of Winter",
        description="An orphan wields a crystal sword against the necromancer king.",
        tags=["Fantasy"], authors=["A. Writer"])
    ranked = [b.id for b, _ in _score_similar_candidates(source, [junk, real])]
    assert "junk" not in ranked
    assert ranked and ranked[0] == "real"


# ---- description weighted above genre ----------------------------------------

def test_description_weighted_above_genre():
    # A strong description match that is OFF-genre must outrank a weak
    # description match that is in-genre. Under the old 0.5/0.5 blend the
    # in-genre book won; with 0.6/0.4 the description carries it.
    source = _bk(
        "src", "The Crystal Sword",
        description="An orphan discovers a crystal sword and battles the "
                    "necromancer king across the frozen wastes of a dying kingdom.",
        tags=["Fantasy", "Epic Fantasy"])
    strong_desc_offgenre = _bk(
        "d", "Blade of Winter",
        description="An orphan discovers a crystal sword and battles the "
                    "necromancer king across the frozen wastes of a dying kingdom.",
        tags=["Occultism"], authors=["X"])
    weak_desc_ingenre = _bk(
        "g", "Random Fantasy",
        description="A tale set in a distant kingdom.",
        tags=["Fantasy", "Adventure"], authors=["Y"])
    ranked = [b.id for b, _ in _score_similar_candidates(
        source, [weak_desc_ingenre, strong_desc_offgenre])]
    assert ranked[0] == "d"


# ---- _feedback_authors -------------------------------------------------------

def test_feedback_authors_squash_spelling():
    books = [_bk("a", authors=["Erin Hunter"]), _bk("b", authors=["erin  hunter"])]
    assert _feedback_authors(books) == {"erinhunter"}


def test_author_liked_and_disliked_cancels():
    # The netting the call site applies: an author both liked and disliked is
    # removed from the disliked set, so their books aren't cut.
    liked = [_bk("l", authors=["Mixed Author"])]
    disliked = [_bk("d", authors=["Mixed Author"])]
    net_disliked = _feedback_authors(disliked) - _feedback_authors(liked)
    assert net_disliked == set()


# ---- _feedback_modifier ------------------------------------------------------

def test_disliked_author_is_cut_hard():
    # Two disliked Warrior Cats books; a third by the same author must be
    # strongly suppressed even though the shared prose is thin.
    d1 = _bk("d1", "Into the Wild", authors=["Erin Hunter"],
             description="A house cat joins a clan of wild forest cats.")
    d2 = _bk("d2", "Forest of Secrets", authors=["Erin Hunter"],
             description="Clan warriors uncover a betrayal deep in the forest.")
    cand = _bk("c", "Fire and Ice", authors=["Erin Hunter"],
               description="Two young warriors journey across the frozen mountains.")
    idf, default_idf = _idf_for([d1, d2, cand])
    disliked_text = [_text_tokens(d1), _text_tokens(d2)]
    disliked_authors = _feedback_authors([d1, d2])
    mod = _feedback_modifier(
        cand, _text_tokens(cand), [], disliked_text,
        set(), disliked_authors, idf, default_idf)
    assert mod < 0.3


def test_disliked_author_beats_a_weak_description_only_penalty():
    # The author penalty is the point: a same-author dislike must suppress far
    # harder than description overlap alone would.
    d1 = _bk("d1", "Into the Wild", authors=["Erin Hunter"],
             description="A house cat joins a clan of wild forest cats.")
    cand = _bk("c", "Fire and Ice", authors=["Erin Hunter"],
               description="Two young warriors journey across the frozen mountains.")
    idf, default_idf = _idf_for([d1, cand])
    disliked_text = [_text_tokens(d1)]
    with_author = _feedback_modifier(
        cand, _text_tokens(cand), [], disliked_text,
        set(), _feedback_authors([d1]), idf, default_idf)
    without_author = _feedback_modifier(
        cand, _text_tokens(cand), [], disliked_text,
        set(), set(), idf, default_idf)
    assert with_author < without_author
    # The author penalty multiplies whatever the description signal produced,
    # so a disliked-author candidate can't exceed FB_AUTHOR_DISLIKE * (<=1.0).
    assert with_author <= FB_AUTHOR_DISLIKE


def test_liked_author_gets_a_gentle_lift():
    liked = _bk("l", "Book One", authors=["Fav Author"],
                description="A quiet story about a small village by the sea.")
    cand = _bk("c", "Book Two", authors=["Fav Author"],
               description="An entirely different tale about deep-space robots.")
    idf, default_idf = _idf_for([liked, cand])
    mod = _feedback_modifier(
        cand, _text_tokens(cand), [_text_tokens(liked)], [],
        _feedback_authors([liked]), set(), idf, default_idf)
    assert mod > 1.0


def test_modifier_stays_within_clamp():
    # A candidate identical to a disliked book AND by a disliked author drives
    # the raw modifier well below the floor — it must clamp, not go negative.
    d1 = _bk("d1", "Same Book", authors=["Bad Author"],
             description="The necromancer king rises across the frozen dying kingdom.")
    cand = _bk("c", "Same Book", authors=["Bad Author"],
               description="The necromancer king rises across the frozen dying kingdom.")
    idf, default_idf = _idf_for([d1, cand])
    mod = _feedback_modifier(
        cand, _text_tokens(cand), [], [_text_tokens(d1)],
        set(), _feedback_authors([d1]), idf, default_idf)
    assert FB_MOD_LO <= mod <= FB_MOD_HI


def test_no_feedback_is_neutral():
    cand = _bk("c", "Any Book", authors=["Someone"], description="Words about things.")
    idf, default_idf = _idf_for([cand])
    mod = _feedback_modifier(
        cand, _text_tokens(cand), [], [], set(), set(), idf, default_idf)
    assert mod == 1.0

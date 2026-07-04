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
    W_DESC,
    W_GENRE,
    _blend_genre_desc,
    _compute_token_idf,
    _feedback_authors,
    _feedback_modifier,
    _has_recommendable_content,
    _net_disliked_authors,
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

def test_no_description_is_not_recommendable():
    # "The City Cantabile Choir Presents" — no author, no description, a lone
    # genre atom. Nothing to assess content similarity on.
    junk = _bk("j", "The City Cantabile Choir Presents", tags=["Epic"])
    assert _has_recommendable_content(junk) is False


def test_author_without_description_is_not_recommendable():
    # "An Atlas of Fantasy" — a real author and genre tags, but no blurb, so it
    # can only match on the word "fantasy" in its title. Genre-only is too weak.
    atlas = _bk("a", "An Atlas of Fantasy", authors=["Jeremiah Benjamin Post"],
                tags=["Literary Criticism", "Science Fiction & Fantasy"])
    assert _has_recommendable_content(atlas) is False


def test_description_makes_a_book_recommendable():
    assert _has_recommendable_content(_bk("a", "T", description="A tale.")) is True


def test_whitespace_only_description_does_not_count():
    # A blank/whitespace description is no description, even with an author.
    assert _has_recommendable_content(
        _bk("a", "T", description="   ", authors=["Someone"])) is False


def test_similar_scoring_drops_descriptionless_book():
    # A source whose text contains "fantasy", a description-less "An Atlas of
    # Fantasy" (author + tags but no blurb) whose title shares that token, and a
    # real look-alike with a blurb. Without the gate the Atlas would rank on the
    # shared title token; with it, only the real match survives.
    source = _bk(
        "src", "The Crystal Sword",
        description="An orphan on an epic fantasy quest wields a crystal sword "
                    "against the necromancer king.",
        tags=["Fantasy", "Epic Fantasy"])
    atlas = _bk("atlas", "An Atlas of Fantasy",
                authors=["Jeremiah Benjamin Post"],
                tags=["Literary Criticism", "Science Fiction & Fantasy"])
    real = _bk(
        "real", "Blade of Winter",
        description="An orphan wields a crystal sword against the necromancer king.",
        tags=["Fantasy"], authors=["A. Writer"])
    ranked = [b.id for b, _ in _score_similar_candidates(source, [atlas, real])]
    assert "atlas" not in ranked
    assert ranked and ranked[0] == "real"


# ---- description weighted above genre ----------------------------------------

def test_description_outweighs_genre_in_the_blend():
    # The core R2 guard: with equal-magnitude signals, description must
    # contribute more than genre. Deterministic — fails if the weights are
    # reverted to equal (0.5/0.5) or flipped.
    desc_only = _blend_genre_desc(genre_score=0.0, desc_score=0.5, has_genres=True)
    genre_only = _blend_genre_desc(genre_score=0.5, desc_score=0.0, has_genres=True)
    assert desc_only > genre_only
    assert W_DESC > W_GENRE


def test_blend_is_description_only_without_genres():
    # A tagless candidate is judged on description alone, not dragged down by a
    # genre_score it never had a chance to earn.
    assert _blend_genre_desc(0.9, 0.3, has_genres=False) == 0.3


def test_blend_ranking_flips_at_the_weight_boundary():
    # A strong-genre/weak-description book vs an off-genre/strong-description one,
    # chosen so the ranking flips exactly at the weight boundary: under equal
    # weights the in-genre book wins, but with W_DESC > W_GENRE the description
    # match wins. This is precisely the behaviour R2 asks for.
    ingenre_weak = _blend_genre_desc(genre_score=0.6, desc_score=0.1, has_genres=True)
    offgenre_strong = _blend_genre_desc(genre_score=0.0, desc_score=0.6, has_genres=True)
    assert offgenre_strong > ingenre_weak                       # 0.36 > 0.30 at 0.4/0.6
    # Sanity: under equal 0.5/0.5 weights the in-genre book would have won.
    assert 0.5 * 0.6 + 0.5 * 0.1 > 0.5 * 0.0 + 0.5 * 0.6         # 0.35 > 0.30


# ---- _feedback_authors -------------------------------------------------------

def test_feedback_authors_squash_spelling():
    books = [_bk("a", authors=["Erin Hunter"]), _bk("b", authors=["erin  hunter"])]
    assert _feedback_authors(books) == {"erinhunter"}


def test_net_disliked_authors_cancels_conflicts():
    # The production helper library_recommend uses: an author both liked and
    # disliked is dropped from the penalty set (the like cancels the dislike),
    # while a purely-disliked author stays. Guards R1(c) via the real code path.
    liked = [_bk("l", authors=["Mixed Author"])]
    disliked = [_bk("d", authors=["Mixed Author"]), _bk("d2", authors=["Bad Author"])]
    net = _net_disliked_authors(liked, disliked)
    assert net == {"badauthor"}          # purely-disliked author penalised
    assert "mixedauthor" not in net       # liked+disliked author spared


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

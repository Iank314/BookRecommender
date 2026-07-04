"""Tests for the token-scoring tuning levers: plural folding (_fold_token /
_text_tokens) and genre-synonym folding (_genre_atoms), plus their combined
effect on cross-source similarity scoring."""

import server.app as app
from server.app import (
    _enrich_source_by_title_lookup,
    _fold_token,
    _genre_atoms,
    _genre_score,
    _score_similar_candidates,
    _text_tokens,
)
from server.models.book import Books


def _bk(id: str, title: str = "", description: str = "",
        tags: list[str] | None = None) -> Books:
    return Books(
        id=id, title=title, authors=[], description=description,
        tags=tags or [], metadata={},
    )


# ---- _fold_token -------------------------------------------------------------

def test_simple_plurals_fold():
    assert _fold_token("swords") == _fold_token("sword")
    assert _fold_token("dragons") == _fold_token("dragon")
    assert _fold_token("horses") == _fold_token("horse")


def test_ies_and_y_fold_to_same_form():
    assert _fold_token("stories") == _fold_token("story")
    assert _fold_token("fantasies") == _fold_token("fantasy")
    assert _fold_token("cities") == _fold_token("city")


def test_es_after_sibilant_folds():
    assert _fold_token("witches") == _fold_token("witch")
    assert _fold_token("kisses") == _fold_token("kiss")


def test_non_plural_s_endings_untouched():
    # -ss / -us / -is words aren't plurals; stripping would corrupt them.
    assert _fold_token("chess") == "chess"
    assert _fold_token("virus") == "virus"
    assert _fold_token("oasis") == "oasis"


def test_text_tokens_match_across_plural_forms():
    a = _text_tokens(_bk("a", description="The dragon guards ancient swords."))
    b = _text_tokens(_bk("b", description="Dragons and an ancient sword."))
    assert _fold_token("dragon") in (a & b)
    assert _fold_token("sword") in (a & b)


def test_text_tokens_drop_stopwords_after_folding():
    # "ones" folds to the stopword "one" and must not survive.
    toks = _text_tokens(_bk("a", description="The chosen ones return."))
    assert "one" not in toks and "ones" not in toks


# ---- genre synonyms ----------------------------------------------------------

def test_scifi_aliases_fold_to_science_fiction():
    for alias in ["Sci-Fi", "SciFi", "SF", "Science-Fiction"]:
        spec, _ = _genre_atoms([alias])
        assert spec == ["science fiction"], alias


def test_ol_subject_phrases_fold_to_genres():
    assert _genre_atoms(["Detective and mystery stories"])[0] == ["mystery"]
    assert _genre_atoms(["Love stories"])[0] == ["romance"]
    assert _genre_atoms(["Fantasy fiction"])[0] == ["fantasy"]
    assert _genre_atoms(["Mystery fiction"])[0] == ["mystery"]
    assert _genre_atoms(["Detective fiction"])[0] == ["mystery"]


def test_genre_noise_atoms_dropped():
    # Award/marketing labels and OL person/topic subjects survive tag-splitting
    # but aren't genres, so _genre_atoms must drop them entirely (neither
    # specific nor generic). Found via scripts/explain_similar.
    assert _genre_atoms(["New York Times bestseller"]) == ([], [])
    assert _genre_atoms(["Married people"]) == ([], [])
    assert _genre_atoms(["Husbands", "Wives"]) == ([], [])
    # A real genre alongside the noise still survives.
    assert _genre_atoms(["Fiction / Fantasy / New York Times bestseller"])[0] == ["fantasy"]


def test_marketing_label_no_longer_creates_genre_overlap():
    # Regression: a NYT-bestseller epic fantasy and a NYT-bestseller self-help
    # book shared a spurious 0.5 genre overlap via the marketing label alone,
    # which pulled the self-help book into fantasy recs (Peterson's "Beyond
    # Order" ranked #5 under "The Way of Kings"). With the label dropped they
    # share no genre at all.
    src = set(_genre_atoms(["Epic Fantasy", "New York Times bestseller"])[0])
    selfhelp = set(_genre_atoms(["Conduct of life", "New York Times bestseller"])[0])
    assert _genre_score(selfhelp, src) == 0.0


def test_synonyms_apply_inside_slash_split_tags():
    spec, generic = _genre_atoms(["Fiction / Sci-Fi / Adventure"])
    assert spec == ["science fiction", "adventure"]
    assert generic == ["fiction"]


def test_unknown_atoms_pass_through():
    assert _genre_atoms(["LitRPG"])[0] == ["litrpg"]


def test_trailing_period_stripped_before_synonym_fold():
    # OL subjects often end with a period: "Fantasy fiction." must still fold.
    assert _genre_atoms(["Fantasy fiction."])[0] == ["fantasy"]


def test_publication_boilerplate_not_tokens():
    # Sparse OL descriptions are often just publication boilerplate; these
    # tokens were the top match driver before they became stopwords.
    toks = _text_tokens(_bk(
        "a", description="First published in 2005. A New York Times bestseller."))
    assert not {"published", "york", "bestseller"} & toks


# ---- recommendation exclusions -----------------------------------------------

def test_liked_books_are_excluded_from_candidates():
    # A thumbs-up book re-weights similar candidates but must never be
    # recommended back — the user already knows it.
    saved = [_bk("s1", "Saved Book")]
    liked = [_bk("l1", "Liked Book")]
    disliked = [_bk("d1", "Disliked Book")]
    keys, titles = app._recommendation_exclusions(saved, liked, disliked)
    assert app._dedup_key(liked[0]) in keys
    assert app._norm_title("Liked Book") in titles
    assert app._dedup_key(disliked[0]) in keys
    assert app._dedup_key(saved[0]) in keys


def test_exclusion_matches_other_editions_by_title():
    # A liked book arriving from the other provider (different id, different
    # author spelling) is still excluded by its normalised title.
    liked = [_bk("l1", "The Crystal Sword!")]
    _, titles = app._recommendation_exclusions([], liked, [])
    assert app._norm_title("The Crystal Sword") in titles


# ---- title-lookup source enrichment ------------------------------------------

class _FakeFetcher:
    """Stands in for server.app.Fetcher in _enrich_source_by_title_lookup."""
    results: list[Books] = []

    def __init__(self, source=None):
        pass

    def fetch_google_page(self, query, **kwargs):
        return list(self.results), len(self.results)

    def fetch_page(self, query, **kwargs):
        return list(self.results), len(self.results)


def test_title_lookup_borrows_tags_and_description(monkeypatch):
    # The Shadow Slave case: a sparse record, but another provider carries a
    # rich edition of the same title — author spelling differs ("Guiltythree"
    # vs "Guilty Three") and must not block the match.
    _FakeFetcher.results = [_bk(
        "gb1", "Shadow Slave",
        description="Sunny is chosen by the Nightmare Spell and must survive "
                    "the Dream Realm where ancient horrors hunt the awakened.",
        tags=["Fantasy", "LitRPG"],
    )]
    _FakeFetcher.results[0].authors = ["Guiltythree"]
    monkeypatch.setattr(app, "Fetcher", _FakeFetcher)

    source = _bk("src", "Shadow Slave Book 1", description="First edition. 2023.")
    source.authors = ["Guilty Three"]
    _enrich_source_by_title_lookup(source)
    assert source.tags == ["Fantasy", "LitRPG"]
    assert "Nightmare Spell" in source.description


def test_title_lookup_rejects_different_author(monkeypatch):
    # Same title, different book (the D. I. Telbat "Shadow Slave") — must not
    # borrow a stranger's genres.
    _FakeFetcher.results = [_bk(
        "gb1", "Shadow Slave",
        description="A Christian thriller about a covert missionary on the run "
                    "from his past across hostile borders and distant lands.",
        tags=["Christian Fiction"],
    )]
    _FakeFetcher.results[0].authors = ["D. I. Telbat"]
    monkeypatch.setattr(app, "Fetcher", _FakeFetcher)

    source = _bk("src", "Shadow Slave", description="First edition. 2023.")
    source.authors = ["Guiltythree"]
    _enrich_source_by_title_lookup(source)
    assert source.tags == []
    assert source.description == "First edition. 2023."


# ---- end-to-end: cross-source genre vocab now overlaps ------------------------

def test_gb_and_ol_genre_vocab_score_overlap():
    # A Google Books-style source ("Science Fiction") and an Open Library-style
    # candidate ("sci-fi") used to score zero genre overlap. With synonym
    # folding the in-genre candidate must outrank the off-genre one.
    source = _bk(
        "src", "Star Wreck",
        description="A starship crew explores derelict alien stations beyond the rim.",
        tags=["Fiction / Science Fiction"],
    )
    ol_style = _bk(
        "ol", "Void Runners",
        description="A starship crew explores a derelict alien station.",
        tags=["sci-fi"],
    )
    off_genre = _bk(
        "off", "Station Gardens",
        description="A starship crew explores a derelict alien station.",
        tags=["gardening"],
    )
    scored = _score_similar_candidates(source, [off_genre, ol_style])
    ranked = [b.id for b, _ in scored]
    assert ranked[0] == "ol"

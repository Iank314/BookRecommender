"""Tests for the display-layer helpers in server.app:
    _split_series — including written-out volume words
    _clean_tags_for_display — facet stripping, genre ranking, derived fallback
    _book_language — title script trumps metadata
    _desc_is_sequel — sequel detection from description text
"""

import pytest

from server.app import (
    _book_language,
    _clean_tags_for_display,
    _desc_is_sequel,
    _split_series,
    _to_out,
)
from server.models.book import Books


def _bk(title: str = "", description: str = "", metadata: dict | None = None) -> Books:
    return Books(
        id="t", title=title, authors=[], description=description,
        tags=[], metadata=metadata or {},
    )


# ---- _split_series ---------------------------------------------------------

def test_split_series_digit_volume():
    assert _split_series("He Who Fights with Monsters 11") == (
        "he who fights with monsters", 11,
    )


def test_split_series_volume_range_picks_low_end():
    assert _split_series("Last Wish System, Vol. 1-18") == (
        "last wish system", 1,
    )


def test_split_series_book_three_word():
    # Bug fix: the Code of Survival case — "Book Three" had been treated as
    # a standalone title because the regex only matched digits.
    assert _split_series("The Code of Survival Book Three") == (
        "the code of survival", 3,
    )


def test_split_series_volume_five_word():
    assert _split_series("Saga of the Forgotten Volume Five") == (
        "saga of the forgotten", 5,
    )


def test_split_series_word_requires_volume_keyword():
    # "The Three Musketeers" must NOT parse as Book Three — without a
    # vol/book/part keyword we don't treat trailing number-words as volumes.
    skey, vol = _split_series("The Three Musketeers")
    assert vol is None


def test_split_series_pure_number_title_stays_standalone():
    # "1984" / "2001" — the guard against using the digit as a volume.
    _, vol = _split_series("1984")
    assert vol is None


def test_split_series_no_trailing_number():
    assert _split_series("Mistborn: The Final Empire") == (
        "mistborn the final empire", None,
    )


# ---- _clean_tags_for_display ----------------------------------------------

def test_clean_drops_series_facet():
    out = _clean_tags_for_display(
        ["series:Dungeon Crawler Carl", "genre:LitRPG", "genre:science fantasy"],
        description="", title="",
    )
    # series: facet entirely gone; genre: prefix stripped on the rest
    assert out == ["LitRPG", "science fantasy"]


def test_clean_ranks_genre_above_series_name():
    # Real genres should sort to the front so the frontend's tags[0] grouping
    # picks "Science Fiction", not the series name.
    out = _clean_tags_for_display(
        ["Trilogy of Four", "fantasy fiction", "Fiction", "Interplanetary voyages", "Science Fiction"],
        description="", title="",
    )
    # Specific genres come before generic "Fiction" come before series-named "Trilogy of Four"
    assert out[0] in {"fantasy fiction", "Science Fiction"}
    assert out[-1] == "Trilogy of Four"


def test_clean_drops_person_place_facets():
    out = _clean_tags_for_display(
        ["person:Sherlock Holmes", "place:London", "Mystery"],
        description="", title="",
    )
    assert out == ["Mystery"]


def test_clean_keeps_other_facet_prefixes():
    # subject: is a kept facet — same as genre:.
    out = _clean_tags_for_display(
        ["subject:Cyberpunk", "Adventure"],
        description="", title="",
    )
    assert "Cyberpunk" in out
    assert "Adventure" in out


def test_clean_keeps_form_facet_value():
    # Open Library "form:" subjects (manga/graphic novels) carry a real genre
    # once the prefix is stripped — keep the value, drop the "form:" noise.
    out = _clean_tags_for_display(
        ["form:manga", "form:graphic novel"],
        description="", title="",
    )
    assert "manga" in out
    assert "graphic novel" in out
    assert not any(t.startswith("form:") for t in out)


def test_clean_drops_franchise_and_nyt_facets():
    # "franchise:One Piece" is a series/franchise name, not a genre, and the
    # "nyt:advice-how-to...=2021-03-21" bestseller-list slugs are pure noise —
    # both must not become the group header.
    out = _clean_tags_for_display(
        ["franchise:One Piece", "nyt:advice-how-to-and-miscellaneous=2021-03-21",
         "Fantasy"],
        description="", title="",
    )
    assert out == ["Fantasy"]


def test_clean_dedupes_case_insensitive():
    out = _clean_tags_for_display(
        ["Fantasy", "fantasy", "FANTASY"],
        description="", title="",
    )
    assert out == ["Fantasy"]


def test_derives_litrpg_from_subtitle_when_tagless():
    # "Drone Captain - A Sci-fi LitRPG" — no tags but the title/description
    # name the genre. LitRPG wins over Science Fiction because it's more
    # specific (listed first in _DERIVED_GENRES).
    out = _clean_tags_for_display(
        tags=[],
        description="A Sci-fi LitRPG: Drone Rising 2",
        title="Drone Captain",
    )
    assert out and out[0] == "LitRPG"


def test_derives_science_fiction_from_description():
    out = _clean_tags_for_display(
        tags=[],
        description="A sweeping space opera with vivid sci-fi action.",
        title="",
    )
    # Space Opera is more specific than Science Fiction; should win.
    assert out and out[0] == "Space Opera"


def test_no_tags_no_match_returns_empty():
    # Empty tags + a description with nothing genre-shaped → caller falls
    # back to "Uncategorized" in the UI. No false positives.
    out = _clean_tags_for_display(
        tags=[],
        description="A book about feelings and the passage of time.",
        title="Some Book",
    )
    assert out == []


def test_derived_genre_only_prepended_when_first_tag_is_weak():
    # If we already have a real-genre tag, don't shove a derived one in front.
    out = _clean_tags_for_display(
        tags=["Science Fiction"],
        description="A sci-fi novel about robots.",
        title="",
    )
    assert out == ["Science Fiction"]


# ---- _to_out relevance clamp ----------------------------------------------

def _rel_bk() -> Books:
    return Books(id="x", title="T", authors=[], description="", tags=[],
                 metadata={})


def test_to_out_clamps_relevance_over_100():
    # The 114% bug: /library/recommend's feedback modifier (up to 1.5x) can
    # push the ranking score past 1.0, so score*100 exceeds 100. A user should
    # never see "114%".
    assert _to_out(_rel_bk(), relevance=114.0)["relevance"] == 100.0
    # /similar's popularity multiplier can nudge it just over, too.
    assert _to_out(_rel_bk(), relevance=104.7)["relevance"] == 100.0


def test_to_out_leaves_normal_relevance_untouched():
    assert _to_out(_rel_bk(), relevance=87.3)["relevance"] == 87.3


def test_to_out_omits_relevance_when_none():
    assert "relevance" not in _to_out(_rel_bk())


# ---- _book_language --------------------------------------------------------

def test_book_language_japanese_title_overrides_metadata_lang_en():
    # The real bug: Google Books labels "鋼の錬金術師 1" with language="en"
    # because the description is in English ("First published in 2005. | By ...").
    # Trusting metadata over the title script let it through the all-English
    # library filter. Title script is now dispositive.
    book = _bk(
        title="鋼の錬金術師 1",
        description="First published in 2005. | By 荒川弘.",
        metadata={"language": "en"},
    )
    assert _book_language(book) == "ja"


def test_book_language_cyrillic_title_overrides_metadata_lang_en():
    book = _bk(title="Война и мир", metadata={"language": "en"})
    assert _book_language(book) == "ru"


def test_book_language_latin_title_uses_metadata_when_present():
    # Reverse case: an English-titled translation of a Russian work whose
    # metadata says "ru" should still report "ru". We only override when the
    # title script clearly contradicts metadata.
    book = _bk(title="War and Peace", metadata={"language": "ru"})
    assert _book_language(book) == "ru"


def test_book_language_latin_title_no_metadata_defaults_english():
    book = _bk(title="Mistborn: The Final Empire")
    assert _book_language(book) == "en"


def test_book_language_empty_input_defaults_english():
    assert _book_language(_bk()) == "en"


# ---- _desc_is_sequel -------------------------------------------------------

def test_desc_sequel_the_fourth_book():
    # Adams case — title carries no marker, description names the position.
    assert _desc_is_sequel(
        "Arthur Dent gets home to Earth in this, the fourth book in the Hitchhiker's Trilogy."
    )


def test_desc_sequel_book_three_phrase():
    assert _desc_is_sequel("Book Three of the Saga continues the journey.")


def test_desc_sequel_numeric_book_4():
    assert _desc_is_sequel("book 4 of the Foundation cycle")


def test_desc_sequel_fourth_and_final_book():
    # Filler words between ordinal and noun still match.
    assert _desc_is_sequel("the fourth and final book in this trilogy")


def test_desc_sequel_last_book_in_the_series():
    # Erin Hunter "A Dangerous Path" case — "last" doesn't tell us a specific
    # position, but it does tell us this isn't book 1.
    assert _desc_is_sequel("the last book in this thrilling series")


def test_desc_sequel_final_installment():
    assert _desc_is_sequel("the final installment of the trilogy")


def test_desc_sequel_concluding_novel():
    assert _desc_is_sequel("the concluding novel in this saga")


def test_desc_sequel_first_intentionally_ignored():
    # "First" / "1" describe entry points, which we want to KEEP.
    assert not _desc_is_sequel("the first book in the series")


def test_desc_sequel_no_volume_noun_no_match():
    # "the fourth time" / "the fourth chapter" / "her final goodbye" —
    # without book/novel/volume/etc. we don't flag it.
    assert not _desc_is_sequel("the fourth time he met her")
    assert not _desc_is_sequel("the fourth chapter of his life")
    assert not _desc_is_sequel("her final goodbye to her childhood home")


def test_desc_sequel_returns_false_for_standalone_description():
    assert not _desc_is_sequel("A young farm boy discovers he is the chosen one.")


def test_desc_sequel_empty_input():
    assert not _desc_is_sequel("")
    assert not _desc_is_sequel(None)  # type: ignore[arg-type]


# ---- Latin-script foreign editions ------------------------------------------
#
# Script detection above catches a Japanese title. It cannot catch a Spanish
# one, and Open Library's `language` field is the union of EVERY edition
# language for a work -- _from_openlib_doc resolves it to "eng" the moment any
# English edition exists. So a Spanish record for a translated book claims
# English and sailed straight through the English gate.
#
# Measured across three real candidate pools: 115 non-English-titled records,
# every one labelled `eng`. "Alas de ónix" reached #1 on Find Similar for both
# Mistborn and The Hobbit in production. Its Open Library description is in
# English too, so the title is the only per-record evidence available.

from server.app import (  # noqa: E402
    NON_ENGLISH_LANG, _title_looks_non_english,
)


@pytest.mark.parametrize("title", [
    "Alas de ónix", "Das Parfum", "Les trois Mousquetaires", "El Jarama",
    "De aanslag", "Il cimitero di Praga", "Die Vermessung der Welt",
    "La fortune des Rougon", "Suite française", "Mémoires d'Hadrien",
    "Röde Orm", "Ségou", "Le petit prince", "Il nome della rosa",
    "Hrabě Monte Cristo. Díl 1 a 2",
])
def test_foreign_titles_are_detected(title):
    assert _title_looks_non_english(title) is True


@pytest.mark.parametrize("title,authors", [
    # An English function word settles it, even with a foreign name in the
    # title -- this one is a real pooled record and the reason the English
    # check runs first rather than counting markers on both sides.
    ("The Thousand Autumns of Jacob de Zoet", ["David Mitchell"]),
    ("A Game of Thrones", ["George R. R. Martin"]),
    ("A Christmas Carol", ["Charles Dickens"]),
    # No markers at all -> defaults to English, which is right for this corpus.
    ("Dune", ["Frank Herbert"]),
    ("Mistborn", ["Brandon Sanderson"]),
    ("Steelheart", ["Brandon Sanderson"]),
    ("Frankenstein", ["Mary Shelley"]),
    # Name particles are not language evidence. "Le Guin Reader" was the only
    # false positive across 3113 pooled titles before authors were consulted.
    ("Le Guin Reader", ["Ursula K. Le Guin"]),
    ("A Perfect Spy", ["John le Carre"]),
])
def test_english_titles_survive(title, authors):
    assert _title_looks_non_english(title, authors) is False


def test_open_library_eng_claim_loses_to_a_spanish_title():
    # The exact production record: a Spanish edition whose work has English
    # editions, so OL reports language 'eng'.
    book = _bk(
        title="Alas de ónix",
        description="After nearly eighteen months at Basgiath War College, "
                    "Violet Sorrengail knows there's no more time for lessons.",
        metadata={"language": "eng"},
    )
    book.authors = ["Rebecca Yarros"]
    assert _book_language(book) == NON_ENGLISH_LANG


def test_a_specific_non_english_claim_is_still_trusted():
    # Only the "en" claim is fabricated by the union-of-editions logic; a
    # provider that says "spa" means it, and that is more precise than the
    # sentinel, so it must not be overwritten.
    book = _bk(title="Alas de ónix", metadata={"language": "spa"})
    book.authors = ["Rebecca Yarros"]
    assert _book_language(book) == "es"


def test_an_english_title_still_reports_english():
    book = _bk(title="The Way of Kings", metadata={"language": "eng"})
    book.authors = ["Brandon Sanderson"]
    assert _book_language(book) == "en"


def test_a_foreign_edition_is_excluded_from_an_english_library():
    from server.app import _apply_language_gate

    english = [_bk(title=f"The Long Road {i}", metadata={"language": "eng"})
               for i in range(20)]
    foreign = _bk(title="Alas de ónix", metadata={"language": "eng"})
    foreign.authors = ["Rebecca Yarros"]

    kept = _apply_language_gate([*english, foreign], {"en"})

    assert foreign not in kept
    assert len(kept) == len(english)

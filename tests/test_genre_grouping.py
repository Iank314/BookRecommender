"""Regression tests for the Max_Ku bugs (June 2026): four Harry Potter books
grouped under four junk headers ("the Elder Wand" is not a genre), and an
all-HP library getting zero recommendations because Open Library's language
list handed us a random translation language ('ben') per book, which the
language gate then used to annihilate every English candidate."""

from server.app import (
    _apply_language_gate,
    _clean_tags_for_display,
    _norm_lang,
    _tag_display_score,
)
from server.fetcher.fetcher import Fetcher
from server.models.book import Books


def _bk(id: str, title: str = "T", lang: str | None = None) -> Books:
    return Books(id=id, title=title, authors=[], description="", tags=[],
                 metadata={"language": lang} if lang else {})


# ---- entity tags are not genres -----------------------------------------------

def test_entity_tags_rank_below_everything():
    assert _tag_display_score("the Elder Wand") == -1
    assert _tag_display_score("Dementors (Imaginary creatures)") == -1
    assert _tag_display_score("Hermione Granger (Fictitious character)") == -1
    # ...but real genres starting with other words are untouched.
    assert _tag_display_score("Science Fiction") == 3


def test_deathly_hallows_tags_derive_childrens_fiction():
    # Real tags from Max_Ku's copy. "the Elder Wand" must not be the header.
    tags = ["the Elder Wand", "children's books", "dementors",
            "good and evil", "Juvenile literature"]
    display = _clean_tags_for_display(tags, "Harry leaves Privet Drive.", "HP7")
    assert display[0] == "Children's Fiction"
    assert "the Elder Wand" != display[0]


def test_witchy_subject_tags_derive_paranormal():
    # Philosopher's Stone's real tags: entities, not genres — but they signal one.
    tags = ["Ghosts", "Monsters", "Vampires", "Witches"]
    display = _clean_tags_for_display(tags, "A letter arrives by owl.", "HP1")
    assert display[0] == "Paranormal"


def test_witchcraft_in_description_derives_fantasy():
    display = _clean_tags_for_display(
        ["orphans", "foster homes"],
        "His fourth year at Hogwarts School of Witchcraft and Wizardry.",
        "HP4",
    )
    assert display[0] == "Fantasy"


def test_magic_subject_tags_derive_fantasy():
    # Real "Shadows of Self" (Mistborn) tags: OL gives only story-content
    # descriptors, none of which is a recognised genre word — so the book was
    # landing under "Domestic Terrorism" instead of Fantasy. The "magic" /
    # "imaginary places" signals must pull it into Fantasy.
    tags = ["domestic terrorism", "imaginary places", "magic",
            "religious disputations"]
    display = _clean_tags_for_display(tags, "Kelsier returns to the city.", "SoS")
    assert display[0] == "Fantasy"


def test_spaceship_content_derives_science_fiction_when_tagless():
    # A tagless result whose description is unmistakably SF must not fall to the
    # "Other" bucket.
    display = _clean_tags_for_display(
        [],
        "A lone starship drifts past a dead galaxy; the last android aboard wakes.",
        "Derelict",
    )
    assert display and display[0] == "Science Fiction"


# ---- language pipeline ---------------------------------------------------------

def test_ol_language_prefers_english_when_present():
    doc = {"key": "/works/OL1W", "title": "Harry Potter",
           "language": ["ben", "cze", "eng", "est"]}
    book = Fetcher._from_openlib_doc(doc)
    assert book.metadata["language"] == "eng"


def test_ol_single_language_kept():
    doc = {"key": "/works/OL1W", "title": "Babička", "language": ["cze"]}
    assert Fetcher._from_openlib_doc(doc).metadata["language"] == "cze"


def test_norm_lang_new_aliases():
    assert _norm_lang("cze") == "cs"
    assert _norm_lang("ben") == "bn"
    assert _norm_lang("est") == "et"


def test_language_gate_filters_when_plausible():
    pool = [_bk(f"e{i}", lang="en") for i in range(20)] + \
           [_bk(f"r{i}", lang="ru") for i in range(5)]
    kept = _apply_language_gate(pool, {"en"})
    assert len(kept) == 20 and all(b.id.startswith("e") for b in kept)


def test_language_gate_bails_out_when_it_would_annihilate_the_pool():
    # The Max_Ku case: junk library languages match ~nothing — the gate must
    # conclude the metadata is wrong and keep the pool.
    pool = [_bk(f"e{i}", lang="en") for i in range(100)]
    kept = _apply_language_gate(pool, {"bn", "cs", "et"})
    assert len(kept) == 100


def test_language_gate_no_langs_is_noop():
    pool = [_bk("a", lang="en")]
    assert _apply_language_gate(pool, set()) == pool

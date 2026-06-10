"""Username moderation: reject slurs and strong profanity at registration.

Matching is substring-based over a normalized form (lowercased, separators
stripped, common leetspeak folded), so "B1g_N!gger" and "f.u.c.k" are caught,
not just verbatim spellings. Known tradeoff: substring matching has rare
false positives (the classic "Scunthorpe problem") — for usernames on a small
site, occasionally asking someone to pick a different name is the right side
of that tradeoff. The list deliberately omits high-collision fragments
("ass" → class/bass, "dick" → Dickens, "coon" → raccoon/tycoon).
"""

from __future__ import annotations

# Leetspeak / obfuscation folding applied before matching.
_LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "8": "b", "@": "a", "$": "s", "!": "i", "+": "t",
})

# Checked as substrings of the normalized username.
_BANNED_SUBSTRINGS = (
    # Slurs
    "nigger", "nigga", "faggot", "kike", "wetback", "beaner", "towelhead",
    "tranny", "chink", "dyke", "raghead", "gypsy",
    # Strong profanity / abuse
    "fuck", "shit", "bitch", "cunt", "whore", "slut", "cocksucker",
    "asshole", "dickhead", "retard", "rapist", "pedo",
    # Hate figures
    "hitler", "nazi", "kkk",
)


def _normalize(username: str) -> str:
    folded = username.lower().translate(_LEET)
    return "".join(ch for ch in folded if ch.isalpha())


def username_is_clean(username: str) -> bool:
    """False when the username contains a banned term (after normalization)."""
    normalized = _normalize(username)
    return not any(term in normalized for term in _BANNED_SUBSTRINGS)

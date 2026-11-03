"""Text normalisation used for all fuzzy comparisons."""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]|_")


def norm(s: str | None) -> str:
    """Normalise: NFKD, drop accents, casefold, & -> and, drop . and ', punctuation -> space."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().replace("&", " and ")
    s = s.replace(".", "").replace("'", "").replace("’", "")
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split())


def contains_words(haystack_norm: str, needle_norm: str) -> bool:
    """Whole-word containment on already-normalised strings."""
    if not needle_norm:
        return False
    return f" {needle_norm} " in f" {haystack_norm} "

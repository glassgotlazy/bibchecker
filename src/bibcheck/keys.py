"""Suggesting citation keys.

Used by ``bibcheck cite`` to name a freshly fetched entry, and available for
reporting a key that does not follow the bibliography's own convention.
"""

from __future__ import annotations

import re
from typing import Final, Iterable

from .latex import strip_accents
from .models import Metadata

__all__ = ["suggest_key", "unique_key"]

#: Title words too common to identify anything.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "an", "the", "of", "on", "in", "for", "and", "or", "to", "with",
        "using", "via", "towards", "toward", "into", "from", "by", "at", "as",
        "is", "are", "new", "novel", "improved", "efficient", "learning",
        "deep", "approach", "method", "study", "analysis", "based",
    }
)


def _clean(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(text).casefold())


def suggest_key(metadata: Metadata) -> str:
    """Build a ``surnameYEARword`` key, the near-universal convention.

    Falls back gracefully: no author gives ``YEARword``, no year gives
    ``surnameword``, and nothing at all gives ``reference``.
    """
    surname = ""
    if metadata.authors:
        surname = _clean(metadata.authors[0].family)[:20]

    year = str(metadata.year) if metadata.year else ""

    word = ""
    if metadata.title:
        for candidate in re.split(r"[\s:–—-]+", strip_accents(metadata.title)):
            cleaned = _clean(candidate)
            # A distinctive word beats the first word: "deep" and "learning"
            # identify nothing, and half a bibliography would collide.
            if len(cleaned) > 2 and cleaned not in _STOPWORDS:
                word = cleaned[:14]
                break

    key = f"{surname}{year}{word}"
    return key or "reference"


def unique_key(base: str, taken: Iterable[str]) -> str:
    """Disambiguate against keys already in use, the way BibTeX does: a, b, c."""
    existing = set(taken)
    if base not in existing:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{base}{suffix}"
        if candidate not in existing:
            return candidate
    index = 2
    while f"{base}{index}" in existing:
        index += 1
    return f"{base}{index}"

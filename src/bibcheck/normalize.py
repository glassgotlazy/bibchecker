"""Comparison keys and similarity scoring.

The rule this module enforces: a value is compared through a *key*, never
directly. :func:`title_key` and friends strip everything cosmetic -- case,
punctuation, accents, LaTeX braces, "&" versus "and", articles, whitespace --
so that a difference that survives is a real difference worth reporting.
The original text is kept alongside so the report can show both sides verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final

from .latex import latex_to_unicode, strip_accents

__all__ = [
    "PageRange",
    "normalize_text",
    "title_key",
    "venue_key",
    "similarity",
    "titles_match",
    "parse_year",
    "parse_pages",
    "parse_volume",
    "looks_abbreviated",
]

# Words dropped from a comparison key: they carry no discriminating signal in a
# title and are exactly what differs between a publisher record and a hand-typed
# bib entry.
_STOPWORDS: Final[frozenset[str]] = frozenset({"a", "an", "the"})

_AMPERSAND_RE: Final[re.Pattern[str]] = re.compile(r"\s*&\s*")
_NONWORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"(1[6-9]\d{2}|20\d{2}|21\d{2})")
_VOLUME_RE: Final[re.Pattern[str]] = re.compile(r"(\d+)")
_PAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<start>[A-Za-z]?\d+(?:\.\d+)?)"
    r"(?:\s*(?:-{1,3}|–|—|‒|to)\s*(?P<end>[A-Za-z]?\d+(?:\.\d+)?))?\s*$"
)
# "Proc." / "Trans." / "Int'l" -- a trailing period on a shortened word.
_ABBREV_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Za-z]{1,6}\.(?!\w)")


@dataclass(frozen=True, slots=True)
class PageRange:
    """A parsed ``pages`` field. ``end`` is ``None`` for single-page items."""

    start: str
    end: str | None

    @property
    def key(self) -> str:
        return self.start if self.end is None else f"{self.start}-{self.end}"

    def __str__(self) -> str:
        return self.key


def normalize_text(value: str) -> str:
    """Render a raw BibTeX value as display text (LaTeX resolved, spaces tidy)."""
    return latex_to_unicode(value)


def _fold(value: str, *, drop_stopwords: bool) -> str:
    text = latex_to_unicode(value)
    text = _AMPERSAND_RE.sub(" and ", text)
    text = strip_accents(text).casefold()
    # Hyphens and slashes join words that the other source may have split.
    text = re.sub(r"[-/‐-―]", " ", text)
    text = _NONWORD_RE.sub(" ", text)
    tokens = _WS_RE.sub(" ", text).strip().split()
    if drop_stopwords:
        tokens = [t for t in tokens if t not in _STOPWORDS]
    return " ".join(tokens)


def title_key(value: str) -> str:
    """Canonical comparison key for a title.

    ``Caf\\'{e}s: A {Study}`` and ``Cafés - a study`` collapse to the same key,
    so only a substantive difference is ever reported as drift.
    """
    return _fold(value, drop_stopwords=True)


def venue_key(value: str) -> str:
    """Comparison key for a journal or conference name.

    Stopwords are kept here -- venue names are short enough that dropping them
    loses signal, and "Proceedings of the" appears or not on both sides equally.
    """
    return _fold(value, drop_stopwords=False)


def similarity(left: str, right: str) -> float:
    """Similarity of two already-keyed strings in ``[0.0, 1.0]``.

    Combines a token-set ratio (robust to reordering and to one side carrying a
    subtitle) with a character-level ratio (robust to tokenisation noise).
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens)
    token_ratio = (2 * overlap) / (len(left_tokens) + len(right_tokens))
    char_ratio = SequenceMatcher(None, left, right).ratio()
    return max(token_ratio, char_ratio)


def titles_match(local: str, authoritative: str, *, threshold: float = 0.95) -> bool:
    """True when two titles differ only cosmetically."""
    left, right = title_key(local), title_key(authoritative)
    if left == right:
        return True
    # A record whose title is a strict prefix of the other is the common
    # "subtitle present on one side only" case, not a wrong citation.
    if left and right and (left.startswith(right) or right.startswith(left)):
        return True
    return similarity(left, right) >= threshold


def parse_year(value: str | None) -> int | None:
    """Extract a four-digit year from ``2020``, ``{2020}``, ``2020-06`` or ``Jan 2020``."""
    if value is None:
        return None
    match = _YEAR_RE.search(latex_to_unicode(value))
    return int(match.group(1)) if match else None


def parse_volume(value: str | None) -> str | None:
    """Extract a volume number, tolerating ``vol. 12`` and ``{12}``."""
    if value is None:
        return None
    text = latex_to_unicode(value)
    match = _VOLUME_RE.search(text)
    if match:
        return match.group(1)
    stripped = text.strip()
    return stripped or None


def parse_pages(value: str | None) -> PageRange | None:
    """Parse ``10--20``, ``10-20``, ``10 to 20``, ``e1234`` or ``42``.

    Returns ``None`` when the field is absent or not a recognisable range, so a
    weird value degrades to "cannot compare" rather than to a false mismatch.
    """
    if value is None:
        return None
    text = latex_to_unicode(value).replace(",", "")
    match = _PAGE_RE.match(text)
    if match is None:
        return None
    start = match.group("start")
    end = match.group("end")
    if end is not None and len(end) < len(start) and start[-len(end):] != end:
        # "1234--56" is the elided form of "1234--1256".
        end = start[: len(start) - len(end)] + end
    return PageRange(start=start, end=end)


def looks_abbreviated(value: str) -> bool:
    """Heuristic: does this venue name use shortened words (``IEEE Trans. Ind.``)?

    Used to soften venue-drift reporting -- an abbreviated local name versus a
    spelled-out authoritative one is a style choice, not an error.
    """
    return bool(_ABBREV_RE.search(latex_to_unicode(value)))

"""Author name parsing and comparison.

Author lists are the noisiest field in any bibliography: ``J. Smith``,
``John Smith``, ``Smith, John A.`` and ``SMITH, J.`` are the same person, while
``Smith`` and ``Schmidt`` are not. Comparison therefore runs on surname plus
first-given-initial, ASCII-folded -- strict enough to catch a wrong author,
loose enough to ignore how the name was typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .latex import latex_to_unicode, strip_accents

__all__ = ["PersonName", "parse_name", "split_authors", "parse_author_field", "authors_match"]

# Lowercase particles that belong to the surname: "van der Berg", "de la Cruz".
_PARTICLES: Final[frozenset[str]] = frozenset(
    {
        "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos",
        "das", "du", "la", "le", "el", "al", "bin", "ibn", "ben", "ter", "ten",
        "af", "av", "zu", "vom", "op", "st",
    }
)
# Generational and honorific suffixes, which are not part of the surname.
_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "esq"}
)
_AND_RE: Final[re.Pattern[str]] = re.compile(r"\s+and\s+", re.IGNORECASE)
_OTHERS: Final[frozenset[str]] = frozenset({"others", "et al", "et al."})


@dataclass(frozen=True, slots=True)
class PersonName:
    """One author. ``display`` is verbatim; the other fields drive comparison."""

    display: str
    family: str
    given: str = ""
    suffix: str = ""

    @property
    def family_key(self) -> str:
        """ASCII-folded, lowercased surname -- the primary comparison handle."""
        return re.sub(r"[^a-z]", "", strip_accents(self.family).casefold())

    @property
    def given_initial(self) -> str:
        """First initial of the given name, or ``""`` when no given name is known."""
        for char in strip_accents(self.given):
            if char.isalpha():
                return char.casefold()
        return ""

    @property
    def key(self) -> str:
        return f"{self.family_key}:{self.given_initial}"

    def __str__(self) -> str:
        return self.display


def _split_top_level_and(value: str) -> list[str]:
    """Split on ``and`` at brace depth zero.

    ``{Smith and Wesson Ltd.}`` is one corporate author, not two people, so the
    split has to respect braces rather than blindly splitting the string.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if depth == 0:
            match = _AND_RE.match(value, index)
            if match is not None:
                parts.append("".join(current))
                current = []
                index = match.end()
                continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def split_authors(value: str) -> list[str]:
    """Split a raw BibTeX author/editor field into individual name strings."""
    return _split_top_level_and(value)


def parse_name(value: str) -> PersonName:
    """Parse one BibTeX name into family/given/suffix parts.

    Handles the three BibTeX spellings -- ``Given Family``, ``Family, Given``
    and ``Family, Suffix, Given`` -- plus lowercase particles and braced
    corporate names, which are treated as an indivisible family name.
    """
    display = latex_to_unicode(value).strip()
    raw = value.strip()

    # A fully braced name is a corporate author: keep it whole.
    if raw.startswith("{") and raw.endswith("}") and raw.count("{") == 1:
        return PersonName(display=display, family=display, given="")

    segments = [segment.strip() for segment in display.split(",")]
    if len(segments) == 2 and segments[1].rstrip(".").casefold() in _SUFFIXES:
        # "Martin Luther King, Jr." -- strict BibTeX would read "Jr." as the
        # given name, which is never what the author meant. Re-read the first
        # segment as a plain "Given Family" name and keep the suffix aside.
        head = parse_name(segments[0])
        return PersonName(
            display=display,
            family=head.family,
            given=head.given,
            suffix=segments[1],
        )
    if len(segments) >= 2:
        family = segments[0]
        suffix = ""
        given = segments[-1]
        if len(segments) >= 3 and segments[1].rstrip(".").casefold() in _SUFFIXES:
            suffix = segments[1]
        return PersonName(display=display, family=family, given=given, suffix=suffix)

    tokens = display.split()
    if not tokens:
        return PersonName(display=display, family=display, given="")
    if len(tokens) == 1:
        return PersonName(display=display, family=tokens[0], given="")

    suffix = ""
    if tokens[-1].rstrip(".").casefold() in _SUFFIXES and len(tokens) > 2:
        suffix = tokens[-1]
        tokens = tokens[:-1]

    # Walk back from the end over particles so "Ludwig van Beethoven" yields the
    # family name "van Beethoven" rather than "Beethoven".
    split_at = len(tokens) - 1
    while split_at > 1 and tokens[split_at - 1].casefold().rstrip(".") in _PARTICLES:
        split_at -= 1

    return PersonName(
        display=display,
        family=" ".join(tokens[split_at:]),
        given=" ".join(tokens[:split_at]),
        suffix=suffix,
    )


def parse_author_field(value: str | None) -> tuple[PersonName, ...]:
    """Parse a whole ``author =`` field. ``and others`` is dropped."""
    if value is None:
        return ()
    names = [
        parse_name(part)
        for part in split_authors(value)
        if latex_to_unicode(part).strip().casefold() not in _OTHERS
    ]
    return tuple(name for name in names if name.family_key or name.display)


def authors_match(
    local: tuple[PersonName, ...],
    authoritative: tuple[PersonName, ...],
    *,
    require_order: bool = True,
) -> bool:
    """Do two author lists describe the same people in the same order?

    A given initial present on only one side is not evidence of a mismatch, so
    ``J. Smith`` matches ``John Smith`` while ``A. Smith`` does not. A truncated
    local list (the ``et al.`` case) matches a prefix of the full list.
    """
    if not local or not authoritative:
        return False
    if len(local) > len(authoritative):
        return False

    if not require_order:
        remaining = list(authoritative)
        for candidate in local:
            for index, other in enumerate(remaining):
                if _person_matches(candidate, other):
                    del remaining[index]
                    break
            else:
                return False
        return True

    return all(
        _person_matches(candidate, other)
        for candidate, other in zip(local, authoritative, strict=False)
    )


def _person_matches(left: PersonName, right: PersonName) -> bool:
    if left.family_key != right.family_key:
        return False
    left_initial, right_initial = left.given_initial, right.given_initial
    if not left_initial or not right_initial:
        return True
    return left_initial == right_initial

"""Style and hygiene checks -- the problems a lookup can never find.

Everything here is *local*: no network, no registry, just the bibliography as
written. These are the things a reviewer notices and a style file punishes, and
they are the reason a bibliography can be entirely factually correct and still
render badly.

The headline one is case protection. ``title = {BERT: pre-training of deep
bidirectional transformers}`` is *correct*, resolves fine, and drifts from
nothing -- and IEEEtran will typeset it as "Bert: pre-training...". The author
finds out at proof stage. A brace around the acronym is the fix, and nothing
else in the pipeline will ever tell them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable, Mapping, Sequence

from .models import BibEntry, Code, EntryKind, Problem, Severity
from .names import PersonName, parse_author_field
from .latex import latex_to_unicode
from .normalize import venue_key

__all__ = [
    "check_case_protection",
    "check_style_fields",
    "check_pages_sanity",
    "check_url_entry",
    "find_inconsistent_venues",
    "find_inconsistent_authors",
    "local_checks",
    "bibliography_style_checks",
    "STYLES",
]


@dataclass(frozen=True, slots=True)
class Style:
    """What a citation style insists on, per entry type."""

    name: str
    required: Mapping[str, tuple[str, ...]]
    recommended: Mapping[str, tuple[str, ...]]


#: IEEE is the default because it is the strictest of the common styles about
#: venue, volume and pages, so satisfying it satisfies most others.
_IEEE = Style(
    name="ieee",
    required={
        "article": ("author", "title", "journal", "year"),
        "inproceedings": ("author", "title", "booktitle", "year"),
        "incollection": ("author", "title", "booktitle", "publisher", "year"),
        "book": ("title", "publisher", "year"),
        "techreport": ("author", "title", "institution", "year"),
        "phdthesis": ("author", "title", "school", "year"),
        "mastersthesis": ("author", "title", "school", "year"),
        "misc": ("title",),
    },
    recommended={
        "article": ("volume", "number", "pages"),
        "inproceedings": ("pages",),
        "book": ("author", "edition"),
        "techreport": ("number",),
        "misc": ("howpublished", "url"),
    },
)

_ACM = Style(
    name="acm",
    required={
        "article": ("author", "title", "journal", "year"),
        "inproceedings": ("author", "title", "booktitle", "year"),
        "book": ("title", "publisher", "year"),
        "misc": ("title",),
    },
    recommended={"article": ("volume", "number", "pages", "doi"), "inproceedings": ("doi",)},
)

STYLES: Final[dict[str, Style]] = {"ieee": _IEEE, "acm": _ACM}

#: An all-caps run of two or more letters, optionally carrying digits, a
#: hyphen, or a lowercase plural: BERT, CNNs, GPT-4, ROUGE-L, IEEE, COVID-19.
#: The trailing ``s?`` matters -- without it "CNNs" fails the word boundary and
#: every pluralised acronym goes unreported.
_ACRONYM_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*s?\b"
)
#: Any word with a lowercase letter directly followed by an uppercase one --
#: iPhone, eLearning, ResNet, DenseNet, YOLOv3. Ordinary words never do this,
#: so the pattern is broad without being noisy.
_CAMEL_RE: Final[re.Pattern[str]] = re.compile(r"\b\w*[a-z][A-Z]\w*\b")

#: Words that are capitalised for ordinary reasons and mean nothing here.
_CASE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"A", "AN", "THE", "AND", "OR", "OF", "IN", "ON", "FOR", "TO", "I", "II", "III", "IV"}
)

_URLISH_RE: Final[re.Pattern[str]] = re.compile(r"https?://", re.IGNORECASE)


def _unprotected_spans(raw_title: str) -> list[tuple[int, int]]:
    """Character ranges of the title that sit outside any brace group.

    The braces are what protect capitalisation, so anything inside one is
    already safe and must not be reported.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    escaped = False
    for index, char in enumerate(raw_title):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "{":
            if depth == 0:
                spans.append((start, index))
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                start = index + 1
    if depth == 0:
        spans.append((start, len(raw_title)))
    return spans


def check_case_protection(entry: BibEntry) -> tuple[Problem, ...]:
    """Find acronyms and internal capitals a style file will flatten.

    ``{BERT}`` survives; ``BERT`` becomes "Bert" under IEEEtran and most
    author-year styles. This is invisible until proof stage, resolves perfectly
    against Crossref, and is entirely the author's to fix.
    """
    raw = entry.get("title")
    if not raw:
        return ()

    exposed: list[str] = []
    for start, end in _unprotected_spans(raw):
        segment = raw[start:end]
        for match in _ACRONYM_RE.finditer(segment):
            word = match.group(0)
            if word.upper() not in _CASE_ALLOWLIST and not word.isdigit():
                exposed.append(word)
        for match in _CAMEL_RE.finditer(segment):
            exposed.append(match.group(0))

    unique = list(dict.fromkeys(exposed))
    if not unique:
        return ()

    listed = ", ".join(unique[:6]) + (" …" if len(unique) > 6 else "")
    suggestion = raw
    for word in unique:
        suggestion = re.sub(rf"(?<![{{\w]){re.escape(word)}(?![}}\w])", "{" + word + "}", suggestion)

    return (
        Problem(
            code=Code.CASE_PROTECTION,
            severity=Severity.WARN,
            message=(
                f"{listed} will be lower-cased by most style files; "
                "wrap each in braces to protect it"
            ),
            field="title",
            local=latex_to_unicode(raw),
            authoritative=suggestion,
        ),
    )


def check_style_fields(entry: BibEntry, style: Style) -> tuple[Problem, ...]:
    """Fields the chosen style needs, and ones it merely expects."""
    entry_type = entry.entry_type.lower()
    problems: list[Problem] = []

    required = style.required.get(entry_type, ())
    missing = [name for name in required if not (entry.get(name) or "").strip()]
    # An author-less work may legitimately carry an editor instead.
    if "author" in missing and (entry.get("editor") or "").strip():
        missing.remove("author")
    if missing:
        problems.append(
            Problem(
                code=Code.MISSING_STYLE_FIELD,
                severity=Severity.WARN,
                message=(
                    f"{style.name.upper()} style needs "
                    f"{', '.join(missing)} on an @{entry_type} entry"
                ),
                field=missing[0],
            )
        )

    advised = style.recommended.get(entry_type, ())
    absent = [name for name in advised if not (entry.get(name) or "").strip()]
    if absent:
        problems.append(
            Problem(
                code=Code.MISSING_STYLE_FIELD,
                severity=Severity.INFO,
                message=f"{style.name.upper()} style usually shows {', '.join(absent)}",
                field=absent[0],
            )
        )
    return tuple(problems)


def check_pages_sanity(entry: BibEntry) -> tuple[Problem, ...]:
    """Catch a page range that cannot be right.

    A transposed range is easy to typo and impossible to notice by eye in a
    list of forty references.
    """
    from .normalize import parse_pages

    raw = entry.get("pages")
    pages = parse_pages(raw)
    if pages is None or pages.end is None:
        return ()
    if not (pages.start.isdigit() and pages.end.isdigit()):
        return ()

    start, end = int(pages.start), int(pages.end)
    if start > end:
        return (
            Problem(
                code=Code.SUSPECT_PAGES,
                severity=Severity.WARN,
                message="the page range runs backwards",
                field="pages",
                local=str(raw),
                authoritative=f"{end}--{start}",
            ),
        )
    if end - start > 2000:
        return (
            Problem(
                code=Code.SUSPECT_PAGES,
                severity=Severity.INFO,
                message=f"that is a {end - start}-page span; check the range",
                field="pages",
                local=str(raw),
            ),
        )
    return ()


def check_url_entry(entry: BibEntry) -> tuple[Problem, ...]:
    """A web citation with no access date cannot be checked by a reader later."""
    url = entry.get("url") or entry.get("howpublished") or ""
    if not _URLISH_RE.search(url):
        return ()
    if entry.get("doi"):
        return ()  # A DOI is the stable identifier; the URL is incidental.
    if (entry.get("urldate") or entry.get("accessed") or entry.get("note") or "").strip():
        return ()
    return (
        Problem(
            code=Code.STALE_URL_ENTRY,
            severity=Severity.INFO,
            message="a URL-only citation should carry an access date (urldate)",
            field="url",
            local=url[:80],
        ),
    )


def local_checks(entry: BibEntry, style: Style) -> tuple[Problem, ...]:
    """Every style check that looks at one entry on its own."""
    return (
        *check_case_protection(entry),
        *check_style_fields(entry, style),
        *check_pages_sanity(entry),
        *check_url_entry(entry),
    )


# ------------------------------------------------- bibliography-wide checks

#: Drop these before building a venue signature; they appear or not at random.
_VENUE_NOISE: Final[frozenset[str]] = frozenset(
    {
        "proceedings", "proc", "the", "of", "on", "in", "and", "a", "an",
        "international", "intl", "conference", "conf", "symposium", "symp",
        "workshop", "annual", "journal", "transactions", "trans", "letters",
    }
)


def venue_signature(venue: str) -> str:
    """A spelling-independent handle for a venue.

    ``IEEE Transactions on Pattern Analysis and Machine Intelligence`` and
    ``IEEE Trans. Pattern Anal. Mach. Intell.`` collapse to the same signature,
    because each significant word contributes only its first letter. That is
    what lets the two spellings be recognised as the *same* venue written
    inconsistently, rather than as two different venues.
    """
    words = [word for word in venue_key(venue).split() if word not in _VENUE_NOISE]
    return "".join(word[0] for word in words if word)


_VENUE_FIELDS: Final[tuple[str, ...]] = ("journal", "journaltitle", "booktitle")


def find_inconsistent_venues(
    entries: Sequence[BibEntry],
) -> dict[str, tuple[Problem, ...]]:
    """Report a venue written more than one way across the bibliography.

    Mixing ``IEEE Trans. Pattern Anal.`` with the spelled-out name in the same
    reference list is exactly the inconsistency a copy-editor sends back.
    """
    by_signature: dict[str, dict[str, list[str]]] = {}
    for entry in entries:
        venue = next((entry.get(name) for name in _VENUE_FIELDS if entry.get(name)), None)
        if not venue:
            continue
        signature = venue_signature(venue)
        if len(signature) < 3:
            continue  # Too short to be a confident match.
        spelling = latex_to_unicode(venue).strip()
        by_signature.setdefault(signature, {}).setdefault(spelling, []).append(entry.key)

    found: dict[str, list[Problem]] = {}
    for spellings in by_signature.values():
        if len(spellings) < 2:
            continue
        # The longest spelling is almost always the canonical, unabbreviated one.
        canonical = max(spellings, key=len)
        for spelling, keys in spellings.items():
            if spelling == canonical:
                continue
            for key in keys:
                found.setdefault(key, []).append(
                    Problem(
                        code=Code.INCONSISTENT_VENUE,
                        severity=Severity.INFO,
                        message="this venue is written differently elsewhere in the bibliography",
                        field="venue",
                        local=spelling,
                        authoritative=canonical,
                    )
                )
    return {key: tuple(problems) for key, problems in found.items()}


def find_inconsistent_authors(
    entries: Sequence[BibEntry],
) -> dict[str, tuple[Problem, ...]]:
    """Report an author given as a full name in one entry and initials in another.

    IEEE wants one or the other throughout. Mixing them is the single most
    common thing a copy-editor flags in a hand-assembled bibliography.
    """
    forms: dict[str, dict[str, list[str]]] = {}
    for entry in entries:
        for name in parse_author_field(entry.get("author")):
            if not name.family_key or not name.given:
                continue
            style = "initials" if _is_initials(name.given) else "full"
            forms.setdefault(name.key, {}).setdefault(style, []).append(entry.key)

    found: dict[str, list[Problem]] = {}
    for person, styles in forms.items():
        if len(styles) < 2:
            continue
        # Report the minority spelling: that is the one to change.
        minority = min(styles, key=lambda style: len(styles[style]))
        surname = person.split(":")[0]
        for key in dict.fromkeys(styles[minority]):
            found.setdefault(key, []).append(
                Problem(
                    code=Code.INCONSISTENT_AUTHOR,
                    severity=Severity.INFO,
                    message=(
                        f"author '{surname}' is given as "
                        f"{'initials' if minority == 'initials' else 'a full name'} here "
                        "and the other way elsewhere"
                    ),
                    field="author",
                )
            )
    return {key: tuple(problems) for key, problems in found.items()}


def _is_initials(given: str) -> bool:
    """Is this given name written as initials (``J.``, ``J. A.``, ``JA``)?"""
    tokens = [token for token in re.split(r"[\s.]+", given) if token]
    return bool(tokens) and all(len(token) == 1 for token in tokens)


def bibliography_style_checks(
    entries: Sequence[BibEntry],
) -> dict[str, tuple[Problem, ...]]:
    """Style checks that need to see the whole bibliography at once."""
    merged: dict[str, list[Problem]] = {}
    for source in (find_inconsistent_venues(entries), find_inconsistent_authors(entries)):
        for key, problems in source.items():
            merged.setdefault(key, []).extend(problems)
    return {key: tuple(problems) for key, problems in merged.items()}

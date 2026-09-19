"""The six checks, plus the structural ones.

Every rule in this module is a **pure function**: local metadata and an
authoritative record in, a tuple of :class:`Problem` out. No HTTP, no state, no
ordering dependency between rules. That is what "each as an independent,
individually testable rule" buys -- each one can be exercised directly with
hand-built inputs, and a new rule cannot break an existing one.

Two conventions hold throughout:

* **Absence is not disagreement.** If either side lacks a field, the rule
  returns nothing. "Cannot compare" is never reported as a mismatch.
* **Rules emit severity, they do not decide status.** ``EntryReport.status``
  derives the verdict, so severity policy stays in one place.
"""

from __future__ import annotations

import re
from typing import Final, Iterable, Mapping, Sequence

from .models import (
    BibEntry,
    Code,
    CrossCheck,
    EntryKind,
    Metadata,
    Problem,
    Severity,
    WorkRecord,
)
from .names import authors_match
from .normalize import looks_abbreviated, similarity, title_key, titles_match, venue_key

__all__ = [
    "check_existence",
    "check_drift",
    "check_lifecycle",
    "check_preprint_superseded",
    "find_duplicates",
    "crosscheck_manuscript",
    "extract_citation_keys",
]

#: Venue names differ cosmetically far more often than substantively, so the
#: bar for reporting drift is lower than for a title.
_VENUE_SIMILARITY: Final[float] = 0.72


# --------------------------------------------------------------- 1. existence


def check_existence(
    *,
    doi: str | None,
    found: bool,
    confidence: float | None = None,
    detail: str | None = None,
) -> tuple[Problem, ...]:
    """Check 1 -- does the cited work exist?

    A DOI that does not resolve is the fabricated-citation signal and the most
    serious thing this tool reports. An entry with no identifier that cannot be
    matched by title is *not* the same claim: it may exist perfectly well and
    simply not be in Crossref, so it is reported as unresolved, not as a
    fabrication.
    """
    if found:
        if confidence is not None and confidence < 1.0:
            return (
                Problem(
                    code=Code.LOW_CONFIDENCE_MATCH,
                    severity=Severity.WARN,
                    message=(
                        "matched by title rather than DOI; verify this is the "
                        "work you meant"
                    ),
                    confidence=round(confidence, 3),
                ),
            )
        return ()

    if doi:
        return (
            Problem(
                code=Code.DOI_NOT_FOUND,
                severity=Severity.CRITICAL,
                message="DOI does not resolve against Crossref",
                field="doi",
                local=doi,
                url=f"https://doi.org/{doi}",
            ),
        )

    return (
        Problem(
            code=Code.NOT_FOUND_BY_TITLE,
            severity=Severity.WARN,
            message=detail or "no DOI, and no confident match found by title",
        ),
    )


# ---------------------------------------------------------- 2. metadata drift


def _drift(
    code: Code,
    field: str,
    local: object,
    authoritative: object,
    *,
    severity: Severity = Severity.WARN,
    message: str | None = None,
) -> Problem:
    return Problem(
        code=code,
        severity=severity,
        message=message or f"{field} differs from the published record",
        field=field,
        local=str(local),
        authoritative=str(authoritative),
    )


def check_drift(local: Metadata, record: WorkRecord) -> tuple[Problem, ...]:
    """Check 2 -- does the local metadata match the authoritative record?

    Each field is compared through its normalized key, so LaTeX accents,
    punctuation, case, "&" versus "and" and protective braces never produce a
    finding. Both raw values are carried on the problem so the report can show
    the author exactly what differs.
    """
    authoritative = record.metadata
    problems: list[Problem] = []

    if local.title and authoritative.title:
        if not titles_match(local.title, authoritative.title):
            problems.append(
                _drift(Code.TITLE_MISMATCH, "title", local.title, authoritative.title)
            )

    if local.authors and authoritative.authors:
        if not authors_match(local.authors, authoritative.authors):
            problems.append(
                _drift(
                    Code.AUTHOR_MISMATCH,
                    "author",
                    local.author_display,
                    authoritative.author_display,
                )
            )

    if local.year is not None and authoritative.year is not None:
        if local.year != authoritative.year:
            problems.append(
                _drift(Code.YEAR_MISMATCH, "year", local.year, authoritative.year)
            )

    problems.extend(_check_venue(local, authoritative))

    if local.volume and authoritative.volume:
        if local.volume != authoritative.volume:
            problems.append(
                _drift(Code.VOLUME_MISMATCH, "volume", local.volume, authoritative.volume)
            )

    if local.pages and authoritative.pages:
        if local.pages.key != authoritative.pages.key:
            problems.append(
                _drift(Code.PAGES_MISMATCH, "pages", local.pages, authoritative.pages)
            )

    return tuple(problems)


def _check_venue(local: Metadata, authoritative: Metadata) -> tuple[Problem, ...]:
    """Venue drift, softened for abbreviation.

    ``IEEE Trans. Pattern Anal. Mach. Intell.`` versus the spelled-out name is a
    house style, not an error, and flagging it at WARN would train authors to
    ignore warnings. When either side looks abbreviated the finding drops to
    INFO, which does not affect the entry's status.
    """
    if not local.venue or not authoritative.venue:
        return ()

    left, right = venue_key(local.venue), venue_key(authoritative.venue)
    if left == right:
        return ()
    # One name containing the other is the abbreviation-or-subtitle case.
    if left in right or right in left:
        return ()
    if similarity(left, right) >= _VENUE_SIMILARITY:
        return ()

    abbreviated = looks_abbreviated(local.venue) or looks_abbreviated(authoritative.venue)
    return (
        _drift(
            Code.VENUE_MISMATCH,
            "venue",
            local.venue,
            authoritative.venue,
            severity=Severity.INFO if abbreviated else Severity.WARN,
            message=(
                "venue differs from the published record (one side is "
                "abbreviated, so this may be a style difference)"
                if abbreviated
                else "venue differs from the published record"
            ),
        ),
    )


# -------------------------------------------------------------- 3. retraction


def check_lifecycle(record: WorkRecord) -> tuple[Problem, ...]:
    """Check 3 -- has the work been retracted, withdrawn, or questioned?

    All three are CRITICAL. Citing retracted work in a submitted paper is the
    single most damaging thing in this tool's remit, and an expression of
    concern is exactly the kind of thing an author wants to know about *before*
    submission rather than after.
    """
    problems: list[Problem] = []
    doi = record.metadata.doi
    url = f"https://doi.org/{doi}" if doi else None

    if record.retracted:
        problems.append(
            Problem(
                code=Code.RETRACTED,
                severity=Severity.CRITICAL,
                message="this work has been RETRACTED",
                local=doi,
                url=url,
            )
        )
    if record.withdrawn:
        problems.append(
            Problem(
                code=Code.WITHDRAWN,
                severity=Severity.CRITICAL,
                message="this work has been withdrawn by its publisher",
                local=doi,
                url=url,
            )
        )
    if record.expression_of_concern:
        problems.append(
            Problem(
                code=Code.EXPRESSION_OF_CONCERN,
                severity=Severity.CRITICAL,
                message="an expression of concern has been issued for this work",
                local=doi,
                url=url,
            )
        )
    return tuple(problems)


# ------------------------------------------------------ 4. preprint superseded


def check_preprint_superseded(
    local: Metadata,
    record: WorkRecord,
    *,
    published: WorkRecord | None = None,
) -> tuple[Problem, ...]:
    """Check 4 -- is this preprint now published somewhere citable?

    Only fires when the *local* entry is the preprint. An entry that already
    cites the published version has nothing to upgrade, even though Crossref
    records the relation on both sides.
    """
    is_preprint = local.kind is EntryKind.PREPRINT or local.arxiv_id is not None
    if not is_preprint:
        return ()

    published_doi = record.published_version_doi
    if published_doi is None and published is not None:
        published_doi = published.metadata.doi
    if published_doi is None:
        return ()
    if local.doi and local.doi == published_doi:
        return ()  # Already citing the published version.

    detail = ""
    if published is not None and published.metadata.venue:
        venue = published.metadata.venue
        year = published.metadata.year
        detail = f" in {venue}" + (f" ({year})" if year else "")

    return (
        Problem(
            code=Code.PREPRINT_SUPERSEDED,
            severity=Severity.WARN,
            message=f"this preprint has since been published{detail}; cite that instead",
            local=local.arxiv_id or local.doi,
            authoritative=published_doi,
            url=f"https://doi.org/{published_doi}",
        ),
    )


# -------------------------------------------------------------- 5. duplicates


def find_duplicates(
    entries: Sequence[tuple[str, Metadata]]
) -> dict[str, tuple[Problem, ...]]:
    """Check 5 -- is the same work cited under more than one key?

    Two entries are the same work if they share a DOI, share an arXiv id, or
    have matching titles *and* a compatible year. Title alone is not enough:
    conference and journal versions of a paper legitimately share a title and
    are genuinely different citations.

    Returns a mapping of citation key to problems, so the caller can attach
    findings to every member of a duplicate group rather than just one.
    """
    groups: dict[str, list[str]] = {}
    for key, metadata in entries:
        for signature in _signatures(metadata):
            groups.setdefault(signature, []).append(key)

    found: dict[str, list[Problem]] = {}
    reported: set[frozenset[str]] = set()
    for keys in groups.values():
        unique = list(dict.fromkeys(keys))
        if len(unique) < 2:
            continue
        group = frozenset(unique)
        if group in reported:
            continue  # Two entries can match on both DOI and title.
        reported.add(group)
        for key in unique:
            others = ", ".join(other for other in unique if other != key)
            found.setdefault(key, []).append(
                Problem(
                    code=Code.DUPLICATE_WORK,
                    severity=Severity.WARN,
                    message=f"the same work is also cited as {others}",
                    local=key,
                    authoritative=others,
                )
            )
    return {key: tuple(problems) for key, problems in found.items()}


def _signatures(metadata: Metadata) -> tuple[str, ...]:
    """Identity handles for a work, strongest first."""
    signatures: list[str] = []
    if metadata.doi:
        signatures.append(f"doi:{metadata.doi}")
    if metadata.arxiv_id:
        signatures.append(f"arxiv:{metadata.arxiv_id}")
    if metadata.title:
        key = title_key(metadata.title)
        if key:
            # Year is part of the signature so a conference paper and its
            # extended journal version are not merged.
            signatures.append(f"title:{key}|{metadata.year or '?'}")
    return tuple(signatures)


# --------------------------------------------------- 6. uncited / undefined

#: LaTeX citation commands, including natbib and biblatex spellings.
_CITE_COMMANDS: Final[str] = (
    r"(?:no|full|foot|text|paren|auto|super|smart|cite)?cite"
    r"(?:al)?(?:p|t|s|author|year|title|url|num|par)?\*?"
)
_CITE_RE: Final[re.Pattern[str]] = re.compile(
    r"\\" + _CITE_COMMANDS + r"(?:\s*\[[^\]]*\])*\s*\{([^}]*)\}",
    re.IGNORECASE,
)
#: Pandoc / Markdown citations: ``[@key]``, ``[-@key; @other]``, ``@key``.
_MARKDOWN_CITE_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w@])-?@([A-Za-z0-9_][\w:.#$%&+?<>~/-]*)")
#: ``\nocite{*}`` means "include everything", which suppresses uncited findings.
_NOCITE_ALL_RE: Final[re.Pattern[str]] = re.compile(r"\\nocite\s*\{\s*\*\s*\}")
#: A LaTeX comment runs to end of line, unless the % is escaped.
_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\\)%.*$", re.MULTILINE)


def extract_citation_keys(source: str, *, strip_comments: bool = True) -> set[str]:
    """Pull every cited key out of a .tex or .md manuscript.

    Commented-out citations are ignored by default: a reference that survives
    only inside ``% \\cite{old}`` is genuinely uncited, and reporting otherwise
    would hide exactly the stale entry the author wants to find.
    """
    text = _COMMENT_RE.sub("", source) if strip_comments else source
    keys: set[str] = set()
    for match in _CITE_RE.finditer(text):
        for key in match.group(1).split(","):
            cleaned = key.strip()
            if cleaned and cleaned != "*":
                keys.add(cleaned)
    for match in _MARKDOWN_CITE_RE.finditer(text):
        keys.add(match.group(1).rstrip(".,;:"))
    return keys


def crosscheck_manuscript(
    bib_keys: Iterable[str],
    source: str,
    *,
    manuscript: str | None = None,
) -> CrossCheck:
    """Check 6 -- compare the bibliography against the manuscript.

    An undefined key is a *build failure waiting to happen* and is treated as
    such; an uncited entry is housekeeping and merely informational.
    """
    defined = list(dict.fromkeys(bib_keys))
    cited = extract_citation_keys(source)
    cite_all = bool(_NOCITE_ALL_RE.search(_COMMENT_RE.sub("", source)))

    uncited = () if cite_all else tuple(key for key in defined if key not in cited)
    undefined = tuple(sorted(key for key in cited if key not in set(defined)))
    return CrossCheck(manuscript=manuscript, uncited=uncited, undefined=undefined)

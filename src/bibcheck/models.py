"""The data model.

Three shapes, and the relationship between them is the whole design:

``BibEntry``
    What the .bib file literally says. Raw field strings, original order,
    source line. Never mutated -- ``--fix`` builds a new file from it.

``Metadata``
    The comparable shape. Produced *both* from a local ``BibEntry`` and from an
    authoritative ``WorkRecord``, so drift checking is a field-by-field compare
    of two values of the same type rather than a pile of special cases.

``Problem`` / ``EntryReport`` / ``Report``
    Findings. A check emits ``Problem`` objects carrying a severity; an entry's
    ``Status`` is *derived* from them, never assigned by hand. That keeps
    "how bad is this entry" in one place, so a new check cannot invent its own
    notion of severity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from .names import PersonName
from .normalize import PageRange

__all__ = [
    "Severity",
    "Status",
    "Code",
    "EntryKind",
    "Metadata",
    "BibEntry",
    "WorkRecord",
    "Problem",
    "EntryReport",
    "CrossCheck",
    "Report",
]


class Severity(str, Enum):
    """How much a single finding matters. Ordered by :attr:`rank`."""

    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARN: 1,
    Severity.CRITICAL: 2,
}


class Status(str, Enum):
    """The verdict shown per entry.

    ``UNRESOLVED`` and ``NETWORK_ERROR`` are *outcomes of looking*, not degrees
    of badness: an entry we could not check is reported honestly as unchecked
    rather than being quietly called OK.
    """

    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    UNRESOLVED = "unresolved"
    NETWORK_ERROR = "network_error"


class Code(str, Enum):
    """Stable identifiers for every kind of finding, safe to key JSON on."""

    # Parsing
    MALFORMED_ENTRY = "malformed_entry"
    DUPLICATE_KEY = "duplicate_key"
    DUPLICATE_FIELD = "duplicate_field"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MALFORMED_DOI = "malformed_doi"

    # Existence
    DOI_NOT_FOUND = "doi_not_found"
    NO_IDENTIFIER = "no_identifier"
    NOT_FOUND_BY_TITLE = "not_found_by_title"
    LOW_CONFIDENCE_MATCH = "low_confidence_match"

    # Metadata drift
    TITLE_MISMATCH = "title_mismatch"
    AUTHOR_MISMATCH = "author_mismatch"
    YEAR_MISMATCH = "year_mismatch"
    VENUE_MISMATCH = "venue_mismatch"
    VOLUME_MISMATCH = "volume_mismatch"
    PAGES_MISMATCH = "pages_mismatch"

    # Lifecycle
    RETRACTED = "retracted"
    WITHDRAWN = "withdrawn"
    EXPRESSION_OF_CONCERN = "expression_of_concern"
    PREPRINT_SUPERSEDED = "preprint_superseded"

    # Bibliography-level
    DUPLICATE_WORK = "duplicate_work"
    UNCITED_ENTRY = "uncited_entry"
    UNDEFINED_CITATION = "undefined_citation"

    # Infrastructure
    NETWORK_ERROR = "network_error"


class EntryKind(str, Enum):
    """Coarse work type, normalised across BibTeX types and Crossref types."""

    JOURNAL_ARTICLE = "journal-article"
    CONFERENCE_PAPER = "conference-paper"
    PREPRINT = "preprint"
    BOOK = "book"
    CHAPTER = "chapter"
    THESIS = "thesis"
    REPORT = "report"
    WEBPAGE = "webpage"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Metadata:
    """The comparable view of a work, from either side of the comparison.

    Every field is optional: absence means "this source does not say", which is
    different from a disagreement and is never reported as drift.
    """

    title: str | None = None
    subtitle: str | None = None
    authors: tuple[PersonName, ...] = ()
    year: int | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: PageRange | None = None
    publisher: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    kind: EntryKind = EntryKind.OTHER

    @property
    def full_title(self) -> str | None:
        if self.title and self.subtitle:
            return f"{self.title}: {self.subtitle}"
        return self.title

    @property
    def author_display(self) -> str:
        return " and ".join(str(name) for name in self.authors)


@dataclass(frozen=True, slots=True)
class BibEntry:
    """One entry exactly as it appears in the source file.

    ``fields`` is keyed by lowercased field name but holds the *raw* value, and
    ``field_order`` records the original spelling and order of the keys. Those
    two together are what let ``--fix`` rewrite a single value while leaving the
    rest of the entry byte-for-byte as the author wrote it.
    """

    key: str
    entry_type: str
    fields: Mapping[str, str]
    field_order: tuple[str, ...] = ()
    start_line: int | None = None
    source: str | None = None

    def get(self, name: str) -> str | None:
        return self.fields.get(name.lower())

    @property
    def kind(self) -> EntryKind:
        return _BIBTEX_KIND.get(self.entry_type.lower(), EntryKind.OTHER)


_BIBTEX_KIND: dict[str, EntryKind] = {
    "article": EntryKind.JOURNAL_ARTICLE,
    "inproceedings": EntryKind.CONFERENCE_PAPER,
    "conference": EntryKind.CONFERENCE_PAPER,
    "proceedings": EntryKind.CONFERENCE_PAPER,
    "book": EntryKind.BOOK,
    "inbook": EntryKind.CHAPTER,
    "incollection": EntryKind.CHAPTER,
    "phdthesis": EntryKind.THESIS,
    "mastersthesis": EntryKind.THESIS,
    "techreport": EntryKind.REPORT,
    "manual": EntryKind.REPORT,
    "online": EntryKind.WEBPAGE,
    "electronic": EntryKind.WEBPAGE,
    "misc": EntryKind.OTHER,
    "unpublished": EntryKind.OTHER,
}


@dataclass(frozen=True, slots=True)
class WorkRecord:
    """An authoritative record retrieved from a registry.

    The lifecycle flags live here rather than on :class:`Metadata` because they
    describe the *work's* standing in the literature, not a field that could
    drift between two copies of it.
    """

    metadata: Metadata
    source: str = "crossref"
    retracted: bool = False
    withdrawn: bool = False
    expression_of_concern: bool = False
    update_notices: tuple[str, ...] = ()
    published_version_doi: str | None = None
    retrieved_from_cache: bool = False
    match_confidence: float = 1.0

    @property
    def has_lifecycle_flag(self) -> bool:
        return self.retracted or self.withdrawn or self.expression_of_concern


@dataclass(frozen=True, slots=True)
class Problem:
    """A single finding about one entry.

    ``local`` and ``authoritative`` are display strings, deliberately kept
    verbatim: the report shows the author both values and lets them judge.
    """

    code: Code
    severity: Severity
    message: str
    field: str | None = None
    local: str | None = None
    authoritative: str | None = None
    confidence: float | None = None
    url: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
        }
        for name in ("field", "local", "authoritative", "confidence", "url"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True, slots=True)
class EntryReport:
    """Everything known about one entry after all checks have run."""

    key: str
    entry_type: str
    problems: tuple[Problem, ...] = ()
    resolved: bool = False
    network_failed: bool = False
    record: WorkRecord | None = None
    metadata: Metadata | None = None
    start_line: int | None = None

    @property
    def status(self) -> Status:
        """Derived, never assigned.

        A network failure outranks everything, because nothing else we found is
        trustworthy when the lookup itself failed. An unresolved entry is next:
        we genuinely do not know whether the work exists. Only then do findings
        decide the verdict.
        """
        if self.network_failed:
            return Status.NETWORK_ERROR
        worst = self.worst_severity
        if worst is Severity.CRITICAL:
            return Status.CRITICAL
        if not self.resolved:
            return Status.UNRESOLVED
        if worst is Severity.WARN:
            return Status.WARN
        return Status.OK

    @property
    def worst_severity(self) -> Severity | None:
        if not self.problems:
            return None
        return max((problem.severity for problem in self.problems), key=lambda s: s.rank)

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "entry_type": self.entry_type,
            "status": self.status.value,
            "line": self.start_line,
            "problems": [problem.to_json() for problem in self.problems],
        }


@dataclass(frozen=True, slots=True)
class CrossCheck:
    """Result of comparing the bibliography against a manuscript."""

    manuscript: str | None = None
    uncited: tuple[str, ...] = ()
    undefined: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "manuscript": self.manuscript,
            "uncited": list(self.uncited),
            "undefined": list(self.undefined),
        }


@dataclass(frozen=True, slots=True)
class Report:
    """The whole run: one report per entry, plus bibliography-level findings."""

    bib_path: str
    entries: tuple[EntryReport, ...] = ()
    parse_problems: tuple[Problem, ...] = ()
    crosscheck: CrossCheck | None = None
    offline: bool = False
    generated_at: str | None = None

    @property
    def counts(self) -> dict[Status, int]:
        tally = {status: 0 for status in Status}
        for entry in self.entries:
            tally[entry.status] += 1
        return tally

    @property
    def worst_status(self) -> Status:
        """The most serious status present, for ``--fail-on``."""
        tally = self.counts
        for status in (Status.CRITICAL, Status.NETWORK_ERROR, Status.UNRESOLVED, Status.WARN):
            if tally[status]:
                return status
        return Status.OK

    def to_json(self) -> dict[str, object]:
        return {
            "bib_path": self.bib_path,
            "generated_at": self.generated_at,
            "offline": self.offline,
            "summary": {status.value: count for status, count in self.counts.items()},
            "entries": [entry.to_json() for entry in self.entries],
            "parse_problems": [problem.to_json() for problem in self.parse_problems],
            "crosscheck": self.crosscheck.to_json() if self.crosscheck else None,
        }

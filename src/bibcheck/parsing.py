"""BibTeX parsing, built on ``bibtexparser`` v2.

Parsing never raises on a bad file. A malformed entry becomes a
``MALFORMED_ENTRY`` problem with its source line and the rest of the
bibliography is checked normally -- one broken entry must not cost the author
the other thirty-nine results.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Mapping, Sequence

from bibtexparser.entrypoint import parse_string
from bibtexparser.model import (
    DuplicateBlockKeyBlock,
    DuplicateFieldKeyBlock,
    Entry,
    ImplicitComment,
    ParsingFailedBlock,
)

from .identifiers import extract_arxiv_id, extract_doi, is_plausible_doi
from .models import (
    BibEntry,
    Code,
    EntryKind,
    Metadata,
    Problem,
    Severity,
)
from .names import parse_author_field
from .normalize import normalize_text, parse_pages, parse_volume, parse_year

__all__ = ["ParsedBib", "parse_bibtex", "parse_bibtex_file", "entry_metadata"]

#: Fields that identify the venue, in the order they should be consulted.
_VENUE_FIELDS: Final[tuple[str, ...]] = (
    "journal", "journaltitle", "booktitle", "series", "school",
    "institution", "publisher", "howpublished",
)

#: Minimum fields required to even attempt a lookup, per entry kind.
_REQUIRED: Final[dict[EntryKind, tuple[str, ...]]] = {
    EntryKind.JOURNAL_ARTICLE: ("title", "author", "year"),
    EntryKind.CONFERENCE_PAPER: ("title", "author", "year"),
    EntryKind.BOOK: ("title", "year"),
    EntryKind.CHAPTER: ("title", "year"),
    EntryKind.THESIS: ("title", "author", "year"),
    EntryKind.REPORT: ("title", "year"),
}
_REQUIRED_DEFAULT: Final[tuple[str, ...]] = ("title",)


@dataclass(frozen=True, slots=True)
class ParsedBib:
    """Outcome of reading a .bib file: entries plus any file-level problems."""

    path: str
    entries: tuple[BibEntry, ...] = ()
    problems: tuple[Problem, ...] = ()

    def by_key(self) -> dict[str, BibEntry]:
        return {entry.key: entry for entry in self.entries}


def _entry_fields(entry: Entry) -> tuple[dict[str, str], tuple[str, ...], list[Problem]]:
    """Collapse an entry's fields to a lowercased mapping, keeping order.

    A field repeated inside one entry is a real error in the source file, so it
    is reported rather than silently resolved; the first occurrence wins, which
    is what BibTeX itself does.
    """
    values: dict[str, str] = {}
    order: list[str] = []
    problems: list[Problem] = []
    for item in entry.fields:
        name = item.key.strip().lower()
        if name in values:
            problems.append(
                Problem(
                    code=Code.DUPLICATE_FIELD,
                    severity=Severity.WARN,
                    message=f"field {name!r} appears more than once; the first value is used",
                    field=name,
                    local=values[name],
                    authoritative=item.value,
                )
            )
            continue
        values[name] = item.value
        order.append(item.key.strip())
    return values, tuple(order), problems


def _describe_failure(block: ParsingFailedBlock) -> str:
    detail = str(getattr(block, "error", "") or "").strip().splitlines()
    reason = detail[-1] if detail else "unparseable entry"
    raw = (getattr(block, "raw", "") or "").strip().splitlines()
    excerpt = raw[0].strip() if raw else ""
    if excerpt:
        return f"{reason} (starting at {excerpt[:60]!r})"
    return reason


def _line_of(block: object) -> int | None:
    """bibtexparser counts lines from zero; humans and editors count from one."""
    raw = getattr(block, "start_line", None)
    return raw + 1 if isinstance(raw, int) else None


def parse_bibtex(text: str, *, path: str = "<string>") -> ParsedBib:
    """Parse BibTeX source into :class:`BibEntry` objects.

    Never raises. A duplicate key, a duplicate field and a syntax error are each
    reported as a distinct problem rather than lumped together, and the first
    two still yield a usable entry -- bibtexparser hands back the recovered
    block, so an author does not lose a reference to a typo.
    """
    library = parse_string(text)
    entries: list[BibEntry] = []
    problems: list[Problem] = []
    seen: set[str] = set()

    def record(entry_block: Entry, extra: Sequence[Problem] = ()) -> None:
        values, order, field_problems = _entry_fields(entry_block)
        key = entry_block.key.strip()
        line = _line_of(entry_block)
        if key in seen:
            problems.append(_duplicate_key_problem(key, line))
            return
        seen.add(key)
        entries.append(
            BibEntry(
                key=key,
                entry_type=entry_block.entry_type.strip().lower(),
                fields=values,
                field_order=order,
                start_line=line,
                source=path,
            )
        )
        problems.extend(extra)
        problems.extend(field_problems)

    for block in library.blocks:
        if isinstance(block, ImplicitComment):
            continue

        # Both duplicate-flavoured blocks subclass ParsingFailedBlock, so they
        # must be matched before the generic failure case.
        if isinstance(block, DuplicateFieldKeyBlock):
            recovered = block.ignore_error_block
            if isinstance(recovered, Entry):
                # The recovered entry still carries both values, so _entry_fields
                # reports the duplicate with both sides shown -- more useful than
                # repeating it here with only the field name.
                record(recovered)
            else:
                names = ", ".join(sorted(block.duplicate_keys))
                problems.append(
                    Problem(
                        code=Code.DUPLICATE_FIELD,
                        severity=Severity.WARN,
                        message=f"field(s) {names} appear more than once in this entry",
                        local=names,
                    )
                )
            continue

        if isinstance(block, DuplicateBlockKeyBlock):
            key = str(getattr(block, "key", "") or "?")
            problems.append(_duplicate_key_problem(key, _line_of(block)))
            continue

        if isinstance(block, ParsingFailedBlock):
            line = _line_of(block)
            problems.append(
                Problem(
                    code=Code.MALFORMED_ENTRY,
                    severity=Severity.WARN,
                    message=_describe_failure(block),
                    local=f"line {line}" if line is not None else None,
                )
            )
            continue

        if isinstance(block, Entry):
            record(block)

    return ParsedBib(path=path, entries=tuple(entries), problems=tuple(problems))


def _duplicate_key_problem(key: str, line: int | None) -> Problem:
    where = f" (line {line})" if line is not None else ""
    return Problem(
        code=Code.DUPLICATE_KEY,
        severity=Severity.WARN,
        message=(
            f"citation key {key!r} is defined more than once{where}; "
            "the first definition is used"
        ),
        local=key,
    )


def parse_bibtex_file(path: str | Path) -> ParsedBib:
    """Read and parse a .bib file from disk, tolerating a non-UTF-8 encoding."""
    location = Path(path)
    raw = location.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Legacy bibliographies are often Latin-1; decoding permissively beats
        # failing the whole run over one stray byte.
        text = raw.decode("latin-1")
    return parse_bibtex(text, path=str(location))


def entry_metadata(entry: BibEntry) -> Metadata:
    """Project a local entry onto the comparable :class:`Metadata` shape."""
    title = entry.get("title")
    venue = next(
        (entry.get(name) for name in _VENUE_FIELDS if entry.get(name)),
        None,
    )
    return Metadata(
        title=normalize_text(title) if title else None,
        subtitle=normalize_text(entry.get("subtitle") or "") or None,
        authors=parse_author_field(entry.get("author") or entry.get("editor")),
        year=parse_year(entry.get("year") or entry.get("date")),
        venue=normalize_text(venue) if venue else None,
        volume=parse_volume(entry.get("volume")),
        issue=parse_volume(entry.get("number") or entry.get("issue")),
        pages=parse_pages(entry.get("pages")),
        publisher=normalize_text(entry.get("publisher") or "") or None,
        doi=extract_doi(entry.fields),
        arxiv_id=extract_arxiv_id(entry.fields),
        kind=_refine_kind(entry),
    )


def _refine_kind(entry: BibEntry) -> EntryKind:
    """An ``@misc`` carrying an arXiv id is a preprint, whatever it calls itself."""
    kind = entry.kind
    if extract_arxiv_id(entry.fields) is not None and kind in {
        EntryKind.OTHER,
        EntryKind.REPORT,
        EntryKind.WEBPAGE,
    }:
        return EntryKind.PREPRINT
    return kind


def structural_problems(entry: BibEntry) -> tuple[Problem, ...]:
    """Problems visible without any network access.

    Kept separate from the network checks so ``--offline`` still reports them,
    and so they are testable with no fixtures at all.
    """
    found: list[Problem] = []
    required = _REQUIRED.get(entry.kind, _REQUIRED_DEFAULT)
    for name in required:
        if not (entry.get(name) or "").strip():
            found.append(
                Problem(
                    code=Code.MISSING_REQUIRED_FIELD,
                    severity=Severity.WARN,
                    message=f"@{entry.entry_type} entry has no {name} field",
                    field=name,
                )
            )

    raw_doi = entry.get("doi")
    if raw_doi and raw_doi.strip() and not is_plausible_doi(raw_doi):
        found.append(
            Problem(
                code=Code.MALFORMED_DOI,
                severity=Severity.WARN,
                message="doi field is not a structurally valid DOI",
                field="doi",
                local=raw_doi.strip(),
            )
        )
    return tuple(found)

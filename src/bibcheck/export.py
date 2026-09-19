"""Write a .bib file from checked references.

The payoff of reading a PDF: a paper arrives with no bibliography source, and
this produces one -- built from the *authoritative* registry record wherever a
reference resolved, rather than from whatever the PDF's typography happened to
survive extraction.
"""

from __future__ import annotations

import re
from typing import Final, Iterable

from .models import EntryKind, EntryReport, Metadata, Report

__all__ = ["to_bibtex"]

#: Characters BibTeX treats specially and that must be escaped in a value.
_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\\)([&%$#_])")

_TYPE_BY_KIND: Final[dict[EntryKind, str]] = {
    EntryKind.JOURNAL_ARTICLE: "article",
    EntryKind.CONFERENCE_PAPER: "inproceedings",
    EntryKind.PREPRINT: "misc",
    EntryKind.BOOK: "book",
    EntryKind.CHAPTER: "incollection",
    EntryKind.THESIS: "phdthesis",
    EntryKind.REPORT: "techreport",
    EntryKind.WEBPAGE: "misc",
    EntryKind.OTHER: "misc",
}


def _escape(value: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", value)


def _fields_for(metadata: Metadata, entry_type: str) -> list[tuple[str, str]]:
    venue_field = "booktitle" if entry_type == "inproceedings" else "journal"
    # A preprint's "venue" is the server it sits on, which eprint and
    # archivePrefix already say. Repeating it as a journal is noise.
    venue = None if metadata.arxiv_id and entry_type == "misc" else metadata.venue
    candidates: list[tuple[str, str | None]] = [
        ("title", metadata.full_title),
        ("author", metadata.author_display or None),
        (venue_field, venue),
        ("volume", metadata.volume),
        ("number", metadata.issue),
        ("pages", str(metadata.pages).replace("-", "--") if metadata.pages else None),
        ("year", str(metadata.year) if metadata.year else None),
        ("publisher", metadata.publisher if entry_type in {"book", "techreport"} else None),
        ("doi", metadata.doi),
    ]
    if metadata.arxiv_id:
        candidates.append(("eprint", metadata.arxiv_id))
        candidates.append(("archiveprefix", "arXiv"))
    return [(name, value) for name, value in candidates if value]


def to_bibtex(report: Report, *, header: str | None = None) -> str:
    """Render a report's entries as a BibTeX file.

    Where a reference resolved, the registry's metadata wins -- that is the
    whole point. Where it did not, whatever was read from the source is kept
    and marked, so nothing silently disappears from the bibliography.
    """
    blocks: list[str] = []
    if header:
        blocks.append("\n".join(f"% {line}" for line in header.splitlines()))

    for entry in report.entries:
        blocks.append(_render_entry(entry))
    return "\n\n".join(block for block in blocks if block) + "\n"


def _render_entry(entry: EntryReport) -> str:
    record = entry.record
    authoritative = record is not None and entry.resolved
    metadata = record.metadata if (record is not None and authoritative) else entry.metadata
    if metadata is None:
        return ""

    entry_type = _TYPE_BY_KIND.get(metadata.kind, "misc")
    if entry_type == "misc" and metadata.arxiv_id is None and entry.entry_type:
        entry_type = entry.entry_type

    fields = _fields_for(metadata, entry_type)
    if not fields:
        return ""

    width = max(len(name) for name, _ in fields)
    lines = [f"@{entry_type}{{{entry.key},"]
    if not authoritative:
        lines.insert(
            0, "% unverified: this entry could not be resolved, and is as-read"
        )
    lines.extend(
        f"  {name.ljust(width)} = {{{_escape(value)}}}," for name, value in fields
    )
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)

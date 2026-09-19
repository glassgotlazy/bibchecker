"""bibcheck -- verify the integrity of every reference in a BibTeX file."""

from __future__ import annotations

__version__ = "0.1.0"

from .models import (
    BibEntry,
    Code,
    CrossCheck,
    EntryKind,
    EntryReport,
    Metadata,
    Problem,
    Report,
    Severity,
    Status,
    WorkRecord,
)
from .parsing import ParsedBib, entry_metadata, parse_bibtex, parse_bibtex_file

__all__ = [
    "__version__",
    "BibEntry",
    "Code",
    "CrossCheck",
    "EntryKind",
    "EntryReport",
    "Metadata",
    "ParsedBib",
    "Problem",
    "Report",
    "Severity",
    "Status",
    "WorkRecord",
    "entry_metadata",
    "parse_bibtex",
    "parse_bibtex_file",
]

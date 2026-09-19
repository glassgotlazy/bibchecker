"""Run every check over a bibliography.

The orchestration layer. It resolves each entry against the registry
concurrently, hands the results to the pure rules in :mod:`bibcheck.checks`,
and assembles a :class:`Report`.

Its one hard guarantee: **an exception anywhere in one entry costs that entry
and nothing else.** Every per-entry step is wrapped, so a malformed record, a
parser surprise or an outright bug degrades to a NETWORK_ERROR status on a
single reference rather than taking down a run the author needs.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .checks import (
    check_drift,
    check_existence,
    check_lifecycle,
    check_preprint_superseded,
    crosscheck_manuscript,
    find_duplicates,
)
from .config import Config
from .crossref import CrossrefClient, Lookup, Outcome
from .models import (
    BibEntry,
    Code,
    CrossCheck,
    EntryReport,
    Metadata,
    Problem,
    Report,
    Severity,
    WorkRecord,
)
from .parsing import ParsedBib, entry_metadata, structural_problems

__all__ = ["check_bibliography", "check_entry"]


async def check_bibliography(
    parsed: ParsedBib,
    config: Config,
    *,
    manuscript_path: str | Path | None = None,
    manuscript_text: str | None = None,
    client: CrossrefClient | None = None,
) -> Report:
    """Check every entry, then apply the bibliography-level rules."""
    metadata_by_key = {entry.key: entry_metadata(entry) for entry in parsed.entries}

    if client is not None:
        reports = await _run_all(parsed.entries, metadata_by_key, client)
    else:
        async with CrossrefClient(config) as owned:
            reports = await _run_all(parsed.entries, metadata_by_key, owned)

    # Checks 5 and 6 see the whole bibliography, so they run once at the end and
    # their findings are merged onto the per-entry reports.
    duplicates = find_duplicates(
        [(entry.key, metadata_by_key[entry.key]) for entry in parsed.entries]
    )
    reports = tuple(
        _with_problems(report, duplicates.get(report.key, ())) for report in reports
    )

    crosscheck = None
    text = _read_manuscript(manuscript_path, manuscript_text)
    if text is not None:
        crosscheck = crosscheck_manuscript(
            [entry.key for entry in parsed.entries],
            text,
            manuscript=str(manuscript_path) if manuscript_path else None,
        )
        reports = tuple(_with_crosscheck(report, crosscheck) for report in reports)

    return Report(
        bib_path=parsed.path,
        entries=reports,
        parse_problems=parsed.problems,
        crosscheck=crosscheck,
        offline=config.offline,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


async def _run_all(
    entries: Sequence[BibEntry],
    metadata_by_key: dict[str, Metadata],
    client: CrossrefClient,
) -> tuple[EntryReport, ...]:
    """Resolve and check every entry concurrently.

    ``return_exceptions`` is deliberate: a bug in one entry's handling must not
    cancel the other thirty-nine. Anything that escapes becomes that entry's
    NETWORK_ERROR rather than the run's traceback.
    """
    results = await asyncio.gather(
        *(check_entry(entry, metadata_by_key[entry.key], client) for entry in entries),
        return_exceptions=True,
    )

    reports: list[EntryReport] = []
    for entry, result in zip(entries, results, strict=True):
        if isinstance(result, EntryReport):
            reports.append(result)
            continue
        reports.append(
            EntryReport(
                key=entry.key,
                entry_type=entry.entry_type,
                start_line=entry.start_line,
                network_failed=True,
                metadata=metadata_by_key[entry.key],
                problems=(
                    Problem(
                        code=Code.NETWORK_ERROR,
                        severity=Severity.WARN,
                        message=f"could not be checked: {type(result).__name__}: {result}",
                    ),
                ),
            )
        )
    return tuple(reports)


async def check_entry(
    entry: BibEntry, local: Metadata, client: CrossrefClient
) -> EntryReport:
    """Resolve one entry and run every entry-level rule against it."""
    problems: list[Problem] = list(structural_problems(entry))
    lookup = await _resolve(local, client)

    if lookup.outcome in {Outcome.NETWORK_ERROR, Outcome.OFFLINE_MISS}:
        problems.append(
            Problem(
                code=Code.NETWORK_ERROR,
                severity=Severity.WARN,
                message=lookup.detail or "lookup did not complete",
            )
        )
        return EntryReport(
            key=entry.key,
            entry_type=entry.entry_type,
            start_line=entry.start_line,
            problems=tuple(problems),
            resolved=False,
            network_failed=True,
            metadata=local,
        )

    record = lookup.record
    problems.extend(
        check_existence(
            doi=local.doi,
            found=lookup.found,
            confidence=record.match_confidence if record else None,
            detail=lookup.detail,
        )
    )

    if record is None:
        return EntryReport(
            key=entry.key,
            entry_type=entry.entry_type,
            start_line=entry.start_line,
            problems=tuple(problems),
            resolved=False,
            metadata=local,
        )

    problems.extend(check_lifecycle(record))
    problems.extend(check_drift(local, record))

    published = await _published_version(record, client)
    problems.extend(check_preprint_superseded(local, record, published=published))

    return EntryReport(
        key=entry.key,
        entry_type=entry.entry_type,
        start_line=entry.start_line,
        problems=tuple(problems),
        resolved=True,
        record=record,
        metadata=local,
    )


async def _resolve(local: Metadata, client: CrossrefClient) -> Lookup:
    """Resolve by DOI when there is one, otherwise by title.

    A DOI that fails to resolve is *not* retried as a title search: "this DOI is
    wrong" is precisely the finding, and quietly finding some other plausible
    paper would bury it.
    """
    if local.doi:
        return await client.fetch_by_doi(local.doi)
    return await client.search(local)


async def _published_version(
    record: WorkRecord, client: CrossrefClient
) -> WorkRecord | None:
    """Fetch the published version of a preprint, if the record names one."""
    doi = record.published_version_doi
    if doi is None:
        return None
    lookup = await client.fetch_by_doi(doi)
    return lookup.record if lookup.found else None


def _with_problems(report: EntryReport, extra: Sequence[Problem]) -> EntryReport:
    if not extra:
        return report
    return replace(report, problems=report.problems + tuple(extra))


def _with_crosscheck(report: EntryReport, crosscheck: CrossCheck) -> EntryReport:
    """Attach an ``uncited`` finding to the entry it concerns.

    Uncited entries are INFO: an unused reference is housekeeping, not an error,
    and reporting it louder would drown the findings that matter.
    """
    if report.key not in crosscheck.uncited:
        return report
    return replace(
        report,
        problems=report.problems
        + (
            Problem(
                code=Code.UNCITED_ENTRY,
                severity=Severity.INFO,
                message="defined in the bibliography but never cited",
            ),
        ),
    )


def _read_manuscript(path: str | Path | None, text: str | None) -> str | None:
    if text is not None:
        return text
    if path is None:
        return None
    location = Path(path)
    try:
        return location.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return location.read_bytes().decode("latin-1")
    except OSError:
        return None

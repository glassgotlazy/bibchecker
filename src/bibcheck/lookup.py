"""``bibcheck cite`` -- fetch one work and emit a ready-to-paste BibTeX entry.

The writing-time companion to the checking side. Mid-paragraph you have a DOI,
an arXiv id, or just a remembered title, and you want the entry now rather than
after a trip to a publisher page and a round of hand-editing.

It goes through exactly the same client, cache and parsing as everything else,
so the entry it emits is the one bibcheck would have verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Sequence

from .config import Config
from .crossref import CONFIDENT_MATCH, CrossrefClient, Lookup, Outcome
from .export import to_bibtex
from .identifiers import normalize_arxiv_id, normalize_doi
from .keys import suggest_key
from .models import EntryReport, Metadata, Report, WorkRecord

__all__ = ["resolve_query", "cite", "CiteResult"]

_ARXIV_DOI: Final[str] = "10.48550/arxiv."


@dataclass(frozen=True, slots=True)
class CiteResult:
    """What ``cite`` found, if anything."""

    record: WorkRecord | None
    bibtex: str
    key: str
    outcome: Outcome
    detail: str | None = None

    @property
    def found(self) -> bool:
        return self.record is not None


def _looks_like_identifier(query: str) -> tuple[str | None, str | None]:
    """Classify the query as (doi, arxiv_id), either or both possibly ``None``."""
    doi = normalize_doi(query)
    if doi is not None:
        return doi, None
    arxiv = normalize_arxiv_id(query)
    if arxiv is not None:
        return None, arxiv
    return None, None


async def resolve_query(query: str, client: CrossrefClient) -> Lookup:
    """Resolve a DOI, an arXiv id, or a free-text title.

    An arXiv id is tried through its registered DOI first, then by title --
    arXiv deposits with DataCite rather than Crossref, so the DOI route often
    misses and the fallback is what actually finds the published version.
    """
    query = query.strip()
    if not query:
        return Lookup(outcome=Outcome.NOT_FOUND, detail="empty query")

    doi, arxiv = _looks_like_identifier(query)
    if doi is not None:
        return await client.fetch_by_doi(doi)

    if arxiv is not None:
        by_doi = await client.fetch_by_doi(f"{_ARXIV_DOI}{arxiv}")
        if by_doi.found:
            return by_doi
        if by_doi.outcome is Outcome.NETWORK_ERROR:
            return by_doi
        return Lookup(
            outcome=Outcome.NOT_FOUND,
            detail=(
                f"arXiv:{arxiv} is not in Crossref; search by title instead, or "
                "cite the published version if there is one"
            ),
        )

    return await client.search(Metadata(title=query))


async def cite(
    query: str,
    config: Config,
    *,
    key: str | None = None,
    client: CrossrefClient | None = None,
) -> CiteResult:
    """Look a work up and render it as BibTeX."""
    if client is not None:
        lookup = await resolve_query(query, client)
    else:
        async with CrossrefClient(config) as owned:
            lookup = await resolve_query(query, owned)

    record = lookup.record
    if record is None:
        return CiteResult(
            record=None, bibtex="", key="", outcome=lookup.outcome, detail=lookup.detail
        )

    chosen = key or suggest_key(record.metadata)
    report = Report(
        bib_path=query,
        entries=(
            EntryReport(
                key=chosen,
                entry_type="article",
                resolved=True,
                record=record,
                metadata=record.metadata,
            ),
        ),
    )
    return CiteResult(
        record=record,
        bibtex=to_bibtex(report).strip(),
        key=chosen,
        outcome=lookup.outcome,
        detail=lookup.detail,
    )

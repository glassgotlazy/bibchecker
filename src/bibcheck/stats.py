"""Bibliography health metrics.

Not errors -- *shape*. A reference list can be entirely correct and still draw
a reviewer's comment: forty references of which thirty are preprints, or a
median year of 2011 in a fast-moving field, or eight self-citations out of
twenty. These are the numbers a reader forms an impression from before reading
a word, and the author almost never counts them.

Everything here is derived from what is already parsed. No network.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Iterable, Mapping, Sequence

from .models import EntryKind, Metadata
from .names import PersonName

__all__ = ["Stats", "summarise", "OLD_REFERENCE_YEARS"]

#: A reference older than this is "older literature". Not wrong -- foundational
#: work is often decades old -- but worth knowing the proportion.
OLD_REFERENCE_YEARS: Final[int] = 10
#: Above this share of preprints, reviewers in most fields start commenting.
PREPRINT_CONCERN: Final[float] = 0.35
#: Above this share of self-citations, likewise.
SELF_CITATION_CONCERN: Final[float] = 0.25


@dataclass(frozen=True, slots=True)
class Stats:
    """A bibliography's shape, in numbers."""

    total: int = 0
    with_doi: int = 0
    with_identifier: int = 0
    preprints: int = 0
    self_citations: int = 0
    years: tuple[int, ...] = ()
    venues: tuple[tuple[str, int], ...] = ()
    kinds: tuple[tuple[str, int], ...] = ()
    old_references: int = 0
    reference_year: int = 0

    @property
    def median_year(self) -> int | None:
        return int(statistics.median(self.years)) if self.years else None

    @property
    def oldest(self) -> int | None:
        return min(self.years) if self.years else None

    @property
    def newest(self) -> int | None:
        return max(self.years) if self.years else None

    def share(self, count: int) -> float:
        return count / self.total if self.total else 0.0

    @property
    def notes(self) -> tuple[str, ...]:
        """Plain-language observations, only where a number is notable.

        Deliberately conservative: a note here is a prompt to look, never a
        finding, and a list of nagging that fires on every bibliography would
        be ignored on every bibliography.
        """
        remarks: list[str] = []
        if not self.total:
            return ()

        if self.share(self.preprints) > PREPRINT_CONCERN:
            remarks.append(
                f"{self.preprints} of {self.total} references are preprints "
                f"({self.share(self.preprints):.0%}); check whether any have "
                "since been published"
            )
        if self.share(self.self_citations) > SELF_CITATION_CONCERN:
            remarks.append(
                f"{self.self_citations} of {self.total} are self-citations "
                f"({self.share(self.self_citations):.0%})"
            )
        missing = self.total - self.with_identifier
        if missing and self.share(missing) > 0.2:
            remarks.append(
                f"{missing} references carry no DOI or arXiv id, so they cannot "
                "be verified automatically"
            )
        if self.share(self.old_references) > 0.6 and self.total >= 8:
            remarks.append(
                f"{self.old_references} of {self.total} references are more than "
                f"{OLD_REFERENCE_YEARS} years old"
            )
        if self.venues and self.total >= 8:
            venue, count = self.venues[0]
            if count / self.total > 0.4:
                remarks.append(
                    f"{count} of {self.total} references come from {venue}"
                )
        return tuple(remarks)


def _is_preprint(metadata: Metadata) -> bool:
    return metadata.kind is EntryKind.PREPRINT or (
        metadata.arxiv_id is not None and metadata.doi is None
    )


def _matches_author(authors: Sequence[PersonName], surnames: frozenset[str]) -> bool:
    return any(name.family_key in surnames for name in authors)


def summarise(
    entries: Iterable[Metadata],
    *,
    self_authors: Sequence[str] = (),
    reference_year: int | None = None,
) -> Stats:
    """Compute the metrics.

    ``self_authors`` are surnames to count as self-citations; matching folds
    accents and case, so "Schölkopf" and "Scholkopf" count the same.
    """
    from .names import parse_name

    surnames = frozenset(
        parse_name(author).family_key for author in self_authors if author.strip()
    )
    year_now = reference_year if reference_year is not None else date.today().year

    total = 0
    with_doi = 0
    with_identifier = 0
    preprints = 0
    self_citations = 0
    old = 0
    years: list[int] = []
    venues: Counter[str] = Counter()
    kinds: Counter[str] = Counter()

    for metadata in entries:
        total += 1
        if metadata.doi:
            with_doi += 1
        if metadata.doi or metadata.arxiv_id:
            with_identifier += 1
        if _is_preprint(metadata):
            preprints += 1
        if surnames and _matches_author(metadata.authors, surnames):
            self_citations += 1
        if metadata.year is not None:
            years.append(metadata.year)
            if year_now - metadata.year > OLD_REFERENCE_YEARS:
                old += 1
        if metadata.venue:
            venues[metadata.venue] += 1
        kinds[metadata.kind.value] += 1

    return Stats(
        total=total,
        with_doi=with_doi,
        with_identifier=with_identifier,
        preprints=preprints,
        self_citations=self_citations,
        years=tuple(sorted(years)),
        venues=tuple(venues.most_common(5)),
        kinds=tuple(sorted(kinds.items(), key=lambda item: -item[1])),
        old_references=old,
        reference_year=year_now,
    )


def to_json(stats: Stats) -> dict[str, object]:
    return {
        "total": stats.total,
        "with_doi": stats.with_doi,
        "with_identifier": stats.with_identifier,
        "preprints": stats.preprints,
        "self_citations": stats.self_citations,
        "median_year": stats.median_year,
        "oldest_year": stats.oldest,
        "newest_year": stats.newest,
        "older_than_10y": stats.old_references,
        "venues": [{"name": name, "count": count} for name, count in stats.venues],
        "kinds": [{"kind": kind, "count": count} for kind, count in stats.kinds],
        "notes": list(stats.notes),
    }

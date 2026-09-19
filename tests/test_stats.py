"""Bibliography health metrics."""

from __future__ import annotations

from bibcheck.models import EntryKind, Metadata
from bibcheck.names import parse_author_field
from bibcheck.stats import OLD_REFERENCE_YEARS, summarise


def meta(**overrides: object) -> Metadata:
    base: dict[str, object] = {
        "title": "A Paper",
        "authors": parse_author_field("He, Kaiming"),
        "year": 2020,
        "venue": "Nature",
        "doi": "10.1000/x",
        "kind": EntryKind.JOURNAL_ARTICLE,
    }
    base.update(overrides)
    return Metadata(**base)  # type: ignore[arg-type]


def test_counts_and_shares() -> None:
    stats = summarise([meta(), meta(doi=None), meta()])
    assert stats.total == 3
    assert stats.with_doi == 2
    assert round(stats.share(stats.with_doi), 2) == 0.67


def test_year_range_and_median() -> None:
    stats = summarise([meta(year=2010), meta(year=2016), meta(year=2022)])
    assert stats.oldest == 2010
    assert stats.newest == 2022
    assert stats.median_year == 2016


def test_older_references_are_counted_against_a_fixed_year() -> None:
    """Pinned so the test does not start failing as time passes."""
    stats = summarise(
        [meta(year=2000), meta(year=2023)], reference_year=2024
    )
    assert stats.old_references == 1


def test_preprints_are_counted() -> None:
    stats = summarise(
        [meta(kind=EntryKind.PREPRINT, doi=None, arxiv_id="2004.05150"), meta()]
    )
    assert stats.preprints == 1


def test_self_citations_match_on_surname() -> None:
    stats = summarise(
        [meta(), meta(authors=parse_author_field("Doe, Jane"))], self_authors=["He"]
    )
    assert stats.self_citations == 1


def test_self_citation_matching_folds_accents() -> None:
    """"Schölkopf" and "Scholkopf" are the same person."""
    stats = summarise(
        [meta(authors=parse_author_field(r"Sch{\"o}lkopf, Bernhard"))],
        self_authors=["Scholkopf"],
    )
    assert stats.self_citations == 1


def test_no_self_authors_means_no_self_citations() -> None:
    assert summarise([meta(), meta()]).self_citations == 0


def test_venues_are_ranked() -> None:
    stats = summarise([meta(venue="Nature"), meta(venue="Nature"), meta(venue="Science")])
    assert stats.venues[0] == ("Nature", 2)


def test_an_empty_bibliography_is_all_zeroes() -> None:
    stats = summarise([])
    assert stats.total == 0
    assert stats.median_year is None
    assert stats.notes == ()
    assert stats.share(0) == 0.0


def test_missing_years_do_not_break_the_median() -> None:
    stats = summarise([meta(year=None), meta(year=2020)])
    assert stats.median_year == 2020


class TestNotes:
    """Notes are prompts to look, so they must stay rare."""

    def test_a_healthy_bibliography_gets_no_notes(self) -> None:
        entries = [meta(year=2020 + (index % 4), venue=f"Venue {index}") for index in range(10)]
        assert summarise(entries, reference_year=2024).notes == ()

    def test_a_preprint_heavy_bibliography_is_noted(self) -> None:
        entries = [meta(kind=EntryKind.PREPRINT, doi=None, arxiv_id="1") for _ in range(6)]
        entries += [meta() for _ in range(4)]
        notes = summarise(entries).notes
        assert any("preprints" in note for note in notes)

    def test_heavy_self_citation_is_noted(self) -> None:
        notes = summarise([meta() for _ in range(5)], self_authors=["He"]).notes
        assert any("self-citations" in note for note in notes)

    def test_missing_identifiers_are_noted(self) -> None:
        notes = summarise([meta(doi=None, arxiv_id=None) for _ in range(5)]).notes
        assert any("no DOI" in note for note in notes)

    def test_an_old_bibliography_is_noted(self) -> None:
        entries = [meta(year=1990, venue=f"V{i}") for i in range(10)]
        notes = summarise(entries, reference_year=2024).notes
        assert any(f"{OLD_REFERENCE_YEARS} years old" in note for note in notes)

    def test_venue_concentration_is_noted(self) -> None:
        notes = summarise([meta(venue="Nature") for _ in range(10)]).notes
        assert any("Nature" in note for note in notes)

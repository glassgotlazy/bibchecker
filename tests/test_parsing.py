"""BibTeX parsing and projection onto the comparable metadata shape."""

from __future__ import annotations

from pathlib import Path

from bibcheck.models import Code, EntryKind, Severity
from bibcheck.normalize import PageRange
from bibcheck.parsing import (
    entry_metadata,
    parse_bibtex,
    parse_bibtex_file,
    structural_problems,
)


def test_parses_every_well_formed_entry(sample_bib: Path) -> None:
    parsed = parse_bibtex_file(sample_bib)
    keys = [entry.key for entry in parsed.entries]
    assert keys == [
        "vaswani2017attention",
        "he2016resnet",
        "lewis2020longformer",
        "accented2019",
        "fabricated2021",
        "noident2018",
        "baddoi2020",
        "incomplete2022",
    ]


def test_a_malformed_entry_does_not_lose_the_rest_of_the_file(sample_bib: Path) -> None:
    """The headline guarantee: one broken entry costs one entry, not the run."""
    parsed = parse_bibtex_file(sample_bib)
    codes = [problem.code for problem in parsed.problems]
    assert Code.MALFORMED_ENTRY in codes
    assert len(parsed.entries) == 8


def test_malformed_entry_problem_names_the_source_line(sample_bib: Path) -> None:
    parsed = parse_bibtex_file(sample_bib)
    malformed = [p for p in parsed.problems if p.code is Code.MALFORMED_ENTRY]
    assert malformed and malformed[0].severity is Severity.WARN
    assert "brokenentry2020" in malformed[0].message


def test_line_numbers_are_one_based(sample_bib: Path) -> None:
    """Editors count from one; a line number that is off by one is a bug report."""
    text = sample_bib.read_text(encoding="utf-8").splitlines()
    parsed = parse_bibtex_file(sample_bib)
    for entry in parsed.entries:
        assert entry.start_line is not None
        assert text[entry.start_line - 1].startswith("@")


def test_field_order_and_raw_values_are_preserved(sample_bib: Path) -> None:
    """``--fix`` depends on this: order and spelling survive a round trip."""
    entry = parse_bibtex_file(sample_bib).by_key()["lewis2020longformer"]
    assert entry.field_order == (
        "title",
        "author",
        "year",
        "eprint",
        "archivePrefix",
        "primaryClass",
    )
    assert entry.get("archiveprefix") == "arXiv"
    # Raw values keep their LaTeX; only the derived metadata is normalised.
    accented = parse_bibtex_file(sample_bib).by_key()["accented2019"]
    assert "\\'" in (accented.get("title") or "")


def test_duplicate_key_is_reported_and_the_first_definition_wins(
    duplicates_bib: Path,
) -> None:
    parsed = parse_bibtex_file(duplicates_bib)
    duplicate = [p for p in parsed.problems if p.code is Code.DUPLICATE_KEY]
    assert len(duplicate) == 1
    assert duplicate[0].local == "dup_a"
    assert "Attention Is All You Need" in (parsed.by_key()["dup_a"].get("title") or "")


def test_duplicate_field_is_reported_with_both_values(duplicates_bib: Path) -> None:
    parsed = parse_bibtex_file(duplicates_bib)
    duplicate = [p for p in parsed.problems if p.code is Code.DUPLICATE_FIELD]
    assert len(duplicate) == 1
    assert "First Title" in (duplicate[0].local or "")
    assert "Second Title" in (duplicate[0].authoritative or "")
    # The entry itself is still recovered and checkable.
    assert "dupfield" in parsed.by_key()


def test_empty_input_is_not_an_error() -> None:
    parsed = parse_bibtex("")
    assert parsed.entries == ()
    assert parsed.problems == ()


def test_comments_are_ignored() -> None:
    parsed = parse_bibtex("% just a comment\n@article{a, title={T}, year={2020}}\n")
    assert [entry.key for entry in parsed.entries] == ["a"]


def test_latin1_file_is_decoded_rather_than_crashing(tmp_path: Path) -> None:
    path = tmp_path / "legacy.bib"
    path.write_bytes("@article{a, title={Caf\xe9}, year={2020}}\n".encode("latin-1"))
    parsed = parse_bibtex_file(path)
    assert entry_metadata(parsed.entries[0]).title == "Café"


class TestEntryMetadata:
    def test_journal_article(self, sample_bib: Path) -> None:
        entry = parse_bibtex_file(sample_bib).by_key()["vaswani2017attention"]
        metadata = entry_metadata(entry)
        assert metadata.title == "Attention Is All You Need"
        assert metadata.year == 2017
        assert metadata.volume == "30"
        assert metadata.pages == PageRange("5998", "6008")
        assert metadata.doi == "10.5555/3295222.3295349"
        assert metadata.kind is EntryKind.JOURNAL_ARTICLE
        assert [name.family for name in metadata.authors] == [
            "Vaswani",
            "Shazeer",
            "Parmar",
        ]

    def test_conference_paper_uses_booktitle_as_venue(self, sample_bib: Path) -> None:
        entry = parse_bibtex_file(sample_bib).by_key()["he2016resnet"]
        metadata = entry_metadata(entry)
        assert metadata.kind is EntryKind.CONFERENCE_PAPER
        assert metadata.venue is not None and metadata.venue.startswith("Proc. IEEE")
        # A URL-form DOI is normalised down to the bare identifier.
        assert metadata.doi == "10.1109/cvpr.2016.90"

    def test_misc_with_an_eprint_is_recognised_as_a_preprint(
        self, sample_bib: Path
    ) -> None:
        """An @misc carrying an arXiv id is a preprint whatever it calls itself."""
        entry = parse_bibtex_file(sample_bib).by_key()["lewis2020longformer"]
        metadata = entry_metadata(entry)
        assert metadata.kind is EntryKind.PREPRINT
        assert metadata.arxiv_id == "2004.05150"

    def test_latex_accents_are_resolved_for_display(self, sample_bib: Path) -> None:
        entry = parse_bibtex_file(sample_bib).by_key()["accented2019"]
        metadata = entry_metadata(entry)
        assert metadata.title == "Café Culture and Naïve Bayes: A Study"
        assert [name.family for name in metadata.authors] == [
            "Pérez",
            "Schölkopf",
            "Erdős",
        ]
        # The elided page range is expanded.
        assert metadata.pages == PageRange("1234", "1256")

    def test_absent_fields_are_none_not_empty_strings(self, sample_bib: Path) -> None:
        """Absence must stay distinguishable from disagreement."""
        metadata = entry_metadata(parse_bibtex_file(sample_bib).by_key()["noident2018"])
        assert metadata.doi is None
        assert metadata.arxiv_id is None
        assert metadata.volume is None
        assert metadata.pages is None


class TestStructuralProblems:
    def test_missing_required_fields_are_reported(self, sample_bib: Path) -> None:
        entry = parse_bibtex_file(sample_bib).by_key()["incomplete2022"]
        fields = {problem.field for problem in structural_problems(entry)}
        assert fields == {"title", "author", "year"}

    def test_a_malformed_doi_is_reported(self, sample_bib: Path) -> None:
        entry = parse_bibtex_file(sample_bib).by_key()["baddoi2020"]
        codes = [problem.code for problem in structural_problems(entry)]
        assert Code.MALFORMED_DOI in codes

    def test_a_complete_entry_has_no_structural_problems(self, sample_bib: Path) -> None:
        entry = parse_bibtex_file(sample_bib).by_key()["vaswani2017attention"]
        assert structural_problems(entry) == ()

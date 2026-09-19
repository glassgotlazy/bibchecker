"""Reading a reference list out of a paper PDF.

The fixture is a two-column IEEE-style paper generated once and committed, so
the suite needs neither reportlab nor a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bibcheck.pdf import (
    PdfExtractionError,
    cited_markers,
    crosscheck_pdf,
    extract_text,
    find_reference_section,
    parse_reference,
    references_to_bib,
    split_references,
)


@pytest.fixture(scope="module")
def paper_pdf(data_dir: Path) -> Path:
    return data_dir / "pdf" / "paper.pdf"


@pytest.fixture(scope="module")
def paper_text(paper_pdf: Path) -> str:
    return extract_text(paper_pdf)


class TestExtraction:
    def test_text_comes_out_in_reading_order(self, paper_text: str) -> None:
        """Two columns must not interleave, or every reference is shredded."""
        assert "Attention is all you need" in paper_text
        assert paper_text.index("INTRODUCTION") < paper_text.index("REFERENCES")

    def test_a_non_pdf_is_rejected_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "not.pdf"
        path.write_text("I am plain text", encoding="utf-8")
        with pytest.raises(PdfExtractionError):
            extract_text(path)

    def test_bytes_are_accepted_as_well_as_a_path(self, paper_pdf: Path) -> None:
        """The web endpoint has bytes, never a path."""
        assert "REFERENCES" in extract_text(paper_pdf.read_bytes())


class TestSectioning:
    def test_the_references_section_is_found(self, paper_text: str) -> None:
        body, references = find_reference_section(paper_text)
        assert "[1] A. Vaswani" in references
        assert "INTRODUCTION" in body
        assert "Attention is all you need" not in body

    def test_a_document_with_no_references_yields_an_empty_section(self) -> None:
        body, references = find_reference_section("Just prose, no bibliography.")
        assert references == ""
        assert body.startswith("Just prose")

    def test_the_last_heading_wins(self) -> None:
        """Papers mention "references" in prose; only the real list counts."""
        text = "We give references below.\n\nREFERENCES\n[1] A. Author, \"T,\" 2020.\n"
        _, references = find_reference_section(text)
        assert references.startswith("[1]")

    def test_an_appendix_after_the_references_is_excluded(self) -> None:
        text = (
            "REFERENCES\n"
            + "[1] A. Author, \"A title,\" Journal, 2020.\n" * 6
            + "\nAPPENDIX A\nSupplementary derivations follow.\n"
        )
        _, references = find_reference_section(text)
        assert "Supplementary derivations" not in references


class TestSplitting:
    def test_bracketed_markers_split_cleanly(self, paper_text: str) -> None:
        _, block = find_reference_section(paper_text)
        found = split_references(block)
        assert [marker for marker, _ in found] == ["1", "2", "3", "4", "5", "6"]

    def test_wrapped_lines_are_rejoined(self, paper_text: str) -> None:
        """A DOI broken across a column break must come back whole."""
        _, block = find_reference_section(paper_text)
        found = dict(split_references(block))
        assert "10.1016/S0140-6736(97)11096-0" in found["4"]
        assert "\n" not in found["4"]

    def test_a_stray_bracketed_number_does_not_split_a_reference(self) -> None:
        """Markers must run 1, 2, 3..., or "[15]" inside a title cuts it up."""
        block = (
            '[1] A. Author, "A survey of [15] approaches," Journal, 2020.\n'
            '[2] B. Author, "Another title," Journal, 2021.\n'
        )
        found = split_references(block)
        assert [marker for marker, _ in found] == ["1", "2"]
        assert "[15]" in found[0][1]

    def test_plain_numbered_references_are_handled(self) -> None:
        block = (
            '1. A. Author, "First title," Journal, 2020.\n'
            '2. B. Author, "Second title," Journal, 2021.\n'
        )
        assert [marker for marker, _ in split_references(block)] == ["1", "2"]

    def test_an_empty_block_yields_nothing(self) -> None:
        assert split_references("") == []
        assert split_references("   \n  ") == []


class TestParsing:
    def test_a_full_ieee_reference(self) -> None:
        raw = (
            'K. He, X. Zhang, S. Ren, and J. Sun, "Deep residual learning for image '
            'recognition," in Proc. IEEE Conf. Comput. Vis. Pattern Recognit. (CVPR), '
            "2016, pp. 770-778. doi: 10.1109/CVPR.2016.90."
        )
        reference = parse_reference("2", raw)
        assert reference.title == "Deep residual learning for image recognition"
        assert reference.authors == "K. He, X. Zhang, S. Ren, and J. Sun"
        assert reference.year == "2016"
        assert reference.pages == "770--778"
        assert reference.doi == "10.1109/cvpr.2016.90"
        assert reference.confidence == 1.0

    def test_a_doi_without_the_doi_prefix_is_still_found(self) -> None:
        reference = parse_reference(
            "1", 'A. B., "A title," Journal, 2020, 10.1000/xyz123.'
        )
        assert reference.doi == "10.1000/xyz123"

    def test_an_arxiv_preprint(self) -> None:
        raw = (
            'I. Beltagy, M. E. Peters, and A. Cohan, "Longformer: The long-document '
            'transformer," arXiv preprint arXiv:2004.05150, 2020.'
        )
        reference = parse_reference("3", raw)
        assert reference.arxiv_id == "2004.05150"
        assert reference.title == "Longformer: The long-document transformer"
        assert reference.venue == "arXiv"

    def test_volume_issue_and_pages(self) -> None:
        raw = (
            'A. Wakefield, "Ileal-lymphoid-nodular hyperplasia," '
            "The Lancet, vol. 351, no. 9103, pp. 637-641, 1998."
        )
        reference = parse_reference("4", raw)
        assert reference.volume == "351"
        assert reference.issue == "9103"
        assert reference.pages == "637--641"
        assert reference.venue == "The Lancet"

    def test_an_author_year_style_reference(self) -> None:
        raw = "Doe, J. and Roe, R. (2021). Quantum architectures. Journal of Advanced Computing."
        reference = parse_reference("1", raw)
        assert reference.year == "2021"
        assert reference.title == "Quantum architectures"

    def test_a_reference_with_no_identifier_scores_lower(self) -> None:
        with_doi = parse_reference("1", 'A. B., "A title," J, 2020. doi: 10.1000/x')
        without = parse_reference("2", 'A. B., "A title," J, 2020.')
        assert with_doi.confidence > without.confidence

    def test_unparseable_junk_does_not_raise(self) -> None:
        reference = parse_reference("1", "|||| ???? ////")
        assert not reference.usable
        assert reference.confidence == 0.0

    def test_the_raw_text_is_always_kept(self) -> None:
        """The report shows what was read off the page, since extraction lies."""
        raw = 'A. B., "A title," Journal, 2020.'
        assert parse_reference("1", raw).raw == raw


class TestToEntries:
    def test_every_reference_becomes_a_checkable_entry(self, paper_text: str) -> None:
        parsed, references = references_to_bib(paper_text, source="paper.pdf")
        assert len(parsed.entries) == 6
        assert [entry.key for entry in parsed.entries] == [f"ref{n}" for n in range(1, 7)]
        assert len(references) == 6

    def test_keys_map_back_to_the_pdf(self, paper_text: str) -> None:
        """``ref4`` sends the reader straight to ``[4]`` on the page."""
        parsed, _ = references_to_bib(paper_text)
        assert parsed.by_key()["ref4"].get("doi") == "10.1016/s0140-6736(97)11096-0"

    def test_entry_types_are_inferred(self, paper_text: str) -> None:
        entries = references_to_bib(paper_text)[0].by_key()
        assert entries["ref2"].entry_type == "inproceedings"
        assert entries["ref3"].entry_type == "misc"  # arXiv preprint
        assert entries["ref4"].entry_type == "article"

    def test_authors_are_converted_to_bibtex_form(self, paper_text: str) -> None:
        entries = references_to_bib(paper_text)[0].by_key()
        assert entries["ref2"].get("author") == "K. He and X. Zhang and S. Ren and J. Sun"

    def test_the_raw_text_rides_along_on_the_entry(self, paper_text: str) -> None:
        entries = references_to_bib(paper_text)[0].by_key()
        assert entries["ref4"].raw is not None
        assert "Wakefield" in entries["ref4"].raw

    def test_an_unparseable_reference_is_reported_not_dropped(self) -> None:
        """A vanished reference would be a silent lie about the count."""
        text = 'REFERENCES\n[1] ||||\n[2] A. B., "A real title," Journal, 2020.\n'
        parsed, references = references_to_bib(text)
        assert len(parsed.entries) == 1
        assert len(references) == 1
        assert any("could not be parsed" in p.message for p in parsed.problems)

    def test_a_document_with_no_references_says_so(self) -> None:
        parsed, references = references_to_bib("Just prose.")
        assert parsed.entries == ()
        assert references == []
        assert any("no references section" in p.message for p in parsed.problems)


class TestCitationMarkers:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("as in [1]", {"1"}),
            ("see [1], [2]", {"1", "2"}),
            ("see [1, 2, 3]", {"1", "2", "3"}),
            ("see [1]-[3]", {"1", "3"}),
            ("see [1-3]", {"1", "2", "3"}),
            ("see [1–3]", {"1", "2", "3"}),
            ("no citations", set()),
        ],
    )
    def test_marker_forms(self, body: str, expected: set[str]) -> None:
        assert cited_markers(body) == expected

    def test_the_pdf_body_drives_the_crosscheck(self, paper_text: str) -> None:
        body, _ = find_reference_section(paper_text)
        parsed, _ = references_to_bib(paper_text)
        result = crosscheck_pdf(body, [entry.key for entry in parsed.entries])
        assert result.undefined == ()
        assert result.uncited == ()

    def test_an_uncited_reference_is_found(self) -> None:
        result = crosscheck_pdf("we cite [1] only", ["ref1", "ref2", "ref3"])
        assert result.uncited == ("ref2", "ref3")

    def test_a_citation_with_no_reference_is_found(self) -> None:
        result = crosscheck_pdf("we cite [1] and [9]", ["ref1"])
        assert result.undefined == ("ref9",)

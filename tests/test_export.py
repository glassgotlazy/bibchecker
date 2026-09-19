"""Writing a .bib from checked references — the payoff of reading a PDF."""

from __future__ import annotations

from bibcheck.export import to_bibtex
from bibcheck.models import EntryKind, EntryReport, Metadata, Report, WorkRecord
from bibcheck.names import parse_author_field
from bibcheck.normalize import parse_pages
from bibcheck.parsing import parse_bibtex


def _entry(key: str, *, resolved: bool = True, **overrides: object) -> EntryReport:
    base: dict[str, object] = {
        "title": "Deep Residual Learning for Image Recognition",
        "authors": parse_author_field("He, Kaiming and Zhang, Xiangyu"),
        "year": 2016,
        "venue": "IEEE Conference on Computer Vision and Pattern Recognition",
        "pages": parse_pages("770--778"),
        "doi": "10.1109/cvpr.2016.90",
        "kind": EntryKind.CONFERENCE_PAPER,
    }
    base.update(overrides)
    metadata = Metadata(**base)  # type: ignore[arg-type]
    return EntryReport(
        key=key,
        entry_type="inproceedings",
        resolved=resolved,
        metadata=metadata,
        record=WorkRecord(metadata=metadata) if resolved else None,
    )


def test_output_is_parseable_bibtex() -> None:
    """The obvious round trip, and the one that actually matters."""
    text = to_bibtex(Report(bib_path="p.pdf", entries=(_entry("ref1"), _entry("ref2"))))
    parsed = parse_bibtex(text)
    assert [entry.key for entry in parsed.entries] == ["ref1", "ref2"]
    assert parsed.problems == ()


def test_authoritative_metadata_is_preferred() -> None:
    local = Metadata(title="Typo'd Titel", year=1999, doi="10.1109/x")
    authoritative = Metadata(
        title="The Correct Title", year=2016, doi="10.1109/x", kind=EntryKind.JOURNAL_ARTICLE
    )
    report = Report(
        bib_path="p.pdf",
        entries=(
            EntryReport(
                key="ref1",
                entry_type="article",
                resolved=True,
                metadata=local,
                record=WorkRecord(metadata=authoritative),
            ),
        ),
    )
    text = to_bibtex(report)
    assert "The Correct Title" in text
    assert "Typo'd Titel" not in text


def test_an_unresolved_entry_is_kept_but_marked() -> None:
    """Nothing silently disappears from a bibliography."""
    text = to_bibtex(Report(bib_path="p.pdf", entries=(_entry("ref1", resolved=False),)))
    assert "ref1" in text
    assert "unverified" in text


def test_a_conference_paper_uses_booktitle() -> None:
    text = to_bibtex(Report(bib_path="p.pdf", entries=(_entry("ref1"),)))
    assert "@inproceedings{ref1," in text
    assert "booktitle" in text
    assert "journal" not in text


def test_a_journal_article_uses_journal() -> None:
    entry = _entry("ref1", kind=EntryKind.JOURNAL_ARTICLE, venue="Nature")
    text = to_bibtex(Report(bib_path="p.pdf", entries=(entry,)))
    assert "journal" in text
    assert "booktitle" not in text


def test_a_preprint_does_not_repeat_the_server_as_a_journal() -> None:
    entry = _entry(
        "ref1", kind=EntryKind.PREPRINT, venue="arXiv", arxiv_id="2004.05150", doi=None
    )
    text = to_bibtex(Report(bib_path="p.pdf", entries=(entry,)))
    assert "eprint" in text
    assert "archiveprefix" in text
    assert "journal" not in text


def test_special_characters_are_escaped() -> None:
    entry = _entry("ref1", title="Cost & Benefit of 50% Coverage_Analysis")
    text = to_bibtex(Report(bib_path="p.pdf", entries=(entry,)))
    assert r"\&" in text
    assert r"\%" in text
    assert r"\_" in text
    assert parse_bibtex(text).problems == ()


def test_page_ranges_use_the_bibtex_double_dash() -> None:
    text = to_bibtex(Report(bib_path="p.pdf", entries=(_entry("ref1"),)))
    assert "770--778" in text


def test_a_header_comment_is_included() -> None:
    text = to_bibtex(Report(bib_path="p.pdf", entries=(_entry("ref1"),)), header="made by x")
    assert text.startswith("% made by x")
    assert parse_bibtex(text).problems == ()


def test_an_empty_report_produces_no_entries() -> None:
    assert parse_bibtex(to_bibtex(Report(bib_path="p.pdf"))).entries == ()


def test_an_entry_with_no_metadata_is_skipped_silently() -> None:
    report = Report(
        bib_path="p.pdf",
        entries=(EntryReport(key="ref1", entry_type="article", metadata=None),),
    )
    assert parse_bibtex(to_bibtex(report)).entries == ()

"""``--fix``. The promises are structural, so the tests assert them byte-wise."""

from __future__ import annotations

from pathlib import Path

import pytest

from bibcheck.fixer import Fix, apply_fixes, plan_fixes, write_fixed
from bibcheck.models import (
    Code,
    EntryReport,
    Metadata,
    Problem,
    Report,
    Severity,
    WorkRecord,
)
from bibcheck.parsing import parse_bibtex

SOURCE = """% A leading comment that must survive.

@article{drifted,
  title   = {A Paper With A Wrong Year},
  author  = {Doe, Jane},
  journal = {Journal of Things},
  volume  = {12},
  pages   = {45--67},
  year    = {2021},
  doi     = {10.1000/drift.2019}
}

@inproceedings{untouched,
  title     = {Something Else},
  author    = {Roe, Richard},
  booktitle = {Proceedings},
  year      = {2019}
}
"""


def _entry(
    key: str,
    problems: tuple[Problem, ...],
    *,
    confidence: float = 1.0,
    resolved: bool = True,
    doi: str | None = "10.1000/drift.2019",
) -> EntryReport:
    return EntryReport(
        key=key,
        entry_type="article",
        resolved=resolved,
        problems=problems,
        metadata=Metadata(title="A Paper", doi=doi),
        record=WorkRecord(
            metadata=Metadata(title="A Paper", doi="10.1000/drift.2019"),
            match_confidence=confidence,
        ),
    )


def _year_problem() -> Problem:
    return Problem(
        code=Code.YEAR_MISMATCH,
        severity=Severity.WARN,
        message="year differs",
        field="year",
        local="2021",
        authoritative="2019",
    )


class TestPlanning:
    def test_a_year_mismatch_is_planned(self) -> None:
        report = Report(bib_path="r.bib", entries=(_entry("drifted", (_year_problem(),)),))
        fixes = plan_fixes(report)
        assert [(f.field, f.new) for f in fixes] == [("year", "2019")]

    def test_a_critical_entry_is_skipped_entirely(self) -> None:
        """A retracted paper needs a decision about whether to cite it at all;
        silently correcting its page numbers would be absurd."""
        problems = (
            _year_problem(),
            Problem(code=Code.RETRACTED, severity=Severity.CRITICAL, message="retracted"),
        )
        report = Report(bib_path="r.bib", entries=(_entry("drifted", problems),))
        assert plan_fixes(report) == []

    def test_a_fuzzy_title_match_does_not_authorise_field_edits(self) -> None:
        report = Report(
            bib_path="r.bib",
            entries=(_entry("drifted", (_year_problem(),), confidence=0.88),),
        )
        assert [f.field for f in plan_fixes(report)] == []

    def test_an_unresolved_entry_is_skipped(self) -> None:
        report = Report(
            bib_path="r.bib",
            entries=(_entry("drifted", (_year_problem(),), resolved=False),),
        )
        assert plan_fixes(report) == []

    @pytest.mark.parametrize(
        "code",
        [Code.TITLE_MISMATCH, Code.AUTHOR_MISMATCH, Code.VENUE_MISMATCH],
    )
    def test_subjective_fields_are_never_rewritten(self, code: Code) -> None:
        """A wrong title may mean the wrong work entirely; an abbreviated venue
        is a deliberate house style. Both are reported, never rewritten."""
        problem = Problem(
            code=code,
            severity=Severity.WARN,
            message="differs",
            field="title",
            local="a",
            authoritative="b",
        )
        report = Report(bib_path="r.bib", entries=(_entry("drifted", (problem,)),))
        assert plan_fixes(report) == []

    def test_a_missing_doi_is_added_only_on_a_near_certain_match(self) -> None:
        confident = Report(
            bib_path="r.bib",
            entries=(_entry("x", (), confidence=0.97, doi=None),),
        )
        assert [(f.field, f.added) for f in plan_fixes(confident)] == [("doi", True)]

        shaky = Report(
            bib_path="r.bib", entries=(_entry("x", (), confidence=0.80, doi=None),)
        )
        assert plan_fixes(shaky) == []


class TestApplying:
    def test_only_the_targeted_value_changes(self) -> None:
        """The headline guarantee, asserted line by line."""
        parsed = parse_bibtex(SOURCE)
        fixed, applied = apply_fixes(
            SOURCE, parsed, [Fix(key="drifted", field="year", old="2021", new="2019")]
        )
        assert len(applied) == 1
        before, after = SOURCE.splitlines(), fixed.splitlines()
        assert len(before) == len(after)
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(differing) == 1
        assert before[differing[0]].strip() == "year    = {2021},"
        assert after[differing[0]].strip() == "year    = {2019},"

    def test_comments_and_untouched_entries_survive_verbatim(self) -> None:
        parsed = parse_bibtex(SOURCE)
        fixed, _ = apply_fixes(
            SOURCE, parsed, [Fix(key="drifted", field="year", old="2021", new="2019")]
        )
        assert fixed.startswith("% A leading comment that must survive.")
        assert "@inproceedings{untouched," in fixed
        assert "  booktitle = {Proceedings}," in fixed

    def test_field_order_and_alignment_are_preserved(self) -> None:
        parsed = parse_bibtex(SOURCE)
        fixed, _ = apply_fixes(
            SOURCE, parsed, [Fix(key="drifted", field="year", old="2021", new="2019")]
        )
        reparsed = parse_bibtex(fixed)
        original = parse_bibtex(SOURCE)
        for left, right in zip(original.entries, reparsed.entries, strict=True):
            assert left.key == right.key
            assert left.field_order == right.field_order

    def test_several_fixes_in_one_entry_all_apply(self) -> None:
        parsed = parse_bibtex(SOURCE)
        fixed, applied = apply_fixes(
            SOURCE,
            parsed,
            [
                Fix(key="drifted", field="year", old="2021", new="2019"),
                Fix(key="drifted", field="volume", old="12", new="13"),
                Fix(key="drifted", field="pages", old="45--67", new="45-70"),
            ],
        )
        assert len(applied) == 3
        assert "year    = {2019}" in fixed
        assert "volume  = {13}" in fixed
        assert "pages   = {45-70}" in fixed

    def test_a_quoted_value_keeps_its_quotes(self) -> None:
        source = '@article{a,\n  year = "2021",\n  title = {T}\n}\n'
        fixed, applied = apply_fixes(
            source, parse_bibtex(source), [Fix(key="a", field="year", old="2021", new="2019")]
        )
        assert applied
        assert 'year = "2019"' in fixed

    def test_a_bare_value_stays_bare(self) -> None:
        source = "@article{a,\n  year = 2021,\n  title = {T}\n}\n"
        fixed, _ = apply_fixes(
            source, parse_bibtex(source), [Fix(key="a", field="year", old="2021", new="2019")]
        )
        assert "year = 2019," in fixed

    def test_nested_braces_do_not_truncate_a_value(self) -> None:
        """``{A {Study}}`` must not end at the first closing brace."""
        source = "@article{a,\n  title = {A {Study} of Things},\n  year = {2021}\n}\n"
        fixed, _ = apply_fixes(
            source, parse_bibtex(source), [Fix(key="a", field="title", old="x", new="New Title")]
        )
        assert "title = {New Title}," in fixed
        assert "year = {2021}" in fixed

    def test_a_field_that_is_not_there_is_reported_as_skipped(self) -> None:
        parsed = parse_bibtex(SOURCE)
        _, applied = apply_fixes(
            SOURCE, parsed, [Fix(key="drifted", field="issn", old="x", new="y")]
        )
        assert applied == []

    def test_an_inserted_field_is_aligned_with_the_existing_ones(self) -> None:
        """An inserted line should look like it was always there."""
        parsed = parse_bibtex(SOURCE)
        fixed, _ = apply_fixes(
            SOURCE,
            parsed,
            [Fix(key="untouched", field="doi", old=None, new="10.1/new", added=True)],
        )
        added = next(line for line in fixed.splitlines() if "10.1/new" in line)
        sample = next(line for line in fixed.splitlines() if "booktitle" in line)
        assert added.index("=") == sample.index("=")

    def test_an_added_doi_is_inserted_into_the_right_entry(self) -> None:
        parsed = parse_bibtex(SOURCE)
        fixed, applied = apply_fixes(
            SOURCE,
            parsed,
            [Fix(key="untouched", field="doi", old=None, new="10.1/new", added=True)],
        )
        assert len(applied) == 1
        reparsed = parse_bibtex(fixed).by_key()
        assert reparsed["untouched"].get("doi") == "10.1/new"
        assert reparsed["drifted"].get("doi") == "10.1000/drift.2019"
        # Appended at the end, so the author's field order is untouched.
        assert reparsed["untouched"].field_order[-1] == "doi"

    def test_no_fixes_returns_the_source_unchanged(self) -> None:
        fixed, applied = apply_fixes(SOURCE, parse_bibtex(SOURCE), [])
        assert fixed == SOURCE
        assert applied == []

    def test_the_result_still_parses(self) -> None:
        parsed = parse_bibtex(SOURCE)
        fixed, _ = apply_fixes(
            SOURCE, parsed, [Fix(key="drifted", field="year", old="2021", new="2019")]
        )
        assert len(parse_bibtex(fixed).entries) == 2
        assert parse_bibtex(fixed).problems == ()


class TestWriting:
    def test_it_refuses_to_write_over_its_input(self, tmp_path: Path) -> None:
        """--fix exists to produce something diffable; clobbering defeats it."""
        source = tmp_path / "refs.bib"
        source.write_text(SOURCE, encoding="utf-8")
        with pytest.raises(ValueError, match="different file"):
            write_fixed(source, source, "anything")
        assert source.read_text(encoding="utf-8") == SOURCE

    def test_it_refuses_to_clobber_an_existing_file(self, tmp_path: Path) -> None:
        source = tmp_path / "refs.bib"
        source.write_text(SOURCE, encoding="utf-8")
        target = tmp_path / "fixed.bib"
        target.write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError):
            write_fixed(source, target, "new")
        assert target.read_text(encoding="utf-8") == "existing"

    def test_force_allows_an_overwrite(self, tmp_path: Path) -> None:
        source = tmp_path / "refs.bib"
        source.write_text(SOURCE, encoding="utf-8")
        target = tmp_path / "fixed.bib"
        target.write_text("existing", encoding="utf-8")
        write_fixed(source, target, "new", force=True)
        assert target.read_text(encoding="utf-8") == "new"

    def test_a_symlink_to_the_input_is_also_refused(self, tmp_path: Path) -> None:
        """Resolving the path stops an alias from sneaking past the check."""
        source = tmp_path / "refs.bib"
        source.write_text(SOURCE, encoding="utf-8")
        alias = tmp_path / "alias.bib"
        alias.symlink_to(source)
        with pytest.raises(ValueError):
            write_fixed(source, alias, "anything")

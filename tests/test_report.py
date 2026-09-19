"""Rendering. Colour is asserted by its absence as much as its presence."""

from __future__ import annotations

import io
import json

from bibcheck.models import (
    Code,
    CrossCheck,
    EntryReport,
    Problem,
    Report,
    Severity,
    Status,
)
from bibcheck.report import STATUS_GLYPH, make_console, render_human, render_json


def _report(**overrides: object) -> Report:
    base: dict[str, object] = {
        "bib_path": "refs.bib",
        "entries": (
            EntryReport(key="clean", entry_type="article", resolved=True),
            EntryReport(
                key="drifted",
                entry_type="article",
                resolved=True,
                problems=(
                    Problem(
                        code=Code.YEAR_MISMATCH,
                        severity=Severity.WARN,
                        message="year differs",
                        field="year",
                        local="2021",
                        authoritative="2019",
                    ),
                ),
            ),
            EntryReport(
                key="fabricated",
                entry_type="article",
                resolved=False,
                problems=(
                    Problem(
                        code=Code.DOI_NOT_FOUND,
                        severity=Severity.CRITICAL,
                        message="DOI does not resolve",
                        field="doi",
                        local="10.9999/fake",
                    ),
                ),
            ),
        ),
    }
    base.update(overrides)
    return Report(**base)  # type: ignore[arg-type]


def _render(report: Report, *, color: bool | None = False, **kwargs: object) -> str:
    buffer = io.StringIO()
    console = make_console(file=buffer, color=color, width=100)
    render_human(report, console, **kwargs)  # type: ignore[arg-type]
    return buffer.getvalue()


class TestHuman:
    def test_every_key_and_glyph_appears(self) -> None:
        output = _render(_report())
        for key in ("clean", "drifted", "fabricated"):
            assert key in output
        assert STATUS_GLYPH[Status.OK] in output
        assert STATUS_GLYPH[Status.CRITICAL] in output

    def test_drift_shows_both_values(self) -> None:
        """The point of the whole report: see what differs without a browser."""
        output = _render(_report())
        assert "2021" in output
        assert "2019" in output

    def test_the_tally_counts_every_status(self) -> None:
        output = _render(_report())
        assert "1 ok" in output
        assert "1 warn" in output
        assert "1 critical" in output

    def test_quiet_hides_clean_entries_only(self) -> None:
        output = _render(_report(), show_ok=False)
        assert "clean" not in output
        assert "fabricated" in output

    def test_a_clean_bibliography_says_so(self) -> None:
        report = Report(
            bib_path="refs.bib",
            entries=(EntryReport(key="a", entry_type="article", resolved=True),),
        )
        assert "every entry checked out" in _render(report, show_ok=False)

    def test_an_empty_bibliography_does_not_crash(self) -> None:
        assert "nothing to report" in _render(Report(bib_path="refs.bib"))

    def test_parse_problems_are_shown(self) -> None:
        report = _report(
            parse_problems=(
                Problem(
                    code=Code.MALFORMED_ENTRY,
                    severity=Severity.WARN,
                    message="unparseable entry at line 40",
                ),
            )
        )
        assert "unparseable entry at line 40" in _render(report)

    def test_undefined_keys_are_reported(self) -> None:
        report = _report(crosscheck=CrossCheck(undefined=("ghost",), uncited=("clean",)))
        output = _render(report)
        assert "ghost" in output
        assert "cited but missing" in output

    def test_uncited_entries_are_not_listed_twice(self) -> None:
        """Each uncited entry carries its own line; repeating them is noise."""
        report = _report(crosscheck=CrossCheck(uncited=("clean", "drifted")))
        assert "never cited" not in _render(report)

    def test_elapsed_time_is_shown_when_given(self) -> None:
        assert "1.5s" in _render(_report(), elapsed=1.5)

    def test_offline_is_labelled_in_the_header(self) -> None:
        assert "cache only" in _render(_report(offline=True))

    def test_a_long_key_does_not_break_the_layout(self) -> None:
        report = _report(
            entries=(
                EntryReport(key="x" * 60, entry_type="article", resolved=True),
                EntryReport(key="short", entry_type="article", resolved=True),
            )
        )
        output = _render(report)
        assert "x" * 60 in output
        assert "short" in output


class TestColour:
    def test_colour_is_emitted_when_requested(self) -> None:
        assert "\x1b[" in _render(_report(), color=True)

    def test_no_colour_when_not_a_terminal(self) -> None:
        """A CI log or a piped file must contain no escape sequences."""
        assert "\x1b[" not in _render(_report(), color=False)

    def test_the_same_information_survives_without_colour(self) -> None:
        """Colour must be redundant with the glyph, never the only signal."""
        plain = _render(_report(), color=False)
        assert STATUS_GLYPH[Status.CRITICAL] in plain
        assert "1 critical" in plain


class TestJson:
    def test_it_is_valid_and_complete(self) -> None:
        payload = json.loads(render_json(_report()))
        assert payload["summary"]["critical"] == 1
        assert [entry["key"] for entry in payload["entries"]] == [
            "clean",
            "drifted",
            "fabricated",
        ]

    def test_problems_carry_both_values(self) -> None:
        payload = json.loads(render_json(_report()))
        problem = payload["entries"][1]["problems"][0]
        assert problem["local"] == "2021"
        assert problem["authoritative"] == "2019"
        assert problem["code"] == "year_mismatch"

    def test_output_is_stable_between_runs(self) -> None:
        assert render_json(_report()) == render_json(_report())

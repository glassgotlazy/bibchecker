"""The data model, in particular that status is derived rather than assigned."""

from __future__ import annotations

from bibcheck.models import (
    Code,
    EntryReport,
    Problem,
    Report,
    Severity,
    Status,
)


def _problem(severity: Severity, code: Code = Code.YEAR_MISMATCH) -> Problem:
    return Problem(code=code, severity=severity, message="…")


def test_a_clean_resolved_entry_is_ok() -> None:
    assert EntryReport(key="a", entry_type="article", resolved=True).status is Status.OK


def test_an_unresolved_entry_is_never_reported_as_ok() -> None:
    """Not having checked is different from having checked and found nothing."""
    assert EntryReport(key="a", entry_type="article").status is Status.UNRESOLVED


def test_a_network_failure_outranks_every_other_signal() -> None:
    report = EntryReport(
        key="a",
        entry_type="article",
        resolved=True,
        network_failed=True,
        problems=(_problem(Severity.WARN),),
    )
    assert report.status is Status.NETWORK_ERROR


def test_a_critical_finding_outranks_being_unresolved() -> None:
    report = EntryReport(
        key="a",
        entry_type="article",
        resolved=False,
        problems=(_problem(Severity.CRITICAL, Code.RETRACTED),),
    )
    assert report.status is Status.CRITICAL


def test_the_worst_severity_decides_the_status() -> None:
    report = EntryReport(
        key="a",
        entry_type="article",
        resolved=True,
        problems=(
            _problem(Severity.INFO),
            _problem(Severity.CRITICAL),
            _problem(Severity.WARN),
        ),
    )
    assert report.worst_severity is Severity.CRITICAL
    assert report.status is Status.CRITICAL


def test_info_findings_alone_leave_an_entry_ok() -> None:
    report = EntryReport(
        key="a", entry_type="article", resolved=True, problems=(_problem(Severity.INFO),)
    )
    assert report.status is Status.OK


def test_report_counts_every_status() -> None:
    report = Report(
        bib_path="refs.bib",
        entries=(
            EntryReport(key="a", entry_type="article", resolved=True),
            EntryReport(key="b", entry_type="article", resolved=True),
            EntryReport(
                key="c",
                entry_type="article",
                resolved=True,
                problems=(_problem(Severity.CRITICAL),),
            ),
            EntryReport(key="d", entry_type="article"),
        ),
    )
    counts = report.counts
    assert counts[Status.OK] == 2
    assert counts[Status.CRITICAL] == 1
    assert counts[Status.UNRESOLVED] == 1
    assert counts[Status.WARN] == 0
    assert sum(counts.values()) == len(report.entries)


def test_worst_status_drives_fail_on() -> None:
    clean = Report(
        bib_path="refs.bib",
        entries=(EntryReport(key="a", entry_type="article", resolved=True),),
    )
    assert clean.worst_status is Status.OK

    dirty = Report(
        bib_path="refs.bib",
        entries=(
            EntryReport(key="a", entry_type="article", resolved=True),
            EntryReport(
                key="b",
                entry_type="article",
                resolved=True,
                problems=(_problem(Severity.WARN),),
            ),
            EntryReport(
                key="c",
                entry_type="article",
                resolved=True,
                problems=(_problem(Severity.CRITICAL),),
            ),
        ),
    )
    assert dirty.worst_status is Status.CRITICAL


def test_problem_json_omits_absent_fields() -> None:
    payload = Problem(
        code=Code.YEAR_MISMATCH,
        severity=Severity.WARN,
        message="year differs",
        field="year",
        local="2020",
        authoritative="2019",
    ).to_json()
    assert payload == {
        "code": "year_mismatch",
        "severity": "warn",
        "message": "year differs",
        "field": "year",
        "local": "2020",
        "authoritative": "2019",
    }
    bare = Problem(code=Code.RETRACTED, severity=Severity.CRITICAL, message="x").to_json()
    assert set(bare) == {"code", "severity", "message"}


def test_report_json_is_serialisable() -> None:
    import json

    report = Report(
        bib_path="refs.bib",
        entries=(
            EntryReport(
                key="a",
                entry_type="article",
                resolved=True,
                start_line=3,
                problems=(_problem(Severity.WARN),),
            ),
        ),
    )
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["summary"]["warn"] == 1
    assert payload["entries"][0]["key"] == "a"
    assert payload["entries"][0]["line"] == 3

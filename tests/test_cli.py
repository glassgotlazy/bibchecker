"""The command line: exit codes, flags, and what lands on which stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import respx

from bibcheck.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main

Loader = Callable[[str], dict[str, Any]]
WORKS = "https://api.crossref.org/works"

BIB = """@inproceedings{good,
  title     = {Deep Residual Learning for Image Recognition},
  author    = {He, Kaiming and Zhang, Xiangyu},
  booktitle = {CVPR},
  pages     = {770--778},
  year      = {2016},
  doi       = {10.1109/CVPR.2016.90}
}

@article{fake,
  title   = {Quantum Neural Architectures},
  author  = {Doe, Jane},
  journal = {J. Adv. Comp.},
  year    = {2021},
  doi     = {10.9999/jac.2021.99999}
}
"""


@pytest.fixture
def bib_file(tmp_path: Path) -> Path:
    path = tmp_path / "refs.bib"
    path.write_text(BIB, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the developer's real cache or config."""
    monkeypatch.setenv("BIBCHECK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)


def _mock(crossref_fixture: Loader) -> None:
    respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
        return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
    )
    respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
        return_value=httpx.Response(404, json=crossref_fixture("not_found"))
    )


class TestExitCodes:
    @respx.mock
    def test_clean_run_without_fail_on_exits_zero(
        self, bib_file: Path, crossref_fixture: Loader
    ) -> None:
        """Findings alone never fail the build; --fail-on is opt-in."""
        _mock(crossref_fixture)
        assert main([str(bib_file)]) == EXIT_OK

    @respx.mock
    def test_fail_on_critical_exits_one(
        self, bib_file: Path, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        assert main([str(bib_file), "--fail-on", "critical"]) == EXIT_FAILED

    @respx.mock
    def test_fail_on_warn_also_catches_critical(
        self, bib_file: Path, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        assert main([str(bib_file), "--fail-on", "warn"]) == EXIT_FAILED

    @respx.mock
    def test_fail_on_unresolved_is_not_triggered_by_a_bad_doi(
        self, bib_file: Path, crossref_fixture: Loader
    ) -> None:
        """A DOI that resolves to "no such work" is CRITICAL, not unresolved."""
        _mock(crossref_fixture)
        assert main([str(bib_file), "--fail-on", "unresolved"]) == EXIT_OK

    def test_a_missing_file_is_a_usage_error(self, tmp_path: Path) -> None:
        assert main([str(tmp_path / "nope.bib")]) == EXIT_USAGE

    def test_an_unknown_fail_on_level_is_a_usage_error(self, bib_file: Path) -> None:
        assert main([str(bib_file), "--fail-on", "nonsense"]) == EXIT_USAGE

    def test_no_arguments_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == EXIT_USAGE
        assert "usage:" in capsys.readouterr().out


class TestOutput:
    @respx.mock
    def test_the_report_names_every_entry(
        self, bib_file: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _mock(crossref_fixture)
        main([str(bib_file)])
        out = capsys.readouterr().out
        assert "good" in out
        assert "fake" in out
        assert "1 critical" in out

    @respx.mock
    def test_output_is_plain_when_not_a_terminal(
        self, bib_file: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Captured output is not a TTY, so no escape codes may appear."""
        _mock(crossref_fixture)
        main([str(bib_file)])
        assert "\x1b[" not in capsys.readouterr().out

    @respx.mock
    def test_no_color_is_honoured_even_when_forced(
        self, bib_file: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _mock(crossref_fixture)
        main([str(bib_file), "--no-color"])
        assert "\x1b[" not in capsys.readouterr().out

    @respx.mock
    def test_quiet_hides_passing_entries(
        self, bib_file: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _mock(crossref_fixture)
        main([str(bib_file), "--quiet"])
        out = capsys.readouterr().out
        assert "fake" in out
        assert "\n  ✓" not in out

    @respx.mock
    def test_json_to_a_file(
        self, bib_file: Path, tmp_path: Path, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        target = tmp_path / "report.json"
        main([str(bib_file), "--json", str(target)])
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["summary"]["critical"] == 1

    @respx.mock
    def test_json_to_stdout_keeps_stdout_machine_readable(
        self, bib_file: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--json - > report.json` must produce valid JSON, so the human
        report moves to stderr."""
        _mock(crossref_fixture)
        main([str(bib_file), "--json", "-"])
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["summary"]["critical"] == 1
        assert "good" in captured.err


class TestTexCrosscheck:
    @respx.mock
    def test_undefined_keys_are_reported(
        self,
        bib_file: Path,
        tmp_path: Path,
        crossref_fixture: Loader,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _mock(crossref_fixture)
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\cite{good} and \cite{ghost}", encoding="utf-8")
        main([str(bib_file), "--tex", str(tex)])
        assert "ghost" in capsys.readouterr().out


class TestFix:
    @respx.mock
    def test_fix_writes_a_new_file_and_leaves_the_input_alone(
        self, bib_file: Path, tmp_path: Path, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_year_drift"))
        )
        respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        before = bib_file.read_text(encoding="utf-8")
        target = tmp_path / "fixed.bib"
        main([str(bib_file), "--fix", str(target)])
        assert bib_file.read_text(encoding="utf-8") == before

    @respx.mock
    def test_fix_onto_the_input_is_a_usage_error(
        self, bib_file: Path, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_year_drift"))
        )
        respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        before = bib_file.read_text(encoding="utf-8")
        assert main([str(bib_file), "--fix", str(bib_file)]) == EXIT_USAGE
        assert bib_file.read_text(encoding="utf-8") == before

    @respx.mock
    def test_nothing_to_fix_writes_no_file(
        self, bib_file: Path, tmp_path: Path, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        target = tmp_path / "fixed.bib"
        assert main([str(bib_file), "--fix", str(target)]) == EXIT_OK
        assert not target.exists()


class TestOfflineAndCache:
    @respx.mock
    def test_offline_opens_no_connection(
        self, bib_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(bib_file), "--offline"]) == EXIT_OK
        assert respx.calls.call_count == 0
        assert "cache only" in capsys.readouterr().out

    @respx.mock
    def test_offline_reports_unchecked_rather_than_ok(
        self, bib_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An entry we could not check must never be presented as fine."""
        main([str(bib_file), "--offline"])
        out = capsys.readouterr().out
        assert "2 error" in out
        assert "ok" not in out.split("\n")[-3]

    def test_clear_cache_reports_and_exits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--clear-cache"]) == EXIT_OK
        assert "cached responses" in capsys.readouterr().out


class TestMalformedInput:
    @respx.mock
    def test_a_broken_entry_does_not_stop_the_run(
        self, tmp_path: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _mock(crossref_fixture)
        path = tmp_path / "refs.bib"
        path.write_text(BIB + "\n@article{broken, title={No close\n", encoding="utf-8")
        assert main([str(path)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "good" in out
        assert "unparseable" in out

    @respx.mock
    def test_an_empty_file_is_not_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "empty.bib"
        path.write_text("", encoding="utf-8")
        assert main([str(path)]) == EXIT_OK
        assert "nothing to report" in capsys.readouterr().out


class TestPdfInput:
    @pytest.fixture
    def paper(self, request: pytest.FixtureRequest) -> Path:
        return Path(request.config.rootpath) / "tests" / "data" / "pdf" / "paper.pdf"

    @respx.mock
    def test_a_pdf_is_checked_without_any_bib_file(
        self, paper: Path, crossref_fixture: Loader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1016/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_retracted"))
        )
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        assert main([str(paper)]) == EXIT_OK
        captured = capsys.readouterr()
        assert "read 6 references" in captured.err
        assert "ref4" in captured.out
        assert "RETRACTED" in captured.out

    @respx.mock
    def test_a_pdf_is_detected_by_content_not_extension(
        self, paper: Path, tmp_path: Path, crossref_fixture: Loader
    ) -> None:
        """A PDF misnamed .bib must still be read as a PDF."""
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        misnamed = tmp_path / "refs.bib"
        misnamed.write_bytes(paper.read_bytes())
        assert main([str(misnamed)]) == EXIT_OK

    @respx.mock
    def test_fail_on_critical_works_for_a_pdf(
        self, paper: Path, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1016/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_retracted"))
        )
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        assert main([str(paper), "--fail-on", "critical"]) == EXIT_FAILED

    @respx.mock
    def test_export_bib_writes_a_parseable_file(
        self, paper: Path, tmp_path: Path, crossref_fixture: Loader
    ) -> None:
        from bibcheck.parsing import parse_bibtex_file

        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        target = tmp_path / "from-pdf.bib"
        assert main([str(paper), "--export-bib", str(target)]) == EXIT_OK
        parsed = parse_bibtex_file(target)
        assert len(parsed.entries) == 6
        assert parsed.problems == ()

    @respx.mock
    def test_export_bib_refuses_to_clobber(
        self, paper: Path, tmp_path: Path, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        target = tmp_path / "out.bib"
        target.write_text("existing", encoding="utf-8")
        assert main([str(paper), "--export-bib", str(target)]) == EXIT_USAGE
        assert target.read_text(encoding="utf-8") == "existing"

    @respx.mock
    def test_fix_on_a_pdf_points_at_export_bib_instead(
        self, paper: Path, tmp_path: Path, crossref_fixture: Loader,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--fix edits a .bib in place-adjacent terms; a PDF has none."""
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        assert main([str(paper), "--fix", str(tmp_path / "x.bib")]) == EXIT_USAGE
        assert "--export-bib" in capsys.readouterr().err

    def test_a_file_that_is_not_a_pdf_named_pdf_is_a_clear_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "fake.pdf"
        path.write_text("I am not a PDF", encoding="utf-8")
        assert main([str(path)]) == EXIT_USAGE
        assert "could not read the PDF" in capsys.readouterr().err

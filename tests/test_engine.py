"""End-to-end orchestration, driven by recorded fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import respx

from bibcheck.config import Config
from bibcheck.crossref import CrossrefClient
from bibcheck.engine import check_bibliography
from bibcheck.models import Code, Status
from bibcheck.parsing import parse_bibtex

Loader = Callable[[str], dict[str, Any]]
WORKS = "https://api.crossref.org/works"


def _report_for(report: Any, key: str) -> Any:
    return next(entry for entry in report.entries if entry.key == key)


def _codes(entry: Any) -> set[Code]:
    return {problem.code for problem in entry.problems}


@pytest.fixture
def bib() -> str:
    return r"""
@inproceedings{good2016,
  title     = {Deep Residual Learning for Image Recognition},
  author    = {He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian},
  booktitle = {2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)},
  pages     = {770--778},
  year      = {2016},
  doi       = {10.1109/CVPR.2016.90}
}

@article{wrongyear2021,
  title   = {A Paper Whose Year Is Wrong In The Bib},
  author  = {Doe, Jane and Roe, Richard},
  journal = {Journal of Reproducible Findings},
  volume  = {12},
  pages   = {45--67},
  year    = {2021},
  doi     = {10.1000/drift.2019}
}

@article{fabricated2021,
  title   = {Quantum Neural Architectures for Sentiment Analysis},
  author  = {Nobody, A.},
  journal = {Journal of Advanced Computing},
  year    = {2021},
  doi     = {10.9999/jac.2021.99999}
}

@article{retracted1998,
  title   = {Ileal-lymphoid-nodular hyperplasia},
  author  = {Wakefield, A J},
  journal = {The Lancet},
  year    = {1998},
  doi     = {10.1016/S0140-6736(97)11096-0}
}
"""


def _mock_all(crossref_fixture: Loader) -> None:
    respx.get(url__startswith=f"{WORKS}/10.1109/cvpr.2016.90").mock(
        return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
    )
    respx.get(url__startswith=f"{WORKS}/10.1000/drift.2019").mock(
        return_value=httpx.Response(200, json=crossref_fixture("work_year_drift"))
    )
    respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
        return_value=httpx.Response(404, json=crossref_fixture("not_found"))
    )
    respx.get(url__startswith=f"{WORKS}/10.1016/").mock(
        return_value=httpx.Response(200, json=crossref_fixture("work_retracted"))
    )


@respx.mock
async def test_a_clean_entry_is_ok(config: Config, bib: str, crossref_fixture: Loader) -> None:
    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config)
    entry = _report_for(report, "good2016")
    assert entry.status is Status.OK
    assert entry.problems == ()


@respx.mock
async def test_a_wrong_year_is_a_warning_showing_both_values(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config)
    entry = _report_for(report, "wrongyear2021")
    assert entry.status is Status.WARN
    problem = next(p for p in entry.problems if p.code is Code.YEAR_MISMATCH)
    assert problem.local == "2021"
    assert problem.authoritative == "2019"


@respx.mock
async def test_a_fabricated_doi_is_critical(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config)
    entry = _report_for(report, "fabricated2021")
    assert entry.status is Status.CRITICAL
    assert Code.DOI_NOT_FOUND in _codes(entry)


@respx.mock
async def test_a_retracted_paper_is_critical(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config)
    entry = _report_for(report, "retracted1998")
    assert entry.status is Status.CRITICAL
    assert Code.RETRACTED in _codes(entry)


@respx.mock
async def test_the_summary_counts_every_entry(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config)
    assert sum(report.counts.values()) == 4
    assert report.counts[Status.CRITICAL] == 2
    assert report.worst_status is Status.CRITICAL


@respx.mock
async def test_one_network_failure_does_not_stop_the_others(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    """The headline guarantee, end to end."""
    _mock_all(crossref_fixture)
    respx.get(url__startswith=f"{WORKS}/10.1000/drift.2019").mock(
        side_effect=httpx.ConnectError("down")
    )
    report = await check_bibliography(parse_bibtex(bib), config)
    assert _report_for(report, "wrongyear2021").status is Status.NETWORK_ERROR
    assert _report_for(report, "good2016").status is Status.OK
    assert _report_for(report, "fabricated2021").status is Status.CRITICAL


@respx.mock
async def test_an_unexpected_exception_costs_one_entry_not_the_run(
    config: Config, bib: str, crossref_fixture: Loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a bug in our own code must degrade to one bad reference."""
    _mock_all(crossref_fixture)
    real = CrossrefClient.fetch_by_doi

    async def explode(self: CrossrefClient, doi: str) -> Any:
        if doi.startswith("10.1000/"):
            raise RuntimeError("a bug in bibcheck itself")
        return await real(self, doi)

    monkeypatch.setattr(CrossrefClient, "fetch_by_doi", explode)
    report = await check_bibliography(parse_bibtex(bib), config)
    assert _report_for(report, "wrongyear2021").status is Status.NETWORK_ERROR
    assert _report_for(report, "good2016").status is Status.OK
    assert sum(report.counts.values()) == 4


@respx.mock
async def test_a_malformed_entry_is_reported_without_losing_the_file(
    config: Config, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    source = (
        "@article{ok, title={T}, author={A}, year={2020}, doi={10.1109/CVPR.2016.90}}\n"
        "@article{broken, title={Never closes,\n"
    )
    report = await check_bibliography(parse_bibtex(source), config)
    assert len(report.entries) == 1
    assert Code.MALFORMED_ENTRY in {p.code for p in report.parse_problems}


@respx.mock
async def test_duplicates_are_reported_on_every_member(
    config: Config, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    source = (
        "@article{a, title={Deep Residual Learning for Image Recognition},"
        " author={He, Kaiming}, year={2016}, doi={10.1109/CVPR.2016.90}}\n"
        "@article{b, title={Deep residual learning for image recognition},"
        " author={K. He}, year={2016}, doi={10.1109/cvpr.2016.90}}\n"
    )
    report = await check_bibliography(parse_bibtex(source), config)
    assert Code.DUPLICATE_WORK in _codes(_report_for(report, "a"))
    assert Code.DUPLICATE_WORK in _codes(_report_for(report, "b"))


@respx.mock
async def test_the_tex_crosscheck_finds_uncited_and_undefined(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    _mock_all(crossref_fixture)
    report = await check_bibliography(
        parse_bibtex(bib), config, manuscript_text=r"\cite{good2016} and \cite{ghost}"
    )
    assert report.crosscheck is not None
    assert "ghost" in report.crosscheck.undefined
    assert "fabricated2021" in report.crosscheck.uncited
    assert Code.UNCITED_ENTRY in _codes(_report_for(report, "fabricated2021"))
    assert Code.UNCITED_ENTRY not in _codes(_report_for(report, "good2016"))


@respx.mock
async def test_an_uncited_entry_alone_does_not_make_an_entry_fail(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    """Housekeeping is INFO, so it never changes an OK verdict."""
    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config, manuscript_text="")
    assert _report_for(report, "good2016").status is Status.OK


@respx.mock
async def test_offline_reports_unchecked_rather_than_guessing(
    config: Config, bib: str
) -> None:
    offline = Config(
        **{**{f: getattr(config, f) for f in Config.__dataclass_fields__}, "offline": True}
    )
    report = await check_bibliography(parse_bibtex(bib), offline)
    assert all(entry.status is Status.NETWORK_ERROR for entry in report.entries)
    assert respx.calls.call_count == 0


@respx.mock
async def test_the_report_serialises_to_json(
    config: Config, bib: str, crossref_fixture: Loader
) -> None:
    import json

    _mock_all(crossref_fixture)
    report = await check_bibliography(parse_bibtex(bib), config)
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["summary"]["critical"] == 2
    assert len(payload["entries"]) == 4
    assert payload["generated_at"] is not None

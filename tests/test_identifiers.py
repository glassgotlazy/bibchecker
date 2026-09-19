"""DOI and arXiv identifier extraction."""

from __future__ import annotations

import pytest

from bibcheck.identifiers import (
    extract_arxiv_id,
    extract_doi,
    is_plausible_doi,
    normalize_arxiv_id,
    normalize_doi,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1109/CVPR.2016.90", "10.1109/cvpr.2016.90"),
        ("https://doi.org/10.1109/CVPR.2016.90", "10.1109/cvpr.2016.90"),
        ("http://dx.doi.org/10.1109/CVPR.2016.90", "10.1109/cvpr.2016.90"),
        ("doi:10.1109/CVPR.2016.90", "10.1109/cvpr.2016.90"),
        ("{10.1145/3292500.3330701}", "10.1145/3292500.3330701"),
        # Trailing prose punctuation is not part of the DOI.
        ("10.1109/TPAMI.2020.1234.", "10.1109/tpami.2020.1234"),
        # Structurally invalid.
        ("see the publisher website", None),
        ("10.99/x", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_doi(raw: str | None, expected: str | None) -> None:
    assert normalize_doi(raw) == expected


def test_doi_spellings_converge_on_one_key() -> None:
    forms = [
        "10.5555/3295222.3295349",
        "https://doi.org/10.5555/3295222.3295349",
        "DOI: 10.5555/3295222.3295349",
    ]
    assert len({normalize_doi(form) for form in forms}) == 1


def test_is_plausible_doi() -> None:
    assert is_plausible_doi("10.1000/xyz")
    assert not is_plausible_doi("nonsense")


def test_extract_doi_prefers_the_doi_field() -> None:
    fields = {"doi": "10.1000/correct", "url": "https://doi.org/10.1000/other"}
    assert extract_doi(fields) == "10.1000/correct"


def test_extract_doi_falls_back_to_a_url() -> None:
    fields = {"url": "See https://dx.doi.org/10.1038/s41586-021-03819-3 for details"}
    assert extract_doi(fields) == "10.1038/s41586-021-03819-3"


def test_extract_doi_returns_none_when_absent() -> None:
    assert extract_doi({"title": "No identifier here", "year": "2020"}) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2103.14030", "2103.14030"),
        ("arXiv:2103.14030", "2103.14030"),
        # The version suffix is dropped: v1 and v3 are the same preprint.
        ("arXiv:2103.14030v2", "2103.14030"),
        ("https://arxiv.org/abs/1706.03762v5", "1706.03762"),
        ("10.48550/arXiv.1706.03762", "1706.03762"),
        # Pre-2007 identifiers.
        ("cs/9901002", "cs/9901002"),
        ("math.GT/0309136", "math.GT/0309136"),
        ("nonsense", None),
        (None, None),
    ],
)
def test_normalize_arxiv_id(raw: str | None, expected: str | None) -> None:
    assert normalize_arxiv_id(raw) == expected


def test_extract_arxiv_id_from_eprint() -> None:
    assert (
        extract_arxiv_id({"eprint": "2004.05150", "archiveprefix": "arXiv"})
        == "2004.05150"
    )


def test_extract_arxiv_id_from_prose() -> None:
    assert (
        extract_arxiv_id({"journal": "arXiv preprint arXiv:2004.05150"}) == "2004.05150"
    )


def test_a_non_arxiv_archive_prefix_suppresses_the_search() -> None:
    assert extract_arxiv_id({"eprint": "12345678", "archiveprefix": "PubMed"}) is None


def test_a_doi_is_not_misread_as_an_arxiv_id() -> None:
    """Regression: ``10.5555/3295222.3295349`` contains an arXiv-shaped substring.

    A bare ``NNNN.NNNNN`` inside a longer string must not be believed without an
    explicit arXiv marker, or every DOI-bearing entry looks like a preprint.
    """
    assert extract_arxiv_id({"doi": "10.5555/3295222.3295349"}) is None
    assert extract_arxiv_id({"doi": "10.1109/CVPR.2016.90"}) is None
    assert normalize_arxiv_id("10.5555/3295222.3295349") is None

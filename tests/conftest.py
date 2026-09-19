"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def sample_bib() -> Path:
    return DATA / "sample.bib"


@pytest.fixture(scope="session")
def duplicates_bib() -> Path:
    return DATA / "duplicates.bib"

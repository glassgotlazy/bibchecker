"""Shared fixtures.

No test in this suite touches the network. Crossref traffic is served from
recorded JSON fixtures through respx, and every test that constructs a Config
points its cache at a tmp_path so a developer's real cache is never read or
written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from bibcheck.cache import DiskCache
from bibcheck.config import Config

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


@pytest.fixture(scope="session")
def crossref_dir() -> Path:
    return DATA / "crossref"


@pytest.fixture(scope="session")
def crossref_fixture(crossref_dir: Path) -> "Callable[[str], dict[str, Any]]":
    """Load a recorded Crossref response by filename stem."""
    import json

    def load(name: str) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(
            (crossref_dir / f"{name}.json").read_text(encoding="utf-8")
        )
        return payload

    return load


@pytest.fixture
def cache(tmp_path: Path) -> DiskCache:
    return DiskCache(tmp_path / "cache", ttl_days=30)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A config that never reads the developer's real environment or cache."""
    return Config(
        contact_email="tests@example.org",
        concurrency=4,
        cache_dir=tmp_path / "cache",
        cache_ttl_days=30,
        timeout_seconds=5.0,
        max_retries=2,
    )


@pytest.fixture(autouse=True)
def _no_backoff_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests instant without weakening what they assert."""

    async def _immediate(_seconds: float) -> None:
        return None

    monkeypatch.setattr("bibcheck.crossref.asyncio.sleep", _immediate)


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to open a real socket.

    The suite is required to pass with no network access at all. Asserting that
    by hand rots; wiring it into every test does not. respx intercepts at the
    transport layer, so genuinely mocked HTTP never reaches here -- anything
    that does is a fixture someone forgot to record.
    """
    import socket

    def deny(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "this test tried to open a network connection; record a fixture instead"
        )

    for attribute in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, attribute, deny, raising=True)
    monkeypatch.setattr(socket, "create_connection", deny, raising=True)
    monkeypatch.setattr(socket, "getaddrinfo", deny, raising=True)

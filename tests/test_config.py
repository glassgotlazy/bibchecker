"""Configuration resolution: defaults, file, environment, overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bibcheck.config import Config, load_config


def test_defaults_need_no_api_key_and_no_email() -> None:
    config = Config()
    assert config.concurrency == 8
    assert config.cache_ttl_days == 30
    assert config.offline is False
    assert config.uses_polite_pool is False
    assert "bibcheck/" in config.user_agent


def test_an_email_moves_us_into_the_polite_pool() -> None:
    config = Config(contact_email="author@example.org")
    assert config.uses_polite_pool
    assert "mailto:author@example.org" in config.user_agent


@pytest.mark.parametrize(
    "overrides",
    [
        {"concurrency": 0},
        {"cache_ttl_days": -1},
        {"max_retries": -1},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_values_are_rejected_at_construction(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Config(**overrides)


def test_environment_overrides_defaults(tmp_path: Path) -> None:
    config = load_config(
        start=tmp_path,
        environ={
            "BIBCHECK_EMAIL": "env@example.org",
            "BIBCHECK_CONCURRENCY": "3",
            "BIBCHECK_CACHE_TTL_DAYS": "7",
        },
    )
    assert config.contact_email == "env@example.org"
    assert config.concurrency == 3
    assert config.cache_ttl_days == 7


def test_a_malformed_environment_value_is_ignored_not_fatal(tmp_path: Path) -> None:
    """A typo in an env var must not abort a run."""
    config = load_config(start=tmp_path, environ={"BIBCHECK_CONCURRENCY": "lots"})
    assert config.concurrency == 8


def test_config_file_is_read(tmp_path: Path) -> None:
    (tmp_path / "bibcheck.toml").write_text(
        '[bibcheck]\nemail = "file@example.org"\nconcurrency = 2\n', encoding="utf-8"
    )
    config = load_config(start=tmp_path, environ={})
    assert config.contact_email == "file@example.org"
    assert config.concurrency == 2


def test_pyproject_tool_section_is_read(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.bibcheck]\nemail = "proj@example.org"\ncache_ttl_days = 90\n',
        encoding="utf-8",
    )
    config = load_config(start=tmp_path, environ={})
    assert config.contact_email == "proj@example.org"
    assert config.cache_ttl_days == 90


def test_environment_beats_config_file(tmp_path: Path) -> None:
    (tmp_path / "bibcheck.toml").write_text(
        '[bibcheck]\nemail = "file@example.org"\n', encoding="utf-8"
    )
    config = load_config(start=tmp_path, environ={"BIBCHECK_EMAIL": "env@example.org"})
    assert config.contact_email == "env@example.org"


def test_explicit_overrides_beat_everything(tmp_path: Path) -> None:
    config = load_config(
        start=tmp_path,
        environ={"BIBCHECK_CONCURRENCY": "3"},
        concurrency=1,
        offline=True,
    )
    assert config.concurrency == 1
    assert config.offline is True


def test_a_malformed_config_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "bibcheck.toml").write_text("this is not [ valid toml", encoding="utf-8")
    assert load_config(start=tmp_path, environ={}).concurrency == 8


def test_an_unknown_config_key_does_not_break_an_older_bibcheck(tmp_path: Path) -> None:
    (tmp_path / "bibcheck.toml").write_text(
        '[bibcheck]\nconcurrency = 2\nsome_future_option = "x"\n', encoding="utf-8"
    )
    assert load_config(start=tmp_path, environ={}).concurrency == 2


def test_with_overrides_ignores_unset_flags() -> None:
    config = Config(concurrency=4)
    assert config.with_overrides(concurrency=None, offline=True).concurrency == 4
    assert config.with_overrides(concurrency=None, offline=True).offline is True

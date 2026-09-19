"""Runtime configuration.

Resolution order, lowest priority first: built-in defaults, a TOML config file,
environment variables, then explicit CLI flags. Nothing here requires an API
key -- the contact email only moves requests into Crossref's *polite pool*,
which is faster and better-behaved but entirely optional.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Mapping

__all__ = ["Config", "load_config", "DEFAULT_CACHE_TTL_DAYS"]

DEFAULT_CACHE_TTL_DAYS: Final[int] = 30
_DEFAULT_CONCURRENCY: Final[int] = 8
_PROJECT_URL: Final[str] = "https://github.com/glassgotlazy/bibchecker"

#: Searched in order; the first that exists wins.
_CONFIG_FILENAMES: Final[tuple[str, ...]] = ("bibcheck.toml", ".bibcheck.toml")


@dataclass(frozen=True, slots=True)
class Config:
    """Everything the network and cache layers need to know."""

    contact_email: str | None = None
    concurrency: int = _DEFAULT_CONCURRENCY
    cache_dir: Path = Path.home() / ".cache" / "bibcheck"
    cache_ttl_days: int = DEFAULT_CACHE_TTL_DAYS
    timeout_seconds: float = 20.0
    max_retries: int = 4
    offline: bool = False
    user_agent_suffix: str | None = None

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.cache_ttl_days < 0:
            raise ValueError("cache_ttl_days must not be negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def user_agent(self) -> str:
        """Identify the tool, and the human to contact if it misbehaves.

        Crossref routes requests carrying a ``mailto:`` into its polite pool.
        Without an email the tool still works against the anonymous pool, which
        is rate-limited more aggressively -- so this is a courtesy, not a key.
        """
        from . import __version__

        parts = [f"bibcheck/{__version__}", f"(+{_PROJECT_URL}"]
        if self.contact_email:
            parts[-1] += f"; mailto:{self.contact_email}"
        parts[-1] += ")"
        if self.user_agent_suffix:
            parts.append(self.user_agent_suffix)
        return " ".join(parts)

    @property
    def uses_polite_pool(self) -> bool:
        return bool(self.contact_email)

    def with_overrides(self, **overrides: Any) -> Config:
        """Apply CLI flags on top, ignoring any left unset."""
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})


def _coerce(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Pull known keys out of a config mapping, ignoring anything unrecognised.

    An unknown key is skipped rather than raising: a config file written for a
    newer bibcheck should not stop an older one from running.
    """
    values: dict[str, Any] = {}
    if isinstance(raw.get("email"), str):
        values["contact_email"] = raw["email"]
    if isinstance(raw.get("contact_email"), str):
        values["contact_email"] = raw["contact_email"]
    for name, caster in (
        ("concurrency", int),
        ("cache_ttl_days", int),
        ("max_retries", int),
        ("timeout_seconds", float),
    ):
        value = raw.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[name] = caster(value)
    if isinstance(raw.get("cache_dir"), str):
        values["cache_dir"] = Path(raw["cache_dir"]).expanduser()
    if isinstance(raw.get("offline"), bool):
        values["offline"] = raw["offline"]
    return values


def _read_config_file(start: Path) -> dict[str, Any]:
    """Read ``[tool.bibcheck]`` from pyproject.toml, or a standalone config file.

    A malformed file is ignored rather than fatal; the run proceeds on defaults.
    """
    candidates: list[Path] = []
    for directory in (start, *start.parents):
        candidates.extend(directory / name for name in _CONFIG_FILENAMES)
        candidates.append(directory / "pyproject.toml")
    candidates.append(Path.home() / ".config" / "bibcheck" / "config.toml")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        if path.name == "pyproject.toml":
            tool = data.get("tool")
            section = tool.get("bibcheck") if isinstance(tool, dict) else None
        else:
            section = data.get("bibcheck", data)
        if isinstance(section, dict):
            return _coerce(section)
    return {}


def _read_environment(environ: Mapping[str, str]) -> dict[str, Any]:
    """Environment variables override the config file."""
    values: dict[str, Any] = {}
    email = environ.get("BIBCHECK_EMAIL") or environ.get("CROSSREF_MAILTO")
    if email:
        values["contact_email"] = email.strip()
    for variable, name, caster in (
        ("BIBCHECK_CONCURRENCY", "concurrency", int),
        ("BIBCHECK_CACHE_TTL_DAYS", "cache_ttl_days", int),
        ("BIBCHECK_MAX_RETRIES", "max_retries", int),
        ("BIBCHECK_TIMEOUT", "timeout_seconds", float),
    ):
        raw = environ.get(variable)
        if raw is None:
            continue
        try:
            values[name] = caster(raw)
        except ValueError:
            continue  # A typo in an env var should not abort the run.
    cache_dir = environ.get("BIBCHECK_CACHE_DIR")
    if cache_dir:
        values["cache_dir"] = Path(cache_dir).expanduser()
    return values


def load_config(
    *,
    start: Path | None = None,
    environ: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Config:
    """Build a :class:`Config` from file, environment and explicit overrides."""
    values: dict[str, Any] = {}
    values.update(_read_config_file(start or Path.cwd()))
    values.update(_read_environment(os.environ if environ is None else environ))
    values.update({k: v for k, v in overrides.items() if v is not None})
    try:
        return Config(**values)
    except TypeError:
        known = {k: v for k, v in values.items() if k in Config.__dataclass_fields__}
        return Config(**known)

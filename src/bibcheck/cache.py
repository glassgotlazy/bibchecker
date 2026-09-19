"""Disk cache for network responses.

Every response is cached, including *negative* ones. A fabricated DOI that
404s is the single most interesting result bibcheck produces, and not caching
it would mean re-hitting the network for it on every run -- so a 404 is stored
just like a hit. That is what makes ``--offline`` and fast re-runs work.

The cache is a plain tree of JSON files. It is readable, greppable, safe to
delete at any time, and needs no database.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

__all__ = ["CacheEntry", "DiskCache", "cache_key"]

_SCHEMA_VERSION: Final[int] = 1
_ENCODING: Final[str] = "utf-8"


def cache_key(namespace: str, identifier: str) -> str:
    """Stable key for a request.

    The identifier is hashed rather than used as a filename: DOIs contain
    slashes and arbitrary punctuation, and a query is far too long to be a path
    component. The namespace stays readable so the tree can be inspected by eye.
    """
    digest = hashlib.sha256(identifier.encode(_ENCODING)).hexdigest()
    return f"{namespace}/{digest}"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached response."""

    key: str
    identifier: str
    status: int
    payload: Any
    fetched_at: datetime
    stale: bool = False

    @property
    def is_negative(self) -> bool:
        """A recorded "this does not exist", which is a real answer."""
        return self.status == 404

    def age(self, *, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(timezone.utc)) - self.fetched_at


class DiskCache:
    """A TTL'd JSON file cache.

    Every failure mode degrades to a miss. A corrupt file, an unreadable
    directory or a full disk must never take down a run whose whole purpose is
    to survive bad inputs.
    """

    def __init__(self, directory: Path, *, ttl_days: int = 30) -> None:
        self._directory = Path(directory)
        self._ttl = timedelta(days=ttl_days)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @property
    def directory(self) -> Path:
        return self._directory

    def _path_for(self, key: str) -> Path:
        namespace, _, digest = key.partition("/")
        # Shard by the first two hex characters so a large bibliography does not
        # produce one directory with thousands of entries in it.
        return self._directory / namespace / digest[:2] / f"{digest}.json"

    def get(self, key: str, *, allow_stale: bool = False) -> CacheEntry | None:
        """Look up a key. Returns ``None`` on a miss, a corrupt file, or expiry.

        ``allow_stale`` serves an expired entry anyway, flagged as such. Offline
        runs use it: a stale answer the user can see beats no answer at all.
        """
        path = self._path_for(key)
        try:
            raw = path.read_text(encoding=_ENCODING)
        except (OSError, UnicodeDecodeError):
            self.misses += 1
            return None

        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema") != _SCHEMA_VERSION:
                raise ValueError("unrecognised cache record")
            fetched_at = datetime.fromisoformat(str(data["fetched_at"]))
            status = int(data["status"])
            identifier = str(data.get("identifier", ""))
        except (ValueError, KeyError, TypeError):
            # A truncated or hand-edited file is simply not a cache hit.
            self.misses += 1
            return None

        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        expired = datetime.now(timezone.utc) - fetched_at > self._ttl
        if expired and not allow_stale:
            self.misses += 1
            return None

        self.hits += 1
        return CacheEntry(
            key=key,
            identifier=identifier,
            status=status,
            payload=data.get("payload"),
            fetched_at=fetched_at,
            stale=expired,
        )

    def set(self, key: str, identifier: str, status: int, payload: Any) -> None:
        """Store a response. Silently gives up if the cache is not writable."""
        path = self._path_for(key)
        record = {
            "schema": _SCHEMA_VERSION,
            "key": key,
            "identifier": identifier,
            "status": status,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(path, json.dumps(record, ensure_ascii=False))
            self.writes += 1
        except (OSError, TypeError, ValueError):
            # A read-only or full disk degrades to "no caching", not a crash.
            return

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        """Write via a temp file and rename, so an interrupt cannot truncate."""
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding=_ENCODING,
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def clear(self) -> int:
        """Delete every cached record. Returns how many files were removed."""
        removed = 0
        if not self._directory.is_dir():
            return removed
        for path in self._directory.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

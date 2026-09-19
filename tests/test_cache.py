"""The disk cache: TTL, negative caching, corruption, atomicity."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bibcheck.cache import DiskCache, cache_key


def test_key_is_stable_and_namespaced() -> None:
    first = cache_key("crossref-work", "10.1109/cvpr.2016.90")
    assert first == cache_key("crossref-work", "10.1109/cvpr.2016.90")
    assert first.startswith("crossref-work/")
    assert first != cache_key("crossref-query", "10.1109/cvpr.2016.90")


def test_round_trip(cache: DiskCache) -> None:
    key = cache_key("crossref-work", "10.1/x")
    assert cache.get(key) is None
    cache.set(key, "10.1/x", 200, {"title": ["Hello"]})
    entry = cache.get(key)
    assert entry is not None
    assert entry.status == 200
    assert entry.payload == {"title": ["Hello"]}
    assert entry.stale is False


def test_a_404_is_cached_like_any_other_answer(cache: DiskCache) -> None:
    """A fabricated DOI must not be re-fetched on every run."""
    key = cache_key("crossref-work", "10.9999/fake")
    cache.set(key, "10.9999/fake", 404, None)
    entry = cache.get(key)
    assert entry is not None
    assert entry.is_negative


def test_an_expired_entry_is_a_miss(tmp_path: Path) -> None:
    writer = DiskCache(tmp_path, ttl_days=30)
    key = cache_key("crossref-work", "10.1/x")
    writer.set(key, "10.1/x", 200, {"a": 1})
    assert DiskCache(tmp_path, ttl_days=0).get(key) is None


def test_offline_serves_a_stale_entry_and_says_so(tmp_path: Path) -> None:
    """A stale answer the author can see beats no answer at all."""
    writer = DiskCache(tmp_path, ttl_days=30)
    key = cache_key("crossref-work", "10.1/x")
    writer.set(key, "10.1/x", 200, {"a": 1})
    entry = DiskCache(tmp_path, ttl_days=0).get(key, allow_stale=True)
    assert entry is not None
    assert entry.stale is True
    assert entry.payload == {"a": 1}


def test_a_corrupt_file_is_a_miss_not_a_crash(cache: DiskCache) -> None:
    key = cache_key("crossref-work", "10.1/x")
    cache.set(key, "10.1/x", 200, {"a": 1})
    corrupted = next(cache.directory.rglob("*.json"))
    corrupted.write_text("{ truncated", encoding="utf-8")
    assert cache.get(key) is None


def test_a_record_from_a_future_schema_is_a_miss(cache: DiskCache) -> None:
    key = cache_key("crossref-work", "10.1/x")
    cache.set(key, "10.1/x", 200, {"a": 1})
    path = next(cache.directory.rglob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cache.get(key) is None


def test_a_naive_timestamp_is_treated_as_utc(cache: DiskCache) -> None:
    key = cache_key("crossref-work", "10.1/x")
    cache.set(key, "10.1/x", 200, {"a": 1})
    path = next(cache.directory.rglob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetched_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cache.get(key) is not None


def test_an_unwritable_cache_degrades_quietly(tmp_path: Path) -> None:
    """A read-only disk means "no caching", never a failed run."""
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    cache = DiskCache(blocker, ttl_days=30)
    cache.set(cache_key("ns", "x"), "x", 200, {"a": 1})  # must not raise
    assert cache.get(cache_key("ns", "x")) is None


def test_no_temporary_files_survive_a_write(cache: DiskCache) -> None:
    cache.set(cache_key("ns", "x"), "x", 200, {"a": 1})
    assert list(cache.directory.rglob("*.tmp")) == []


def test_entries_are_sharded_rather_than_piled_in_one_directory(cache: DiskCache) -> None:
    for index in range(20):
        cache.set(cache_key("ns", f"10.1/{index}"), f"10.1/{index}", 200, {"i": index})
    shards = {path.parent for path in cache.directory.rglob("*.json")}
    assert len(shards) > 1


def test_clear_removes_everything(cache: DiskCache) -> None:
    for index in range(3):
        cache.set(cache_key("ns", str(index)), str(index), 200, {})
    assert cache.clear() == 3
    assert cache.get(cache_key("ns", "0")) is None


def test_stats_track_hits_and_misses(cache: DiskCache) -> None:
    key = cache_key("ns", "x")
    cache.get(key)
    cache.set(key, "x", 200, {})
    cache.get(key)
    stats = cache.stats()
    assert stats == {"hits": 1, "misses": 1, "writes": 1}


def test_age_is_reported(cache: DiskCache) -> None:
    key = cache_key("ns", "x")
    cache.set(key, "x", 200, {})
    entry = cache.get(key)
    assert entry is not None
    assert entry.age(now=entry.fetched_at + timedelta(days=2)) == timedelta(days=2)

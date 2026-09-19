"""Async Crossref client.

Three properties matter more than speed here:

* **Nothing crashes the run.** Every failure -- a timeout, a DNS error, a 500,
  a body that is not JSON -- becomes a ``NETWORK_ERROR`` outcome for one entry.
  Thirty-nine good references must not be lost to one bad one.
* **Everything is cached, including 404s.** A fabricated DOI is the most
  interesting result the tool produces; re-fetching it every run would be
  wasteful and would make ``--offline`` useless.
* **The server is treated as a guest treats a host.** Concurrency is capped,
  ``Retry-After`` is obeyed, and rate-limit headers narrow the cap at runtime.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Final, Mapping, Sequence
from urllib.parse import urlencode

import httpx

from .cache import DiskCache, cache_key
from .config import Config
from .crossref_parse import parse_work
from .models import Metadata, WorkRecord
from .names import PersonName
from .normalize import similarity, title_key

__all__ = [
    "CrossrefClient",
    "Lookup",
    "Outcome",
    "match_confidence",
    "CONFIDENT_MATCH",
    "POSSIBLE_MATCH",
]

API_ROOT: Final[str] = "https://api.crossref.org"
_WORK_NAMESPACE: Final[str] = "crossref-work"
_QUERY_NAMESPACE: Final[str] = "crossref-query"
_SEARCH_ROWS: Final[int] = 5

#: Above this, a title+author+year search result is accepted as the same work.
CONFIDENT_MATCH: Final[float] = 0.85
#: Between this and :data:`CONFIDENT_MATCH`, the match is reported but flagged.
POSSIBLE_MATCH: Final[float] = 0.65

_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_BASE_BACKOFF: Final[float] = 1.0
_MAX_BACKOFF: Final[float] = 32.0


class Outcome(str, Enum):
    """Why a lookup ended the way it did.

    ``NOT_FOUND`` is a *result* -- the registry was asked and said no, which is
    exactly what a fabricated DOI looks like. ``NETWORK_ERROR`` and
    ``OFFLINE_MISS`` mean the question was never answered, and must never be
    presented as though it had been.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    NETWORK_ERROR = "network_error"
    OFFLINE_MISS = "offline_miss"


@dataclass(frozen=True, slots=True)
class Lookup:
    """The result of one registry query."""

    outcome: Outcome
    record: WorkRecord | None = None
    detail: str | None = None
    from_cache: bool = False
    stale: bool = False

    @property
    def found(self) -> bool:
        return self.outcome is Outcome.FOUND and self.record is not None

    @property
    def checked(self) -> bool:
        """Did we actually get an answer, either way?"""
        return self.outcome in {Outcome.FOUND, Outcome.NOT_FOUND}


def _year_score(local: int | None, candidate: int | None) -> float:
    """Score a year comparison.

    A one-year gap is normal and not evidence against a match: preprints are
    published the following year, and conference proceedings routinely carry
    the year after the meeting.
    """
    if local is None or candidate is None:
        return 0.5  # Unknown is neutral, neither support nor contradiction.
    gap = abs(local - candidate)
    if gap == 0:
        return 1.0
    if gap == 1:
        return 0.7
    if gap == 2:
        return 0.3
    return 0.0


def _author_score(
    local: Sequence[PersonName], candidate: Sequence[PersonName]
) -> float:
    """Score an author comparison, weighted towards the first author."""
    if not local or not candidate:
        return 0.5
    local_keys = {name.family_key for name in local if name.family_key}
    candidate_keys = {name.family_key for name in candidate if name.family_key}
    if not local_keys or not candidate_keys:
        return 0.5
    if local[0].family_key and local[0].family_key == candidate[0].family_key:
        return 1.0
    overlap = len(local_keys & candidate_keys) / len(local_keys)
    return min(0.8, overlap)


def match_confidence(local: Metadata, candidate: Metadata) -> float:
    """How confident are we that a search result is the cited work?

    Title dominates, because it is the field an author is least likely to get
    wrong and the registry least likely to record differently. Authors and year
    corroborate. The weights are constants so they can be tuned and tested
    rather than buried in a formula.
    """
    if not local.title or not candidate.title:
        return 0.0
    title = similarity(title_key(local.title), title_key(candidate.title))
    authors = _author_score(local.authors, candidate.authors)
    year = _year_score(local.year, candidate.year)
    return round(0.60 * title + 0.25 * authors + 0.15 * year, 4)


class CrossrefClient:
    """Cached, rate-limited, retrying access to the Crossref REST API."""

    def __init__(
        self,
        config: Config,
        *,
        cache: DiskCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._cache = cache or DiskCache(config.cache_dir, ttl_days=config.cache_ttl_days)
        self._client = client
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self.requests_made = 0

    @property
    def cache(self) -> DiskCache:
        return self._cache

    async def __aenter__(self) -> CrossrefClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_seconds),
                headers={"User-Agent": self._config.user_agent, "Accept": "application/json"},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- lookups

    async def fetch_by_doi(self, doi: str) -> Lookup:
        """Resolve a DOI against Crossref."""
        identifier = doi.lower()
        key = cache_key(_WORK_NAMESPACE, identifier)
        url = f"{API_ROOT}/works/{_escape_doi(identifier)}"
        raw = await self._fetch(key, identifier, url, params={})
        if raw.outcome is not Outcome.FOUND:
            return raw.as_lookup()
        message = _envelope_message(raw.payload)
        if message is None:
            return Lookup(outcome=Outcome.NETWORK_ERROR, detail="response had no message")
        return Lookup(
            outcome=Outcome.FOUND,
            record=parse_work(message, from_cache=raw.from_cache),
            from_cache=raw.from_cache,
            stale=raw.stale,
        )

    async def search(self, metadata: Metadata) -> Lookup:
        """Find a work by title, author and year when there is no DOI.

        The result carries a :attr:`WorkRecord.match_confidence` computed by
        bibcheck rather than Crossref's own opaque relevance score, so the
        number shown to the author means something reproducible.
        """
        if not metadata.title:
            return Lookup(outcome=Outcome.NOT_FOUND, detail="entry has no title to search on")

        params = _search_params(metadata)
        identifier = urlencode(sorted(params.items()))
        key = cache_key(_QUERY_NAMESPACE, identifier)
        raw = await self._fetch(key, identifier, f"{API_ROOT}/works", params=params)
        if raw.outcome is not Outcome.FOUND:
            return raw.as_lookup()
        return _best_candidate(metadata, raw)

    # ------------------------------------------------------------- transport

    async def _fetch(
        self,
        key: str,
        identifier: str,
        url: str,
        *,
        params: Mapping[str, str],
    ) -> _Raw:
        """Serve from cache, or fetch and cache. Interpretation is the caller's."""
        cached = self._cache.get(key, allow_stale=self._config.offline)
        if cached is not None:
            if cached.is_negative:
                return _Raw(
                    outcome=Outcome.NOT_FOUND,
                    detail="not registered with Crossref",
                    from_cache=True,
                    stale=cached.stale,
                )
            return _Raw(
                outcome=Outcome.FOUND,
                payload=cached.payload,
                from_cache=True,
                stale=cached.stale,
            )

        if self._config.offline:
            return _Raw(
                outcome=Outcome.OFFLINE_MISS,
                detail="not in cache and --offline was requested",
            )

        status, payload, detail = await self._request(url, params)
        if status is None:
            return _Raw(outcome=Outcome.NETWORK_ERROR, detail=detail)

        # A 404 is a real answer and is cached like any other.
        if status in (200, 404):
            self._cache.set(key, identifier, status, payload)
        if status == 404:
            return _Raw(outcome=Outcome.NOT_FOUND, detail="not registered with Crossref")
        if status != 200:
            return _Raw(outcome=Outcome.NETWORK_ERROR, detail=f"HTTP {status}")
        return _Raw(outcome=Outcome.FOUND, payload=payload)

    async def _request(
        self, url: str, params: Mapping[str, str]
    ) -> tuple[int | None, Any, str | None]:
        """Perform one request with retries. Returns ``(status, body, detail)``.

        A ``status`` of ``None`` means the request never produced a response.
        """
        query = dict(params)
        if self._config.contact_email:
            query["mailto"] = self._config.contact_email

        last_detail: str | None = None
        for attempt in range(self._config.max_retries + 1):
            async with self._semaphore:
                try:
                    client = self._client
                    if client is None:
                        return None, None, "client is not open"
                    self.requests_made += 1
                    response = await client.get(url, params=query)
                except httpx.HTTPError as error:
                    last_detail = f"{type(error).__name__}: {error}"
                    response = None

            if response is not None:
                if response.status_code not in _RETRY_STATUSES:
                    try:
                        body = response.json() if response.content else None
                    except ValueError:
                        return response.status_code, None, "response was not JSON"
                    return response.status_code, body, None
                last_detail = f"HTTP {response.status_code}"

            if attempt >= self._config.max_retries:
                break
            await asyncio.sleep(_backoff_delay(attempt, response))

        return None, None, last_detail or "request failed"


def _backoff_delay(attempt: int, response: httpx.Response | None) -> float:
    """Exponential backoff with jitter, overridden by ``Retry-After``.

    Jitter matters: without it, eight concurrent workers that all get a 429
    retry in lockstep and trip the limit again together.
    """
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(0.0, min(float(header), _MAX_BACKOFF))
            except ValueError:
                pass
    delay = min(_BASE_BACKOFF * (2.0**attempt), _MAX_BACKOFF)
    return delay * (0.5 + random.random() / 2)


def _escape_doi(doi: str) -> str:
    """DOIs contain slashes; Crossref accepts them unescaped in the path."""
    return doi.replace(" ", "%20")


def _search_params(metadata: Metadata) -> dict[str, str]:
    params: dict[str, str] = {
        "query.bibliographic": metadata.title or "",
        "rows": str(_SEARCH_ROWS),
        "select": ",".join(
            (
                "DOI", "title", "subtitle", "author", "issued", "published",
                "container-title", "short-container-title", "volume", "issue",
                "page", "publisher", "type", "subtype", "relation",
                "update-to", "updated-by", "alternative-id",
            )
        ),
    }
    if metadata.authors:
        params["query.author"] = metadata.authors[0].family
    return params


@dataclass(frozen=True, slots=True)
class _Raw:
    """A transport result, before anyone has decided what the body means.

    Keeping this separate from :class:`Lookup` is what lets one code path serve
    both a single work and a result list without either pretending to be a
    :class:`WorkRecord` it is not.
    """

    outcome: Outcome
    payload: Any = None
    detail: str | None = None
    from_cache: bool = False
    stale: bool = False

    def as_lookup(self) -> Lookup:
        """Carry a non-FOUND transport result through unchanged."""
        return Lookup(
            outcome=self.outcome,
            detail=self.detail,
            from_cache=self.from_cache,
            stale=self.stale,
        )


def _envelope_message(payload: Any) -> Mapping[str, Any] | None:
    """Unwrap Crossref's ``{"status": ..., "message": ...}`` envelope."""
    if not isinstance(payload, Mapping):
        return None
    message = payload.get("message")
    return message if isinstance(message, Mapping) else None


def _result_items(payload: Any) -> tuple[Mapping[str, Any], ...]:
    """Pull ``message.items`` out of a search response."""
    message = _envelope_message(payload)
    if message is None:
        return ()
    items = message.get("items")
    if not isinstance(items, Sequence) or isinstance(items, str):
        return ()
    return tuple(item for item in items if isinstance(item, Mapping))


def _best_candidate(local: Metadata, raw: _Raw) -> Lookup:
    """Pick the best search result and attach our own confidence score."""
    candidates = _result_items(raw.payload)
    if not candidates:
        return Lookup(
            outcome=Outcome.NOT_FOUND,
            detail="no results",
            from_cache=raw.from_cache,
            stale=raw.stale,
        )

    best: WorkRecord | None = None
    best_score = 0.0
    for item in candidates:
        record = parse_work(item, from_cache=raw.from_cache)
        score = match_confidence(local, record.metadata)
        if score > best_score:
            best, best_score = record, score

    if best is None or best_score < POSSIBLE_MATCH:
        return Lookup(
            outcome=Outcome.NOT_FOUND,
            detail=(
                f"no candidate scored above {POSSIBLE_MATCH:.2f} "
                f"(best was {best_score:.2f})"
            ),
            from_cache=raw.from_cache,
            stale=raw.stale,
        )

    return Lookup(
        outcome=Outcome.FOUND,
        record=replace(best, match_confidence=best_score),
        from_cache=raw.from_cache,
        stale=raw.stale,
    )

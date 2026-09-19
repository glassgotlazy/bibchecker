"""The async Crossref client, driven entirely by recorded fixtures.

Every response here comes from respx. Nothing in this module opens a socket.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
import pytest
import respx

from bibcheck.cache import DiskCache
from bibcheck.config import Config
from bibcheck.crossref import (
    CONFIDENT_MATCH,
    POSSIBLE_MATCH,
    CrossrefClient,
    Outcome,
    match_confidence,
)
from bibcheck.models import EntryKind, Metadata
from bibcheck.names import parse_author_field

Loader = Callable[[str], dict[str, Any]]
WORKS = "https://api.crossref.org/works"


def local(**overrides: Any) -> Metadata:
    """A local bib entry's metadata, with sensible defaults."""
    base: dict[str, Any] = {
        "title": "A Paper With No Identifier At All",
        "authors": parse_author_field("Smith, John"),
        "year": 2018,
    }
    base.update(overrides)
    return Metadata(**base)


class TestFetchByDoi:
    @respx.mock
    async def test_a_resolving_doi_yields_a_record(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/cvpr.2016.90").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            lookup = await client.fetch_by_doi("10.1109/CVPR.2016.90")
        assert lookup.outcome is Outcome.FOUND
        assert lookup.found
        assert lookup.record is not None
        assert lookup.record.metadata.year == 2016

    @respx.mock
    async def test_a_fabricated_doi_is_reported_as_not_found(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        """The headline case: a DOI that does not exist."""
        respx.get(url__startswith=f"{WORKS}/10.9999/jac.2021.99999").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        async with CrossrefClient(config) as client:
            lookup = await client.fetch_by_doi("10.9999/jac.2021.99999")
        assert lookup.outcome is Outcome.NOT_FOUND
        assert lookup.checked  # We asked and got an answer.
        assert not lookup.found

    @respx.mock
    async def test_the_polite_pool_is_used_when_an_email_is_configured(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            await client.fetch_by_doi("10.1109/cvpr.2016.90")
        request = route.calls.last.request
        assert request.url.params["mailto"] == "tests@example.org"
        assert "mailto:tests@example.org" in request.headers["User-Agent"]
        assert "bibcheck/" in request.headers["User-Agent"]

    @respx.mock
    async def test_no_email_still_works_without_an_api_key(
        self, tmp_path_factory: pytest.TempPathFactory, crossref_fixture: Loader
    ) -> None:
        config = Config(cache_dir=tmp_path_factory.mktemp("c"), contact_email=None)
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            assert (await client.fetch_by_doi("10.1109/cvpr.2016.90")).found
        assert "mailto" not in route.calls.last.request.url.params


class TestCaching:
    @respx.mock
    async def test_a_second_lookup_is_served_from_cache(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            first = await client.fetch_by_doi("10.1109/cvpr.2016.90")
            second = await client.fetch_by_doi("10.1109/cvpr.2016.90")
        assert route.call_count == 1
        assert first.from_cache is False
        assert second.from_cache is True
        assert second.found

    @respx.mock
    async def test_a_404_is_cached_so_re_runs_are_free(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        async with CrossrefClient(config) as client:
            await client.fetch_by_doi("10.9999/fake")
            second = await client.fetch_by_doi("10.9999/fake")
        assert route.call_count == 1
        assert second.outcome is Outcome.NOT_FOUND
        assert second.from_cache

    @respx.mock
    async def test_doi_case_does_not_produce_a_second_request(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            await client.fetch_by_doi("10.1109/CVPR.2016.90")
            await client.fetch_by_doi("10.1109/cvpr.2016.90")
        assert route.call_count == 1

    @respx.mock
    async def test_a_500_is_never_cached(self, config: Config) -> None:
        """A transient failure must not poison the cache for 30 days."""
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(500)
        )
        async with CrossrefClient(config) as client:
            assert (await client.fetch_by_doi("10.1109/x")).outcome is Outcome.NETWORK_ERROR
        assert list(DiskCache(config.cache_dir).directory.rglob("*.json")) == []


class TestOffline:
    @respx.mock
    async def test_offline_serves_the_cache_and_opens_no_socket(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            await client.fetch_by_doi("10.1109/cvpr.2016.90")

        offline = Config(**{**_as_dict(config), "offline": True})
        async with CrossrefClient(offline) as client:
            lookup = await client.fetch_by_doi("10.1109/cvpr.2016.90")
        assert route.call_count == 1
        assert lookup.found
        assert lookup.from_cache

    @respx.mock
    async def test_offline_miss_is_distinct_from_not_found(self, config: Config) -> None:
        """"We did not check" must never be presented as "it does not exist"."""
        offline = Config(**{**_as_dict(config), "offline": True})
        async with CrossrefClient(offline) as client:
            lookup = await client.fetch_by_doi("10.1109/never-fetched")
        assert lookup.outcome is Outcome.OFFLINE_MISS
        assert not lookup.checked
        assert respx.calls.call_count == 0

    @respx.mock
    async def test_offline_serves_a_stale_entry_and_flags_it(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        async with CrossrefClient(config) as client:
            await client.fetch_by_doi("10.1109/cvpr.2016.90")

        expired = Config(**{**_as_dict(config), "offline": True, "cache_ttl_days": 0})
        async with CrossrefClient(expired) as client:
            lookup = await client.fetch_by_doi("10.1109/cvpr.2016.90")
        assert lookup.found
        assert lookup.stale


class TestRetryAndFailure:
    @respx.mock
    async def test_a_429_is_retried_and_then_succeeds(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(200, json=crossref_fixture("work_resnet")),
            ]
        )
        async with CrossrefClient(config) as client:
            lookup = await client.fetch_by_doi("10.1109/cvpr.2016.90")
        assert route.call_count == 2
        assert lookup.found

    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_server_errors_are_retried(self, config: Config, status: int) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(status)
        )
        async with CrossrefClient(config) as client:
            lookup = await client.fetch_by_doi("10.1109/x")
        assert route.call_count == config.max_retries + 1
        assert lookup.outcome is Outcome.NETWORK_ERROR

    @respx.mock
    async def test_a_404_is_not_retried(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        async with CrossrefClient(config) as client:
            await client.fetch_by_doi("10.9999/fake")
        assert route.call_count == 1

    @respx.mock
    async def test_a_connection_error_degrades_to_network_error(
        self, config: Config
    ) -> None:
        """One unreachable reference must not take down the run."""
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        async with CrossrefClient(config) as client:
            lookup = await client.fetch_by_doi("10.1109/x")
        assert lookup.outcome is Outcome.NETWORK_ERROR
        assert lookup.detail is not None and "ConnectError" in lookup.detail

    @respx.mock
    async def test_a_timeout_degrades_to_network_error(self, config: Config) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            side_effect=httpx.ReadTimeout("too slow")
        )
        async with CrossrefClient(config) as client:
            assert (await client.fetch_by_doi("10.1109/x")).outcome is Outcome.NETWORK_ERROR

    @respx.mock
    async def test_a_non_json_body_does_not_raise(self, config: Config) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, text="<html>gateway error</html>")
        )
        async with CrossrefClient(config) as client:
            assert (await client.fetch_by_doi("10.1109/x")).outcome is Outcome.NETWORK_ERROR

    @respx.mock
    async def test_a_json_body_of_the_wrong_shape_does_not_raise(
        self, config: Config
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json={"unexpected": True})
        )
        async with CrossrefClient(config) as client:
            assert (await client.fetch_by_doi("10.1109/x")).outcome is Outcome.NETWORK_ERROR

    @respx.mock
    async def test_one_failure_does_not_stop_the_others(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        """The whole point: the run degrades per entry, never in total."""
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        respx.get(url__startswith=f"{WORKS}/10.6666/").mock(
            side_effect=httpx.ConnectError("down")
        )
        async with CrossrefClient(config) as client:
            results = await asyncio.gather(
                client.fetch_by_doi("10.1109/cvpr.2016.90"),
                client.fetch_by_doi("10.6666/broken"),
                client.fetch_by_doi("10.1109/cvpr.2016.90"),
            )
        assert [result.outcome for result in results] == [
            Outcome.FOUND,
            Outcome.NETWORK_ERROR,
            Outcome.FOUND,
        ]


class TestConcurrency:
    @respx.mock
    async def test_concurrency_never_exceeds_the_configured_cap(
        self, tmp_path_factory: pytest.TempPathFactory, crossref_fixture: Loader
    ) -> None:
        config = Config(cache_dir=tmp_path_factory.mktemp("c"), concurrency=3)
        in_flight = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return httpx.Response(200, json=crossref_fixture("work_resnet"))

        respx.get(url__startswith=WORKS).mock(side_effect=handler)
        async with CrossrefClient(config) as client:
            await asyncio.gather(
                *(client.fetch_by_doi(f"10.1109/x{index}") for index in range(12))
            )
        assert peak <= 3


class TestSearch:
    @respx.mock
    async def test_a_title_search_finds_the_right_candidate(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_noident"))
        )
        async with CrossrefClient(config) as client:
            lookup = await client.search(local())
        assert lookup.found
        assert lookup.record is not None
        assert lookup.record.metadata.doi == "10.1000/searchhit.2018"
        assert lookup.record.match_confidence >= CONFIDENT_MATCH

    @respx.mock
    async def test_an_unrelated_result_set_is_rejected(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        """A bad match reported confidently is worse than no match at all."""
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_noident"))
        )
        async with CrossrefClient(config) as client:
            lookup = await client.search(
                local(title="Entirely Different Work On Quantum Gravity", year=1995)
            )
        assert lookup.outcome is Outcome.NOT_FOUND
        assert lookup.detail is not None and "scored above" in lookup.detail

    @respx.mock
    async def test_an_empty_result_set_is_not_found(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_empty"))
        )
        async with CrossrefClient(config) as client:
            assert (await client.search(local())).outcome is Outcome.NOT_FOUND

    async def test_an_entry_with_no_title_is_not_searched(self, config: Config) -> None:
        async with CrossrefClient(config) as client:
            lookup = await client.search(Metadata(title=None))
        assert lookup.outcome is Outcome.NOT_FOUND
        assert lookup.detail is not None and "no title" in lookup.detail

    @respx.mock
    async def test_the_query_carries_title_and_first_author(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_noident"))
        )
        async with CrossrefClient(config) as client:
            await client.search(local())
        params = route.calls.last.request.url.params
        assert params["query.bibliographic"] == "A Paper With No Identifier At All"
        assert params["query.author"] == "Smith"

    @respx.mock
    async def test_searches_are_cached_too(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_noident"))
        )
        async with CrossrefClient(config) as client:
            await client.search(local())
            second = await client.search(local())
        assert route.call_count == 1
        assert second.from_cache


class TestMatchConfidence:
    def test_an_exact_match_scores_at_the_top(self) -> None:
        assert match_confidence(local(), local()) == 1.0

    def test_a_different_title_scores_below_the_possible_threshold(self) -> None:
        score = match_confidence(local(), local(title="Marine Biology Of The Baltic"))
        assert score < POSSIBLE_MATCH

    def test_a_one_year_gap_is_barely_penalised(self) -> None:
        """Preprint-to-publication lag is normal, not evidence against a match."""
        assert match_confidence(local(), local(year=2019)) >= CONFIDENT_MATCH

    def test_a_wrong_author_lowers_confidence_without_destroying_it(self) -> None:
        score = match_confidence(local(), local(authors=parse_author_field("Jones, Bob")))
        assert POSSIBLE_MATCH <= score < CONFIDENT_MATCH

    def test_a_missing_title_on_either_side_scores_zero(self) -> None:
        assert match_confidence(local(title=None), local()) == 0.0
        assert match_confidence(local(), local(title=None)) == 0.0

    def test_an_unknown_year_is_neutral_rather_than_penalised(self) -> None:
        assert match_confidence(local(), local(year=None)) >= CONFIDENT_MATCH


def _as_dict(config: Config) -> dict[str, Any]:
    return {
        field: getattr(config, field) for field in Config.__dataclass_fields__
    }

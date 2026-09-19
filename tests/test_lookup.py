"""``bibcheck cite`` — the writing-time lookup."""

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest
import respx

from bibcheck.config import Config
from bibcheck.crossref import Outcome
from bibcheck.lookup import cite, resolve_query
from bibcheck.parsing import parse_bibtex

Loader = Callable[[str], dict[str, Any]]
WORKS = "https://api.crossref.org/works"


class TestCite:
    @respx.mock
    async def test_a_doi_yields_pasteable_bibtex(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        result = await cite("10.1109/CVPR.2016.90", config)
        assert result.found
        parsed = parse_bibtex(result.bibtex)
        assert len(parsed.entries) == 1
        assert parsed.problems == ()

    @respx.mock
    async def test_the_key_follows_the_usual_convention(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        assert (await cite("10.1109/CVPR.2016.90", config)).key == "he2016residual"

    @respx.mock
    async def test_an_explicit_key_is_honoured(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        result = await cite("10.1109/CVPR.2016.90", config, key="mine2016")
        assert result.key == "mine2016"
        assert "@inproceedings{mine2016," in result.bibtex

    @respx.mock
    async def test_a_doi_url_is_accepted(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
        )
        assert (await cite("https://doi.org/10.1109/CVPR.2016.90", config)).found

    @respx.mock
    async def test_a_title_search_is_used_for_free_text(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_noident"))
        )
        result = await cite("A Paper With No Identifier At All", config)
        assert result.found
        assert "query.bibliographic" in str(route.calls.last.request.url)

    @respx.mock
    async def test_a_fabricated_doi_reports_not_found(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        result = await cite("10.9999/jac.2021.99999", config)
        assert not result.found
        assert result.outcome is Outcome.NOT_FOUND
        assert result.bibtex == ""

    async def test_an_empty_query_is_rejected(self, config: Config) -> None:
        assert not (await cite("   ", config)).found


class TestResolveQuery:
    @respx.mock
    async def test_an_arxiv_id_is_tried_through_its_registered_doi(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        route = respx.get(url__startswith=f"{WORKS}/10.48550/").mock(
            return_value=httpx.Response(200, json=crossref_fixture("work_preprint"))
        )
        from bibcheck.crossref import CrossrefClient

        async with CrossrefClient(config) as client:
            lookup = await resolve_query("arXiv:2004.05150", client)
        assert lookup.found
        assert route.call_count == 1

    @respx.mock
    async def test_an_arxiv_id_missing_from_crossref_says_what_to_do(
        self, config: Config, crossref_fixture: Loader
    ) -> None:
        """arXiv deposits with DataCite, so the DOI route often misses."""
        respx.get(url__startswith=f"{WORKS}/10.48550/").mock(
            return_value=httpx.Response(404, json=crossref_fixture("not_found"))
        )
        from bibcheck.crossref import CrossrefClient

        async with CrossrefClient(config) as client:
            lookup = await resolve_query("arXiv:9999.99999", client)
        assert not lookup.found
        assert lookup.detail is not None and "search by title" in lookup.detail

    @respx.mock
    async def test_a_network_failure_is_not_reported_as_not_found(
        self, config: Config
    ) -> None:
        respx.get(url__startswith=WORKS).mock(side_effect=httpx.ConnectError("down"))
        from bibcheck.crossref import CrossrefClient

        async with CrossrefClient(config) as client:
            lookup = await resolve_query("10.1109/x", client)
        assert lookup.outcome is Outcome.NETWORK_ERROR

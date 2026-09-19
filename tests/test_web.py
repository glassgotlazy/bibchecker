"""The ASGI app, exercised in-process. No server and no sockets involved."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import respx

from bibcheck.config import Config
from bibcheck.web import MAX_BODY_BYTES, MAX_ENTRIES, create_app

Loader = Callable[[str], dict[str, Any]]
WORKS = "https://api.crossref.org/works"

GOOD_BIB = (
    "@inproceedings{he2016resnet,\n"
    "  title={Deep Residual Learning for Image Recognition},\n"
    "  author={He, Kaiming and Zhang, Xiangyu},\n"
    "  booktitle={CVPR}, pages={770--778}, year={2016},\n"
    "  doi={10.1109/CVPR.2016.90}\n}\n"
)
FAKE_BIB = (
    "@article{fabricated2021,\n"
    "  title={Quantum Neural Architectures},\n"
    "  author={Doe, Jane}, journal={J. Adv. Comp.}, year={2021},\n"
    "  doi={10.9999/jac.2021.99999}\n}\n"
)


@pytest.fixture
def client(config: Config, tmp_path: Path) -> httpx.AsyncClient:
    static = tmp_path / "public"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>bibcheck</title>", encoding="utf-8")
    app = create_app(static_root=static, config=config)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _mock(crossref_fixture: Loader) -> None:
    respx.get(url__startswith=f"{WORKS}/10.1109/").mock(
        return_value=httpx.Response(200, json=crossref_fixture("work_resnet"))
    )
    respx.get(url__startswith=f"{WORKS}/10.9999/").mock(
        return_value=httpx.Response(404, json=crossref_fixture("not_found"))
    )


class TestRoutes:
    async def test_index_is_served(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "bibcheck" in response.text

    async def test_health(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_an_unknown_path_is_404(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.get("/nope")
        assert response.status_code == 404

    async def test_a_wrong_method_is_405(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.delete("/api/check")
        assert response.status_code == 405

    async def test_responses_carry_nosniff(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.get("/api/health")
        assert response.headers["x-content-type-options"] == "nosniff"


class TestCheckEndpoint:
    @respx.mock
    async def test_a_json_body_is_checked(
        self, client: httpx.AsyncClient, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        async with client:
            response = await client.post("/api/check", json={"bib": GOOD_BIB})
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["ok"] == 1
        assert payload["entries"][0]["key"] == "he2016resnet"

    @respx.mock
    async def test_a_raw_bib_body_is_also_accepted(
        self, client: httpx.AsyncClient, crossref_fixture: Loader
    ) -> None:
        """So the endpoint is usable with a plain curl --data-binary @refs.bib."""
        _mock(crossref_fixture)
        async with client:
            response = await client.post(
                "/api/check", content=GOOD_BIB.encode(), headers={"content-type": "text/plain"}
            )
        assert response.status_code == 200
        assert response.json()["summary"]["ok"] == 1

    @respx.mock
    async def test_a_fabricated_doi_comes_back_critical(
        self, client: httpx.AsyncClient, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        async with client:
            response = await client.post("/api/check", json={"bib": FAKE_BIB})
        payload = response.json()
        assert payload["summary"]["critical"] == 1
        assert payload["entries"][0]["problems"][0]["code"] == "doi_not_found"

    @respx.mock
    async def test_a_tex_manuscript_drives_the_crosscheck(
        self, client: httpx.AsyncClient, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        async with client:
            response = await client.post(
                "/api/check", json={"bib": GOOD_BIB, "tex": r"\cite{ghost}"}
            )
        crosscheck = response.json()["crosscheck"]
        assert crosscheck["undefined"] == ["ghost"]
        assert crosscheck["uncited"] == ["he2016resnet"]

    @respx.mock
    async def test_a_malformed_entry_is_reported_not_fatal(
        self, client: httpx.AsyncClient, crossref_fixture: Loader
    ) -> None:
        _mock(crossref_fixture)
        async with client:
            response = await client.post(
                "/api/check", json={"bib": GOOD_BIB + "@article{broken, title={No close\n"}
            )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["entries"]) == 1
        assert payload["parse_problems"]


class TestInputHandling:
    async def test_an_empty_body_is_rejected(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.post("/api/check", content=b"")
        assert response.status_code == 400

    async def test_invalid_json_is_rejected(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.post(
                "/api/check", content=b"{not json", headers={"content-type": "application/json"}
            )
        assert response.status_code == 400
        assert "JSON" in response.json()["error"]

    async def test_a_missing_bib_field_is_rejected(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.post("/api/check", json={"tex": "x"})
        assert response.status_code == 400

    async def test_a_file_with_no_entries_is_rejected(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.post("/api/check", json={"bib": "just some prose"})
        assert response.status_code == 400

    async def test_an_oversized_body_is_rejected_before_any_work(
        self, client: httpx.AsyncClient
    ) -> None:
        payload = b"@article{x, title={" + b"a" * (MAX_BODY_BYTES + 1) + b"}}"
        async with client:
            response = await client.post(
                "/api/check", content=payload, headers={"content-type": "text/plain"}
            )
        assert response.status_code == 413

    @respx.mock
    async def test_too_many_entries_are_truncated_with_a_notice(
        self, client: httpx.AsyncClient, crossref_fixture: Loader
    ) -> None:
        """A bounded answer beats a platform timeout."""
        _mock(crossref_fixture)
        bib = "".join(
            f"@article{{k{i}, title={{T{i}}}, author={{A}}, year={{2020}}}}\n"
            for i in range(MAX_ENTRIES + 5)
        )
        respx.get(url__startswith=WORKS).mock(
            return_value=httpx.Response(200, json=crossref_fixture("search_empty"))
        )
        async with client:
            response = await client.post("/api/check", json={"bib": bib})
        payload = response.json()
        assert payload["truncated"] is True
        assert len(payload["entries"]) == MAX_ENTRIES
        assert "notice" in payload

    async def test_non_utf8_input_does_not_crash(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.post(
                "/api/check",
                content="@article{a, title={Caf\xe9}, year={2020}}".encode("latin-1"),
                headers={"content-type": "text/plain"},
            )
        assert response.status_code in (200, 400)


class TestFailureHandling:
    async def test_a_handler_exception_becomes_a_500_not_a_hang(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A web handler must never fail silently or leave the socket open."""

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr("bibcheck.web.parse_bibtex", explode)
        async with client:
            response = await client.post("/api/check", json={"bib": GOOD_BIB})
        assert response.status_code == 500
        assert "boom" in response.json()["detail"]

    async def test_a_missing_index_is_reported_rather_than_crashing(
        self, config: Config
    ) -> None:
        app = create_app(static_root=None, config=config)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/")
        assert response.status_code == 500

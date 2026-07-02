"""HTTP-client unit tests (httpx mock via respx)."""
from __future__ import annotations

import httpx
import pytest
import respx

from hptsu_mcp.client import HptSuApiError, HptSuClient
from hptsu_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(api_key="test-key", base_url="https://hpt.su/api/v1")


@pytest.mark.asyncio
async def test_default_api_key_sent_as_x_api_key(settings: Settings) -> None:
    """Stdio-режим: ключ из env идёт в X-API-Key (формат хочет td_billing)."""
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/").mock(
            return_value=httpx.Response(200, json={"count": 0, "results": []}),
        )
        await client.list_documents()
        assert route.called
        req = route.calls.last.request
        assert req.headers["X-API-Key"] == "test-key"
        assert "Authorization" not in req.headers


@pytest.mark.asyncio
async def test_per_request_token_overrides_default(settings: Settings) -> None:
    """Hosted: token= per-call перебивает default из settings."""
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/").mock(
            return_value=httpx.Response(200, json={}),
        )
        await client.list_documents(token="per-call-token")
        assert route.calls.last.request.headers["X-API-Key"] == "per-call-token"


@pytest.mark.asyncio
async def test_list_documents_passes_filters(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/").mock(
            return_value=httpx.Response(200, json={"count": 1, "results": [{"id": "x"}]}),
        )
        result = await client.list_documents(number="RU C-RU", page=2, page_size=50)
        assert result == {"count": 1, "results": [{"id": "x"}]}
        params = dict(route.calls.last.request.url.params)
        assert params == {"number": "RU C-RU", "page": "2", "page_size": "50"}


@pytest.mark.asyncio
async def test_get_document_slug_kind(settings: Settings) -> None:
    """HIGH#223: client.get_document(slug, kind) — UUID-only флоу удалён в 0.2.0."""
    slug = "RU-C-RU-MTS-00001"
    kind = "cert"
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        mock.get(f"/docs/{kind}/{slug}/").mock(
            return_value=httpx.Response(200, json={"slug": slug, "kind": kind}),
        )
        data = await client.get_document(slug, kind=kind)
        assert data["slug"] == slug


@pytest.mark.asyncio
async def test_error_response_raised(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/").mock(
            return_value=httpx.Response(401, json={"detail": "Invalid token"}),
        )
        with pytest.raises(HptSuApiError) as exc:
            await client.list_documents()
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_no_filters_envelope_parsed(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/otts/").mock(
            return_value=httpx.Response(400, json={
                "code": "NO_FILTERS",
                "message": "Задайте хотя бы один фильтр.",
                "available_filters": ["brand", "number", "vin"],
            }),
        )
        with pytest.raises(HptSuApiError) as exc:
            await client.list_by_kind("otts")
        assert exc.value.code == "NO_FILTERS"
        assert exc.value.available_filters == ["brand", "number", "vin"]


@pytest.mark.asyncio
async def test_list_by_kind_strips_empty(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/otts/").mock(
            return_value=httpx.Response(200, json={"results": []}),
        )
        await client.list_by_kind("otts", number="X", applicant="", brand=None, year=2024)
        params = dict(route.calls.last.request.url.params)
        # empty/None filters should be dropped.
        assert "applicant" not in params
        assert "brand" not in params
        assert params["year"] == "2024"


@pytest.mark.asyncio
async def test_no_auth_when_key_missing() -> None:
    settings = Settings(api_key=None, base_url="https://hpt.su/api/v1")
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/").mock(return_value=httpx.Response(200, json={}))
        await client.list_documents()
        headers = route.calls.last.request.headers
        assert "Authorization" not in headers
        assert "X-API-Key" not in headers


@pytest.mark.asyncio
async def test_x_mcp_client_header(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        client.set_mcp_client("claude-desktop", "0.10.5")
        route = mock.get("/docs/").mock(return_value=httpx.Response(200, json={}))
        await client.list_documents()
        assert route.calls.last.request.headers["X-MCP-Client"] == "claude-desktop/0.10.5"


@pytest.mark.asyncio
async def test_x_mcp_client_no_version(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        client.set_mcp_client("cursor", None)
        route = mock.get("/docs/").mock(return_value=httpx.Response(200, json={}))
        await client.list_documents()
        assert route.calls.last.request.headers["X-MCP-Client"] == "cursor"


@pytest.mark.asyncio
async def test_per_request_token_used_in_post(settings: Settings) -> None:
    """Token override работает и для POST-методов (download)."""
    uid = "550e8400-e29b-41d4-a716-446655440000"
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.post(f"/files/{uid}/download/").mock(
            return_value=httpx.Response(200, json={"download_url": "..."}),
        )
        await client.download_document_file(uid, token="hosted-token")
        assert route.calls.last.request.headers["X-API-Key"] == "hosted-token"


@pytest.mark.asyncio
async def test_search_by_vin(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/by_vin/XTA21703080123456/").mock(
            return_value=httpx.Response(200, json={"results": []}),
        )
        await client.search_by_vin("XTA21703080123456")
        assert route.called


@pytest.mark.asyncio
async def test_fulltext_search_sends_q(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/docs/fulltext/").mock(
            return_value=httpx.Response(200, json={"results": []}),
        )
        await client.fulltext_search("асбестосодержащие тормозные колодки")
        assert dict(route.calls.last.request.url.params).get("q") == "асбестосодержащие тормозные колодки"


@pytest.mark.asyncio
async def test_download_post(settings: Settings) -> None:
    uid = "550e8400-e29b-41d4-a716-446655440000"
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.post(f"/files/{uid}/download/").mock(
            return_value=httpx.Response(200, json={"download_url": "https://hpt.su/d/..."}),
        )
        result = await client.download_document_file(uid)
        assert route.called
        assert "download_url" in result


@pytest.mark.asyncio
async def test_list_document_files(settings: Settings) -> None:
    doc_slug = "otts-12345"
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        route = mock.get(f"/docs/otts/{doc_slug}/files/").mock(
            return_value=httpx.Response(200, json=[
                {"file_uid": "f1", "file_name": "a.pdf", "kind": "otts"},
            ]),
        )
        result = await client.list_document_files(doc_slug, "otts")
        assert route.called
        assert isinstance(result, list) and result[0]["file_uid"] == "f1"


@pytest.mark.asyncio
async def test_402_handled(settings: Settings) -> None:
    async with HptSuClient(settings) as client, respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/fulltext/").mock(
            return_value=httpx.Response(402, json={"detail": "Upgrade required"}),
        )
        with pytest.raises(HptSuApiError) as exc:
            await client.fulltext_search("test")
        assert exc.value.status_code == 402

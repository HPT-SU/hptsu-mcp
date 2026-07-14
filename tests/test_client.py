"""HTTP-client unit tests (httpx mock via respx)."""
from __future__ import annotations

import httpx
import pytest
import respx

from hptsu_mcp.client import HptSuApiError, HptSuClient, RefResolution, _safe
from hptsu_mcp.config import Settings


@pytest.mark.asyncio
async def test_resolve_ref_single_match_returns_id(settings: Settings) -> None:
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/nsi/brands/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 417, "title": "КАМАЗ"}]}))
        async with HptSuClient(settings) as client:
            res = await client.resolve_ref("/nsi/brands/", "KAMAZ")
    assert isinstance(res, RefResolution)
    assert res.found and res.id == 417
    assert not res.ambiguous


@pytest.mark.asyncio
async def test_resolve_ref_no_match(settings: Settings) -> None:
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/nsi/brands/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(settings) as client:
            res = await client.resolve_ref("/nsi/brands/", "Нетакого")
    assert not res.found and not res.ambiguous
    assert res.candidates == []


@pytest.mark.asyncio
async def test_resolve_ref_many_truncates_candidates(settings: Settings) -> None:
    rows = [{"id": i, "name": f"НАМИ-{i}"} for i in range(1, 13)]  # 12 из 18
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/nsi/certification-bodies/").mock(return_value=httpx.Response(
            200, json={"count": 18, "results": rows}))
        async with HptSuClient(settings) as client:
            res = await client.resolve_ref("/nsi/certification-bodies/", "НАМИ")
    assert res.ambiguous and res.id is None
    assert len(res.candidates) == 12
    assert res.truncated
    assert res.candidates[0] == {"id": 1, "label": "НАМИ-1"}


@pytest.mark.asyncio
async def test_resolve_ref_cached_second_call_no_http(settings: Settings) -> None:
    with respx.mock(base_url=settings.base_url) as mock:
        route = mock.get("/nsi/brands/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 417, "title": "КАМАЗ"}]}))
        async with HptSuClient(settings) as client:
            r1 = await client.resolve_ref("/nsi/brands/", "KAMAZ")
            r2 = await client.resolve_ref("/nsi/brands/", "kamaz")  # casefold → кэш
    assert r1.id == 417 and r2.id == 417
    assert route.call_count == 1


@pytest.fixture
def settings() -> Settings:
    return Settings(api_key="test-key", base_url="https://hpt.su/api/v1")


@pytest.mark.parametrize("slug", [
    "тс-ru-e-ru.нв23.00256",        # ОТТС: кириллица + точки
    "еаэс-ru-c-se.мт49.в.0050720",  # сертификат
    "RU-C-RU-MTS-00001",            # ASCII (регресс)
    "otts-12345",
])
def test_safe_allows_real_cyrillic_dotted_slugs(slug: str) -> None:
    """Реальные slug'и документов кириллические и содержат точки — `_safe`
    их пропускает (иначе get_document/list_document_files не работают)."""
    assert _safe(slug, "slug") == slug


@pytest.mark.parametrize("bad", ["../admin", "a/b", "..", "foo/../bar", "", "a b"])
def test_safe_blocks_traversal_and_separators(bad: str) -> None:
    """`/`, пробел и `..` отвергаются — path-traversal (HIGH#216) невозможен."""
    with pytest.raises(ValueError):
        _safe(bad, "slug")


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


# ──── Проверка минимальной версии клиента (X-HPTSU-MCP-Min-Version) ─────────

def _resp(min_version: str | None = None) -> httpx.Response:
    headers = {"X-HPTSU-MCP-Min-Version": min_version} if min_version else {}
    return httpx.Response(200, json={"count": 0, "results": []}, headers=headers)


@pytest.mark.asyncio
async def test_min_version_outdated_sets_notice_once(settings, monkeypatch) -> None:
    from hptsu_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_UPDATE_NOTICE", None)
    monkeypatch.delenv("HPTSU_TRANSPORT", raising=False)  # default stdio
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/").mock(return_value=_resp("99.0.0"))
        async with HptSuClient(settings) as client:
            await client.list_documents()
            await client.list_documents()
    notice = client_mod.consume_update_notice()
    assert notice and "99.0.0" in notice and "pip install -U hptsu-mcp" in notice
    assert client_mod.consume_update_notice() is None  # одноразовое


@pytest.mark.asyncio
async def test_min_version_current_ok_no_notice(settings, monkeypatch) -> None:
    from hptsu_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_UPDATE_NOTICE", None)
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/").mock(return_value=_resp("0.0.1"))
        async with HptSuClient(settings) as client:
            await client.list_documents()
    assert client_mod.consume_update_notice() is None


@pytest.mark.asyncio
async def test_min_version_no_header_keeps_checking(settings, monkeypatch) -> None:
    """Старый бэкенд без заголовка → проверка не финализируется, следующий
    ответ с заголовком всё же срабатывает."""
    from hptsu_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_UPDATE_NOTICE", None)
    monkeypatch.delenv("HPTSU_TRANSPORT", raising=False)
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/").mock(side_effect=[_resp(None), _resp("99.0.0")])
        async with HptSuClient(settings) as client:
            await client.list_documents()
            assert client_mod.consume_update_notice() is None
            await client.list_documents()
    assert client_mod.consume_update_notice() is not None


@pytest.mark.asyncio
async def test_min_version_hosted_transport_logs_only(settings, monkeypatch) -> None:
    """В hosted-режиме предупреждение не попадает в tool-ответы (только лог):
    пользователь hosted-инстанс не обновляет."""
    from hptsu_mcp import client as client_mod
    monkeypatch.setattr(client_mod, "_UPDATE_NOTICE", None)
    monkeypatch.setenv("HPTSU_TRANSPORT", "http")
    with respx.mock(base_url=settings.base_url) as mock:
        mock.get("/docs/").mock(return_value=_resp("99.0.0"))
        async with HptSuClient(settings) as client:
            await client.list_documents()
    assert client_mod.consume_update_notice() is None


def test_format_prepends_notice_once(monkeypatch) -> None:
    from hptsu_mcp import client as client_mod
    from hptsu_mcp.server import _format
    monkeypatch.setattr(client_mod, "_UPDATE_NOTICE", "⚠️ update me")
    first = _format({"a": 1})
    assert first.startswith("⚠️ update me\n\n")
    assert _format({"a": 1}).startswith("{")  # второй раз — без приписки

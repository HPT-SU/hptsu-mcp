"""Smoke checks for the FastMCP server wiring."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from hptsu_mcp.client import HptSuApiError, HptSuClient
from hptsu_mcp.config import Settings
from hptsu_mcp.server import (
    PAGE_SIZE_MAX,
    REGISTRY_KINDS,
    _err,
    _resolve_or_message,
    _search_kind,
    mcp,
)


_BASE = "https://hpt.su/api/v1"


def _settings() -> Settings:
    return Settings(api_key="test-key", base_url=_BASE)


def _ctx(client: HptSuClient) -> SimpleNamespace:
    """Минимальный фейковый Context: только то, что читают _get_client/_request_token."""
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"client": client},
            session=SimpleNamespace(client_params=None),
            request=None,
        ),
    )


def test_registry_kinds_documented() -> None:
    assert {"cert", "decl", "otts", "sbkts", "otch", "sout"} <= REGISTRY_KINDS.keys()


def test_mcp_app_constructed() -> None:
    assert mcp.name == "hpt-su"


def test_page_size_max_is_50() -> None:
    assert PAGE_SIZE_MAX == 50


def test_err_no_filters_mentions_available_filters() -> None:
    exc = HptSuApiError(
        400, "Задайте хотя бы один фильтр.",
        code="NO_FILTERS", available_filters=["brand", "number", "vin"],
    )
    msg = _err(exc)
    assert "Retry with a filter" in msg
    assert "brand, number, vin" in msg


def test_err_no_filters_without_list() -> None:
    exc = HptSuApiError(400, "Задайте хотя бы один фильтр.", code="NO_FILTERS")
    msg = _err(exc)
    assert "Retry with a filter." in msg
    assert "Available filters" not in msg


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    """HIGH#223: список tools обновлён до фактического server.py (0.2.0).

    search_type_approvals / search_safety_reports были удалены в пользу
    per-kind tools (search_otts/otch/zotts/zotch/sbkts/zoets/sout).
    """
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "search_documents", "get_document",
        "search_certificates", "search_declarations",
        "search_otts", "search_otch",
        "search_zotts", "search_zotch",
        "search_sbkts", "search_zoets", "search_sout",
        "search_by_vin", "fulltext_search",
        "list_document_files", "download_document_file",
        "list_brands", "list_vehicle_models",
        "list_test_labs", "list_certification_bodies",
        "list_tnved_codes", "list_registry_kinds",
    }
    assert expected <= names, f"Missing tools: {expected - names}"


# ──── Авто-резолв «имя справочника → id» ─────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_or_message_digit_passthrough() -> None:
    """Числовое значение = уже id, справочник не дёргаем."""
    async with HptSuClient(_settings()) as client:
        rid, msg = await _resolve_or_message(
            client, "/nsi/brands/", "417", token=None, label="brand")
    assert rid == "417"
    assert msg is None


@pytest.mark.asyncio
async def test_resolve_or_message_found() -> None:
    with respx.mock(base_url=_BASE) as mock:
        mock.get("/nsi/brands/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 417, "title": "КАМАЗ"}]}))
        async with HptSuClient(_settings()) as client:
            rid, msg = await _resolve_or_message(
                client, "/nsi/brands/", "KAMAZ", token=None, label="brand")
    assert rid == "417"
    assert msg is None


@pytest.mark.asyncio
async def test_resolve_or_message_ambiguous() -> None:
    with respx.mock(base_url=_BASE) as mock:
        mock.get("/nsi/certification-bodies/").mock(return_value=httpx.Response(
            200, json={"count": 3, "results": [
                {"id": 1, "name": "A"}, {"id": 2, "name": "B"}, {"id": 3, "name": "C"}]}))
        async with HptSuClient(_settings()) as client:
            rid, msg = await _resolve_or_message(
                client, "/nsi/certification-bodies/", "НАМИ", token=None, label="issuer")
    assert rid is None
    assert "ambiguous" in msg
    assert "id=1" in msg


@pytest.mark.asyncio
async def test_resolve_or_message_not_found() -> None:
    with respx.mock(base_url=_BASE) as mock:
        mock.get("/nsi/brands/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            rid, msg = await _resolve_or_message(
                client, "/nsi/brands/", "Нетакого", token=None, label="brand")
    assert rid is None
    assert "not found" in msg


@pytest.mark.asyncio
async def test_search_kind_resolves_issuer_via_cert_bodies() -> None:
    """otts.issuer='НАМИ' → резолв через /nsi/certification-bodies/ → id в /docs/."""
    with respx.mock(base_url=_BASE) as mock:
        cb = mock.get("/nsi/certification-bodies/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 42, "name": "ОС НАМИ"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "otts", page=1, page_size=20, issuer="НАМИ")
    assert cb.called
    assert docs.calls.last.request.url.params["issuer"] == "42"


@pytest.mark.asyncio
async def test_search_kind_sbkts_issuer_via_test_labs() -> None:
    """sbkts.issuer резолвится через /nsi/test-labs/ (не cert-bodies)."""
    with respx.mock(base_url=_BASE) as mock:
        tl = mock.get("/nsi/test-labs/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 7, "name": "НАМИ лаб"}]}))
        docs = mock.get("/docs/sbkts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "sbkts", page=1, page_size=20, issuer="НАМИ")
    assert tl.called
    assert docs.calls.last.request.url.params["issuer"] == "7"


@pytest.mark.asyncio
async def test_search_kind_ambiguous_issuer_skips_search() -> None:
    with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get("/nsi/certification-bodies/").mock(return_value=httpx.Response(
            200, json={"count": 2, "results": [
                {"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            out = await _search_kind(_ctx(client), "otts", page=1, page_size=20, issuer="НАМИ")
    assert "ambiguous" in out
    assert not docs.called


@pytest.mark.asyncio
async def test_search_kind_numeric_issuer_no_nsi_call() -> None:
    with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        cb = mock.get("/nsi/certification-bodies/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "otts", page=1, page_size=20, issuer="42")
    assert not cb.called
    assert docs.calls.last.request.url.params["issuer"] == "42"


@pytest.mark.asyncio
async def test_search_kind_resolves_eco_class_digit_not_id() -> None:
    """eco_class='5' — семантическое значение, а НЕ id: резолвим через справочник."""
    with respx.mock(base_url=_BASE) as mock:
        eco = mock.get("/nsi/eco-classes/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 6, "short_name": "5", "name": "Пятый"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "otts", page=1, page_size=20, eco_class="5")
    assert eco.calls.last.request.url.params["name"] == "5"
    assert docs.calls.last.request.url.params["eco_class"] == "6"


@pytest.mark.asyncio
async def test_search_kind_resolves_wheel_formula() -> None:
    with respx.mock(base_url=_BASE) as mock:
        wf = mock.get("/nsi/wheel-formulas/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 5, "name": "4 x 2"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "otts", page=1, page_size=20, wheel_formula="4x2")
    assert wf.calls.last.request.url.params["name"] == "4x2"
    assert docs.calls.last.request.url.params["wheel_formula"] == "5"


@pytest.mark.asyncio
async def test_search_kind_axis_count_numeric_uses_axis_count_param() -> None:
    with respx.mock(base_url=_BASE) as mock:
        ax = mock.get("/nsi/axis-counts/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 9, "name": "3 / 6"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "otts", page=1, page_size=20, axis_count="3")
    assert ax.calls.last.request.url.params["axis_count"] == "3"
    assert docs.calls.last.request.url.params["axis_count"] == "9"


@pytest.mark.asyncio
async def test_search_kind_axis_count_name_uses_name_param() -> None:
    with respx.mock(base_url=_BASE) as mock:
        ax = mock.get("/nsi/axis-counts/").mock(return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 5, "name": "2 / 4"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            await _search_kind(_ctx(client), "otts", page=1, page_size=20, axis_count="2 / 4")
    assert ax.calls.last.request.url.params["name"] == "2 / 4"
    assert docs.calls.last.request.url.params["axis_count"] == "5"


@pytest.mark.asyncio
async def test_search_kind_axis_count_ambiguous_returns_candidates() -> None:
    with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get("/nsi/axis-counts/").mock(return_value=httpx.Response(
            200, json={"count": 6, "results": [
                {"id": 5, "name": "2 / 4"}, {"id": 16, "name": "2 / 6"}]}))
        docs = mock.get("/docs/otts/").mock(return_value=httpx.Response(
            200, json={"count": 0, "results": []}))
        async with HptSuClient(_settings()) as client:
            out = await _search_kind(_ctx(client), "otts", page=1, page_size=20, axis_count="2")
    assert "ambiguous" in out
    assert "2 / 4" in out
    assert not docs.called

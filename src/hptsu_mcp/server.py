"""MCP server exposing the hpt.su document registry as tools.

Source registry: https://hpt.su — Russian/EAEU vehicle compliance documents
(type-approvals ОТТС/ОТШ, safety-of-design certificates СБКТС, conformity
certificates, declarations, type-approval notifications СУТ and more).

Authentication:

* Free MCP scope key — issue at https://hpt.su/cabinet/mcp/, 50 requests/day.
* Paid (`scope=MCP_PAID`) — 10 000 requests/day, includes full-text search
  and downloads.

The server speaks Model Context Protocol over stdio by default — point any
MCP-aware client (Claude Desktop, Cursor, Cline, Continue, Goose, Cherry,
5ire, LM Studio, …) at the ``hptsu-mcp`` executable. A streamable HTTP
transport mode is provided via ``HPTSU_TRANSPORT=http`` for hosted setups
(e.g. ``https://mcp.hpt.su``).
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .client import HptSuApiError, HptSuClient
from .config import Settings, load_settings


log = logging.getLogger("hptsu_mcp")


PAGE_SIZE_MAX = 50  # see DECISIONS.md C15 — bounded to fit LLM context.

REGISTRY_KINDS: dict[str, str] = {
    "cert": "Сертификат соответствия (Conformity Certificate, ТР ТС/ТР ЕАЭС)",
    "decl": "Декларация о соответствии (Declaration of Conformity, ТР ТС/ТР ЕАЭС)",
    "otts": "ОТТС — Одобрение типа транспортного средства (Vehicle Type Approval, EAEU)",
    "sbkts": "СБКТС — Свидетельство о безопасности конструкции ТС",
    "otch": "ОТШ — Одобрение типа шасси (Chassis Type Approval)",
    "sout": "СУТ — Сообщение об утверждении типа транспортного средства (Notification of Type Approval)",
    "zoets": "ЗОЕТС — Заключение об оценке единичного транспортного средства",
    "zotch": "ЗОТШ — Заключение об оценке типа шасси",
    "zotts": "ЗОТТС — Заключение об оценке типа транспортного средства",
}


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    settings: Settings = load_settings()
    client = HptSuClient(settings)
    try:
        yield {"client": client, "settings": settings}
    finally:
        await client.close()


mcp = FastMCP(
    "hpt-su",
    instructions=(
        "Search the hpt.su registry of Russian / EAEU vehicle compliance "
        "documents (https://hpt.su): type approvals (ОТТС/СБКТС/ОТШ), "
        "conformity certificates (ТР ТС/ТР ЕАЭС), declarations of conformity, "
        "type-approval notifications СУТ. Read-only. Free tier (50 req/day) "
        "available at https://hpt.su/cabinet/mcp/. Paid tier (10 000 req/day) "
        "unlocks full-text search and file downloads."
    ),
    lifespan=_lifespan,
)


def _get_client(ctx: Context) -> HptSuClient:
    # Attach client attribution once we know who is calling — InitializeParams.
    client: HptSuClient = ctx.request_context.lifespan_context["client"]
    info = getattr(ctx.request_context.session, "client_params", None)
    client_info = getattr(info, "clientInfo", None) if info else None
    if client_info is not None:
        client.set_mcp_client(
            getattr(client_info, "name", None),
            getattr(client_info, "version", None),
        )
    return client


def _request_token(ctx: Context) -> str | None:
    """Извлечь токен из заголовка входящего HTTP-запроса (hosted mode).

    Поддерживаемые входные форматы:

    * ``X-API-Key: <public_id>:<secret>`` — статичный ApiKey, пробрасываем
      в upstream через ``X-API-Key`` (формат, который понимает
      ``td_billing.api.auth.ApiKeyAuthentication``).
    * ``Authorization: Bearer <oauth_token>`` — OAuth2 access token
      (Cursor / Smithery / Claude через OAuth flow). Помечаем префиксом
      ``BEARER `` (см. ``HptSuClient._build_headers``) — клиент пробросит
      как ``Authorization: Bearer ...`` в upstream, где его прочитает
      ``OAuthOrApiKeyAuthentication``.

    Для stdio заголовков нет → None, клиент возьмёт
    ``settings.api_key`` из env.
    """
    req = getattr(ctx.request_context, "request", None)
    headers = getattr(req, "headers", None)
    if headers is None:
        return None
    direct = headers.get("x-api-key")
    if direct:
        return direct
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        return f"BEARER {token}" if token else None
    return None


def _format(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def _err(exc: HptSuApiError) -> str:
    if exc.status_code == 402:
        return (
            f"Payment required ({exc.detail}). Upgrade your MCP key to the "
            "paid tier at https://hpt.su/pricing/ to unlock this tool."
        )
    if exc.status_code == 429:
        return (
            "Rate limit reached. Free MCP tier is 50 requests/day — "
            "upgrade at https://hpt.su/pricing/ for 10 000 req/day."
        )
    if exc.status_code == 404:
        return f"Not found: {exc.detail}"
    return f"hpt.su API error {exc.status_code}: {exc.detail}"


# ──── Search & retrieval ────────────────────────────────────────────────


@mcp.tool()
async def search_documents(
    ctx: Context,
    number: str | None = Field(default=None, description="Document number, full or partial."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Cross-registry search by document number across all hpt.su kinds."""
    client = _get_client(ctx)
    token = _request_token(ctx)
    try:
        data = await client.list_documents(number=number, page=page, page_size=page_size, token=token)
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def get_document(
    ctx: Context,
    document_id: str = Field(description="Document UUID (`number_code`)."),
) -> str:
    """Fetch a single Document by its UUID."""
    client = _get_client(ctx)
    try:
        return _format(await client.get_document(document_id, token=_request_token(ctx)))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def search_certificates(
    ctx: Context,
    number: str | None = Field(default=None, description="Certificate number, full or partial."),
    applicant: str | None = Field(default=None, description="Applicant name (icontains)."),
    applicant_inn: str | None = Field(default=None, description="Applicant INN (exact match)."),
    manufacturer: str | None = Field(default=None, description="Manufacturer name (icontains)."),
    regulations: str | None = Field(default=None, description="Technical regulation code (e.g. 'ТР ТС 018/2011')."),
    product: str | None = Field(default=None, description="Product full name (icontains)."),
    status: str | None = Field(default=None, description="Certificate status code."),
    scheme: str | None = Field(default=None, description="Certification scheme — '1с'…'9с'."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search conformity certificates (ТР ТС / ТР ЕАЭС)."""
    client = _get_client(ctx)
    try:
        data = await client.list_certificates(
            number=number, applicant=applicant, applicant_inn=applicant_inn,
            manufacturer=manufacturer, regulations=regulations, product=product,
            status=status, scheme=scheme,
            page=page, page_size=page_size,
            token=_request_token(ctx),
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def search_declarations(
    ctx: Context,
    number: str | None = Field(default=None, description="Declaration number, full or partial."),
    applicant: str | None = Field(default=None, description="Applicant name (icontains)."),
    applicant_inn: str | None = Field(default=None, description="Applicant INN (exact match)."),
    manufacturer: str | None = Field(default=None, description="Manufacturer name (icontains)."),
    regulations: str | None = Field(default=None, description="Technical regulation code (e.g. 'ТР ТС 018/2011')."),
    product: str | None = Field(default=None, description="Product full name (icontains)."),
    status: str | None = Field(default=None, description="Declaration status code."),
    scheme: str | None = Field(default=None, description="Declaration scheme — '1д'…'6д'."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search declarations of conformity (ТР ТС / ТР ЕАЭС)."""
    client = _get_client(ctx)
    try:
        data = await client.list_declarations(
            number=number, applicant=applicant, applicant_inn=applicant_inn,
            manufacturer=manufacturer, regulations=regulations, product=product,
            status=status, scheme=scheme,
            page=page, page_size=page_size,
            token=_request_token(ctx),
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


async def _search_kind(ctx: Context, kind: str, *, page: int, page_size: int, **kw) -> str:
    """Shared body for per-kind search tools."""
    client = _get_client(ctx)
    try:
        data = await client.list_by_kind(
            kind, page=page, page_size=page_size,
            token=_request_token(ctx),
            **{k: v for k, v in kw.items() if v is not None},
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def search_otts(
    ctx: Context,
    number: str | None = Field(default=None, description="ОТТС number, full or partial."),
    vin: str | None = Field(default=None, description="VIN substring (5-17 chars) — backend autoroute substring/DAWG."),
    brand: str | None = Field(default=None, description="Vehicle brand (e.g. 'Toyota')."),
    type: str | None = Field(default=None, description="Vehicle type / model (icontains)."),
    comm_name: str | None = Field(default=None, description="Commercial name (icontains)."),
    chassis: str | None = Field(default=None, description="Chassis identifier (icontains)."),
    mods: str | None = Field(default=None, description="Modifications (icontains)."),
    category: str | None = Field(default=None, description="Vehicle category (M1, N2, L3, …)."),
    eco_class: str | None = Field(default=None, description="Ecological class (Euro 5, etc.)."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (4x2, 6x4, …)."),
    axis_count: int | None = Field(default=None, description="Number of axles."),
    issuer: str | None = Field(default=None, description="Certification body id (см. list_certification_bodies)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ОТТС (vehicle type approvals, ТР ТС 018/2011)."""
    return await _search_kind(
        ctx, "otts", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type, comm_name=comm_name,
        chassis=chassis, mods=mods,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
    )


@mcp.tool()
async def search_otch(
    ctx: Context,
    number: str | None = Field(default=None, description="ОТШ number, full or partial."),
    vin: str | None = Field(default=None, description="VIN substring (5-17 chars)."),
    brand: str | None = Field(default=None, description="Brand."),
    type: str | None = Field(default=None, description="Type / model."),
    comm_name: str | None = Field(default=None, description="Commercial name."),
    category: str | None = Field(default=None, description="Vehicle category."),
    eco_class: str | None = Field(default=None, description="Ecological class."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula."),
    axis_count: int | None = Field(default=None, description="Number of axles."),
    issuer: str | None = Field(default=None, description="Certification body id."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ОТШ (chassis type approvals)."""
    return await _search_kind(
        ctx, "otch", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type, comm_name=comm_name,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
    )


@mcp.tool()
async def search_zotts(
    ctx: Context,
    number: str | None = Field(default=None, description="ЗОТТС number."),
    vin: str | None = Field(default=None, description="VIN substring."),
    brand: str | None = Field(default=None, description="Brand."),
    type: str | None = Field(default=None, description="Type."),
    category: str | None = Field(default=None, description="Vehicle category."),
    eco_class: str | None = Field(default=None, description="Ecological class."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula."),
    axis_count: int | None = Field(default=None, description="Number of axles."),
    issuer: str | None = Field(default=None, description="Certification body id."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ЗОТТС (vehicle conformity assessment conclusion)."""
    return await _search_kind(
        ctx, "zotts", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
    )


@mcp.tool()
async def search_zotch(
    ctx: Context,
    number: str | None = Field(default=None, description="ЗОТШ number."),
    vin: str | None = Field(default=None, description="VIN substring."),
    brand: str | None = Field(default=None, description="Brand."),
    type: str | None = Field(default=None, description="Type."),
    category: str | None = Field(default=None, description="Vehicle category."),
    eco_class: str | None = Field(default=None, description="Ecological class."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula."),
    axis_count: int | None = Field(default=None, description="Number of axles."),
    issuer: str | None = Field(default=None, description="Certification body id."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ЗОТШ (chassis conformity assessment conclusion)."""
    return await _search_kind(
        ctx, "zotch", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
    )


@mcp.tool()
async def search_sbkts(
    ctx: Context,
    number: str | None = Field(default=None, description="СБКТС number."),
    vin: str | None = Field(default=None, description="VIN (10-17 chars; substring/exact)."),
    brand: str | None = Field(default=None, description="Brand."),
    type: str | None = Field(default=None, description="Type."),
    comm_name: str | None = Field(default=None, description="Commercial name."),
    engine: str | None = Field(default=None, description="ICE engine model (icontains)."),
    year: int | None = Field(default=None, description="Manufacture year (YYYY)."),
    motor: str | None = Field(default=None, description="Electric motor model (icontains)."),
    motor_power: int | None = Field(default=None, description="Motor power (kW)."),
    category: str | None = Field(default=None, description="Vehicle category."),
    eco_class: str | None = Field(default=None, description="Ecological class."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula."),
    axis_count: int | None = Field(default=None, description="Number of axles."),
    issuer: str | None = Field(default=None, description="Testing lab id (см. list_test_labs)."),
    date_from: str | None = Field(default=None, description="Issue date from (YYYY-MM-DD)."),
    date_to: str | None = Field(default=None, description="Issue date to (YYYY-MM-DD)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search СБКТС (vehicle safety certificate)."""
    return await _search_kind(
        ctx, "sbkts", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type, comm_name=comm_name,
        engine=engine, year=year, motor=motor, motor_power=motor_power,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
        date_from=date_from, date_to=date_to,
    )


@mcp.tool()
async def search_zoets(
    ctx: Context,
    number: str | None = Field(default=None, description="ЗОЕТС number."),
    vin: str | None = Field(default=None, description="VIN (10-17 chars)."),
    brand: str | None = Field(default=None, description="Brand."),
    type: str | None = Field(default=None, description="Type."),
    comm_name: str | None = Field(default=None, description="Commercial name."),
    engine: str | None = Field(default=None, description="ICE engine model."),
    year: int | None = Field(default=None, description="Manufacture year."),
    motor: str | None = Field(default=None, description="Electric motor model."),
    motor_power: int | None = Field(default=None, description="Motor power (kW)."),
    category: str | None = Field(default=None, description="Vehicle category."),
    eco_class: str | None = Field(default=None, description="Ecological class."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula."),
    axis_count: int | None = Field(default=None, description="Number of axles."),
    issuer: str | None = Field(default=None, description="Testing lab id."),
    date_from: str | None = Field(default=None, description="Issue date from (YYYY-MM-DD)."),
    date_to: str | None = Field(default=None, description="Issue date to (YYYY-MM-DD)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ЗОЕТС (vehicle technical expertise conclusion)."""
    return await _search_kind(
        ctx, "zoets", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type, comm_name=comm_name,
        engine=engine, year=year, motor=motor, motor_power=motor_power,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
        date_from=date_from, date_to=date_to,
    )


@mcp.tool()
async def search_sout(
    ctx: Context,
    number: str | None = Field(default=None, description="СУТ number."),
    brand: str | None = Field(default=None, description="Brand."),
    type: str | None = Field(default=None, description="Type."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search СУТ (vehicle type notification — small dataset, only basic fields)."""
    return await _search_kind(
        ctx, "sout", page=page, page_size=page_size,
        number=number, brand=brand, type=type,
    )


@mcp.tool()
async def search_by_vin(
    ctx: Context,
    vin: str = Field(description="VIN substring or full code (5-17 chars)."),
) -> str:
    """Aggregated search by VIN across all car-kinds (ОТТС/ОТШ/ЗОТТС/ЗОТШ/
    СБКТС/ЗОЕТС). Open for any active API key — no subscription required.

    Returns substring matches via UNION across kind-tables sorted by
    `issue_date DESC`. For exact full-VIN match within a single kind,
    use the per-kind tool (search_otts/search_sbkts/…) — they autoroute
    substring↔DAWG by VIN validity.
    """
    if not (5 <= len(vin) <= 17):
        return f"Invalid VIN length: {len(vin)} chars (expected 5-17)."
    client = _get_client(ctx)
    try:
        return _format(await client.search_by_vin(vin, token=_request_token(ctx)))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def fulltext_search(
    ctx: Context,
    query: str = Field(description="Free-text query (Russian, tsquery-syntax allowed)."),
    kind: str | None = Field(
        default=None,
        description="Filter by registry kind. One of: otts, otch, zotts, zotch. "
                    "Default: search across all 4 type-approval kinds.",
    ),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Full-text search inside type-approval document bodies (PDF text).

    Covers ОТТС / ОТШ / ЗОТТС / ЗОТШ — kinds where `Document.fulltext` index
    is populated. СБКТС / ЗОЕТС / СУТ / cert / decl don't have fulltext
    index and are not searchable here.

    **Premium feature** — requires a paid MCP key with `use_fulltext` +
    subscription on at least one type-approval kind.
    """
    if kind and kind not in ("otts", "otch", "zotts", "zotch"):
        return f"Invalid kind={kind!r}. Allowed: otts, otch, zotts, zotch (or omit for all)."
    client = _get_client(ctx)
    try:
        data = await client.fulltext_search(query, kind=kind, page=page, page_size=page_size,
                                            token=_request_token(ctx))
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def list_document_files(
    ctx: Context,
    document_id: str = Field(description="Document UUID (number_code from search)."),
) -> str:
    """List the files attached to a document.

    Returns `[{file_uid, file_name, kind, pages_count, published_at}]` —
    each entry's `file_uid` can be passed to `download_document_file`.

    A document often has several PDFs (e.g. cleaned + original); use this
    tool to enumerate them and pick the right one before download.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.list_document_files(
            document_id, token=_request_token(ctx)))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def download_document_file(
    ctx: Context,
    file_uid: str = Field(description="DocumentFile UID (from list_document_files)."),
) -> str:
    """Issue a signed URL to download the document PDF from hpt.su.

    Returns `{download_url, file_name, kind, document_id}`. The URL is
    encrypted with the user_id behind the API key — opening it works only
    if the user is signed in to hpt.su under the same account. The
    `dl_counter` is decremented on the website at actual download time, not
    here.

    Requires an active subscription covering the document's kind or a
    stand-alone DOC_PURCHASE. On the free tier returns 403 with an
    upgrade prompt to https://hpt.su/pricing/.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.download_document_file(
            file_uid, token=_request_token(ctx)))
    except HptSuApiError as exc:
        return _err(exc)


# ──── Reference / NSI dictionaries ──────────────────────────────────────


@mcp.tool()
async def list_brands(
    ctx: Context,
    name: str | None = Field(default=None, description="Brand name substring (e.g. 'KAMAZ')."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Resolve a vehicle brand name to its canonical record (used to filter
    type approvals)."""
    client = _get_client(ctx)
    try:
        return _format(await client.list_brands(
            name=name, page=page, page_size=page_size, token=_request_token(ctx)))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def list_vehicle_models(
    ctx: Context,
    brand: str | None = Field(default=None, description="Brand name or id."),
    name: str | None = Field(default=None, description="Model name substring."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Resolve a vehicle model name (within a brand) to canonical record."""
    client = _get_client(ctx)
    try:
        return _format(await client.list_vehicle_models(
            brand=brand, name=name, page=page, page_size=page_size,
            token=_request_token(ctx),
        ))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def list_test_labs(
    ctx: Context,
    name: str | None = Field(default=None, description="Lab name substring."),
    short_id: str | None = Field(default=None, description="Lab short identifier."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Lookup accredited testing laboratories (issuers of СБКТС/ЗОЕТС)."""
    client = _get_client(ctx)
    try:
        return _format(await client.list_test_labs(
            name=name, short_id=short_id, page=page, page_size=page_size,
            token=_request_token(ctx),
        ))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def list_certification_bodies(
    ctx: Context,
    name: str | None = Field(default=None, description="Body name substring."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Lookup accredited certification bodies (issuers of сертификатов/деклараций)."""
    client = _get_client(ctx)
    try:
        return _format(await client.list_certification_bodies(
            name=name, page=page, page_size=page_size,
            token=_request_token(ctx),
        ))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def list_tnved_codes(
    ctx: Context,
    prefix: str | None = Field(default=None, description="TN VED code prefix (e.g. '8704')."),
    query: str | None = Field(default=None, description="Free-text description search."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """TN VED EAEU classifier lookup.

    Note: TN VED codes are linked **only** to certificates and declarations —
    not to vehicle type approvals or safety reports.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.list_tnved_codes(
            prefix=prefix, query=query, page=page, page_size=page_size,
            token=_request_token(ctx),
        ))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def list_registry_kinds() -> str:
    """Return the catalogue of registry kinds exposed by the hpt.su public API."""
    return _format([{"kind": k, "title": v} for k, v in REGISTRY_KINDS.items()])


# ──── Health check (HTTP transport only) ──────────────────────────────────


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request: Request) -> JSONResponse:
    """Liveness probe — used by Docker / Kubernetes / reverse-proxy.

    Always 200 when the server is up. Does not call upstream hpt.su (so it
    stays 200 during transient upstream outages and Docker doesn't kill the
    container). For upstream-aware probe see ``/readyz``.
    """
    return JSONResponse({"status": "ok", "version": __version__})


@mcp.custom_route("/readyz", methods=["GET"], include_in_schema=False)
async def readyz(_request: Request) -> JSONResponse:
    """Readiness probe — проверяет, что upstream hpt.su отвечает.

    Не использует сервисный ключ (контейнер на hosted его не хранит) —
    ходит в `/api/v1/docs/` без auth и ожидает 401. Это значит:

    * TLS-handshake прошёл;
    * DNS резолвится;
    * Django + DRF + ApiKeyAuthentication работают (вернули JSON-401).

    Любой 5xx / connection-error / timeout → 503 «not ready».
    """
    settings = load_settings()
    # Стираем default api_key чтобы probe был без auth (deterministic).
    settings = settings.model_copy(update={"api_key": None})
    async with HptSuClient(settings) as client:
        try:
            await client.list_documents(page=1, page_size=1)
        except HptSuApiError as exc:
            if exc.status_code in (401, 403):
                # Это ожидаемый результат — upstream живой и аутентифицирующий.
                return JSONResponse({"status": "ready", "version": __version__,
                                     "upstream": "auth-rejected-as-expected"})
            return JSONResponse(
                {"status": "error", "detail": f"upstream {exc.status_code}"},
                status_code=503,
            )
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "detail": str(exc)},
                status_code=503,
            )
        # Если 200 пришёл — тоже норм (вдруг unauthenticated endpoint открыт).
        return JSONResponse({"status": "ready", "version": __version__,
                             "upstream": "200"})


# ──── Resources ───────────────────────────────────────────────────────────


@mcp.resource("hptsu://about")
def about_registry() -> str:
    return (
        "hpt.su — registry of Russian and EAEU vehicle compliance documents.\n"
        "Free MCP tier: 50 req/day at https://hpt.su/user/mcp/.\n"
        "Paid tier: 10 000 req/day + full-text + downloads, https://hpt.su/pricing/.\n\n"
        "Registries:\n"
        + "\n".join(f"  • {kind} — {title}" for kind, title in REGISTRY_KINDS.items())
        + "\n\nAPI base: https://hpt.su/api/v1/\n"
        "Auth: X-API-Key: <public_id>:<secret> (issued at /user/mcp/),\n"
        "      or OAuth2 Bearer (DCR via /.well-known/oauth-authorization-server).\n"
        "Schema: https://hpt.su/api/v1/schema/\n\n"
        "Tools by kind: search_certificates / search_declarations / search_otts /\n"
        "  search_otch / search_zotts / search_zotch / search_sbkts / search_zoets /\n"
        "  search_sout. Plus search_documents (cross-kind by number), search_by_vin\n"
        "  (cross-kind VIN), fulltext_search (otts/otch/zotts/zotch, paid).\n"
        "NSI lookups: list_brands / list_vehicle_models / list_test_labs /\n"
        "  list_certification_bodies / list_tnved_codes.\n"
        "Files: list_document_files (free) / download_document_file (paid).\n"
    )


# ──── Entry point ─────────────────────────────────────────────────────────


def _configure_http_binding() -> None:
    """Apply host/port/allowed_hosts from env to FastMCP's settings.

    Used only for streamable-http and sse transports. FastMCP defaults to
    binding 127.0.0.1, which is wrong for hosted setups behind nginx;
    Docker forwards traffic to the container's external network interface,
    so we must bind 0.0.0.0 (configured via HPTSU_HOST in the compose file).

    Also relaxes the DNS-rebind protection: FastMCP defaults to allowing
    only localhost as Host header, which would reject `mcp.hpt.su`
    completely. Production must whitelist the public hostname.
    """
    settings: Settings = load_settings()
    mcp.settings.host = settings.host
    mcp.settings.port = settings.port
    if settings.allowed_hosts:
        hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
        ts = mcp.settings.transport_security
        # Add to the default localhost-allowlist so probes still work.
        ts.allowed_hosts = list(set(list(ts.allowed_hosts) + hosts))
        # Same for origins — keep localhost defaults, add public hosts.
        public_origins = [f"https://{h.split(':', 1)[0]}" for h in hosts]
        ts.allowed_origins = list(set(list(ts.allowed_origins) + public_origins))


def main() -> None:
    logging.basicConfig(
        level=os.getenv("HPTSU_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.getenv("HPTSU_TRANSPORT", "stdio").lower()
    if transport in {"http", "streamable-http", "streamable_http"}:
        _configure_http_binding()
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        _configure_http_binding()
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

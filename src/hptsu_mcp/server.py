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
    """Извлечь API-ключ из заголовка входящего HTTP-запроса (hosted mode).

    Для stdio — заголовка нет, возвращаем None и клиент использует
    `settings.api_key` из env. В streamable-http режиме MCP-клиент
    (Claude/Cursor/Cline) кладёт ключ в ``Authorization: Bearer <token>``
    или ``X-API-Key: <token>``; парсим оба, чтобы не зависеть от
    конкретной реализации клиента.

    Формат токена ожидается ``<public_id>:<secret>`` — это то, что
    td_billing.api.auth.ApiKeyAuthentication принимает в X-API-Key.
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
        return auth.split(" ", 1)[1].strip() or None
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
    applicant: str | None = Field(default=None, description="Applicant / manufacturer name."),
    status: str | None = Field(default=None, description="Certificate status code."),
    scheme: str | None = Field(default=None, description="Certification scheme — '1с'…'9с'."),
    code: str | None = Field(default=None, description="TN VED EAEU / OKPD2 classifier code."),
    has_doc: bool | None = Field(default=None, description="Only entries with attached files."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search conformity certificates (ТР ТС / ТР ЕАЭС)."""
    client = _get_client(ctx)
    try:
        data = await client.list_certificates(
            number=number, applicant=applicant, status=status, scheme=scheme,
            code=code, has_doc=has_doc, page=page, page_size=page_size,
            token=_request_token(ctx),
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def search_declarations(
    ctx: Context,
    number: str | None = Field(default=None, description="Declaration number, full or partial."),
    applicant: str | None = Field(default=None, description="Applicant / manufacturer name."),
    status: str | None = Field(default=None, description="Declaration status code."),
    code: str | None = Field(default=None, description="TN VED EAEU classifier code."),
    has_doc: bool | None = Field(default=None, description="Only entries with attached files."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search declarations of conformity (ТР ТС / ТР ЕАЭС)."""
    client = _get_client(ctx)
    try:
        data = await client.list_declarations(
            number=number, applicant=applicant, status=status, code=code,
            has_doc=has_doc, page=page, page_size=page_size,
            token=_request_token(ctx),
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def search_type_approvals(
    ctx: Context,
    kind: str = Field(default="otts", description="'otts' (vehicle) or 'otch' (chassis)."),
    number: str | None = Field(default=None, description="Document number, full or partial."),
    applicant: str | None = Field(default=None, description="Holder / manufacturer name."),
    brand: str | None = Field(default=None, description="Vehicle brand."),
    model: str | None = Field(default=None, description="Vehicle model."),
    year: int | None = Field(default=None, description="Issue year (YYYY)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Vehicle (ОТТС) or chassis (ОТШ) type approvals (TR CU 018)."""
    if kind not in {"otts", "otch"}:
        return f"Invalid kind={kind!r}. Use one of: otts, otch."
    client = _get_client(ctx)
    try:
        data = await client.list_by_kind(
            kind, number=number, applicant=applicant, brand=brand, model=model,
            year=year, page=page, page_size=page_size, token=_request_token(ctx),
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def search_safety_reports(
    ctx: Context,
    kind: str = Field(description="One of: sbkts, sout, zoets, zotch, zotts."),
    number: str | None = Field(default=None, description="Document number."),
    applicant: str | None = Field(default=None, description="Applicant / employer name."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search СБКТС / СУТ / ЗО* feeds. For ОТТС/ОТШ use search_type_approvals."""
    allowed = {"sbkts", "sout", "zoets", "zotch", "zotts"}
    if kind not in allowed:
        return f"Invalid kind={kind!r}. Allowed: {sorted(allowed)}."
    client = _get_client(ctx)
    try:
        data = await client.list_by_kind(
            kind, number=number, applicant=applicant, page=page, page_size=page_size,
            token=_request_token(ctx),
        )
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def search_by_vin(
    ctx: Context,
    vin: str = Field(description="17-character VIN (Vehicle Identification Number)."),
) -> str:
    """Find every hpt.su document tied to a given VIN — aggregated across
    ОТТС / ОТШ / СБКТС / ЗОЕТС / ЗОТТС.

    Requires the `/docs/by_vin/` endpoint on hpt.su (planned — currently in
    integration backlog, see integration-hpt-su.md §5). Will return 'Not yet
    available' until upstream lands.
    """
    if not (10 <= len(vin) <= 17):
        return f"Invalid VIN length: {len(vin)} chars (expected 10-17)."
    client = _get_client(ctx)
    try:
        return _format(await client.search_by_vin(vin, token=_request_token(ctx)))
    except HptSuApiError as exc:
        return _err(exc)


@mcp.tool()
async def fulltext_search(
    ctx: Context,
    query: str = Field(description="Free-text query (Russian or English)."),
    kind: str | None = Field(default=None, description="Filter by registry kind (optional)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Full-text search inside PDF bodies. **Premium feature** — requires a
    paid MCP key with `use_fulltext` enabled.
    """
    client = _get_client(ctx)
    try:
        data = await client.fulltext_search(query, kind=kind, page=page, page_size=page_size,
                                            token=_request_token(ctx))
    except HptSuApiError as exc:
        return _err(exc)
    return _format(data)


@mcp.tool()
async def download_document_file(
    ctx: Context,
    document_id: str = Field(description="Document UUID."),
    file_id: str | None = Field(default=None, description="Specific file UUID (if a document has several)."),
) -> str:
    """Issue a signed, time-limited URL to download the document PDF.

    Requires an active subscription covering the document's kind or a
    stand-alone DOC_PURCHASE. On the free tier this returns an upgrade
    prompt with a link to https://hpt.su/pricing/.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.download_document_file(
            document_id, file_id=file_id, token=_request_token(ctx)))
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
        "Free MCP tier: 50 req/day at https://hpt.su/cabinet/mcp/.\n"
        "Paid tier: 10 000 req/day + full-text + downloads, https://hpt.su/pricing/.\n\n"
        "Registries:\n"
        + "\n".join(f"  • {kind} — {title}" for kind, title in REGISTRY_KINDS.items())
        + "\n\nAPI base: https://hpt.su/api/v1/   Auth: Bearer ApiKey (MCP scope).\n"
        "Schema: https://hpt.su/api/v1/schema/\n"
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

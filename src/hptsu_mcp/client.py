"""HTTP client for hpt.su public API (`/api/v1/`).

Wraps the DRF endpoints documented at https://hpt.su/api/v1/docs/ (OpenAPI
schema at /api/v1/schema/). All endpoints are read-only ViewSets that require
Bearer ApiKey auth backed by an active API_TIER subscription, **or** a free
MCP scope key obtained on https://hpt.su/cabinet/mcp/.

Some endpoints below are not yet live in hpt_su — see
`docs/integration-hpt-su.md` (P1 §5-9). The MCP-server returns a polite
"not yet available" error if the upstream answers 404 / 501.
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class HptSuApiError(RuntimeError):
    """Raised when the hpt.su API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"hpt.su API {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HptSuClient:
    """Async client over /api/v1/.

    Один экземпляр на MCP-сессию. API-ключ может задаваться через:

    1. ``settings.api_key`` — default header (для stdio-режима, ключ из env).
    2. Per-call ``token=`` параметр в любом методе ниже — для hosted-режима
       (mcp.hpt.su), где ключ извлекается из заголовка ``Authorization``
       входящего MCP-запроса и пробрасывается в upstream per-tool-call.

    Если ни то ни другое не задано — ходим без auth (используется
    ``/readyz`` probe, ждёт 401 как «upstream живой»).

    Формат токена везде один: ``<public_id>:<secret>``. На hpt.su backend
    он передаётся в заголовке ``X-API-Key`` (см. td_billing.api.auth).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._default_token = settings.api_key
        self._mcp_client_tag: str | None = None
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": settings.user_agent,
            },
        )

    def set_mcp_client(self, name: str | None, version: str | None) -> None:
        """Attach X-MCP-Client header sourced from the MCP `clientInfo` block."""
        if not name:
            return
        self._mcp_client_tag = f"{name}/{version}" if version else name

    def _build_headers(self, token: str | None) -> dict[str, str]:
        """Compose headers for outgoing API call.

        `token` may be:

        * ``None`` → use the default ``settings.api_key`` as X-API-Key.
        * ``"BEARER <oauth_token>"`` → forwarded as
          ``Authorization: Bearer <oauth_token>`` (hosted MCP via OAuth).
        * Any other string → forwarded as ``X-API-Key`` (stdio or hosted
          MCP using a static ApiKey).

        The ``BEARER `` prefix is a hint produced by server-side helper
        ``_request_token`` when it detects a Bearer header in the
        incoming MCP HTTP request.
        """
        h: dict[str, str] = {}
        chosen = token if token is not None else self._default_token
        if chosen:
            if chosen.startswith("BEARER "):
                h["Authorization"] = "Bearer " + chosen[len("BEARER "):]
            else:
                h["X-API-Key"] = chosen
        if self._mcp_client_tag:
            h["X-MCP-Client"] = self._mcp_client_tag
        return h

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HptSuClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---------- raw plumbing ----------

    async def _get(self, path: str, *, params: dict[str, Any] | None = None,
                   token: str | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        resp = await self._client.get(path, params=clean, headers=self._build_headers(token))
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HptSuApiError(resp.status_code, str(detail))
        return resp.json()

    async def _post(self, path: str, *, json: dict[str, Any] | None = None,
                    token: str | None = None) -> Any:
        resp = await self._client.post(path, json=json or {},
                                       headers=self._build_headers(token))
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HptSuApiError(resp.status_code, str(detail))
        return resp.json()

    # ---------- public endpoints (live) ----------
    #
    # Каждый метод принимает опциональный `token` через **filters / **kwargs.
    # В hosted-режиме MCP-сервер достаёт ключ из Authorization-заголовка
    # входящего запроса и пробрасывает в каждый upstream-вызов; в stdio —
    # пользуется default из env (`settings.api_key`).

    async def list_documents(self, **filters: Any) -> dict[str, Any]:
        """Global cross-kind document search — `GET /docs/`."""
        token = filters.pop("token", None)
        return await self._get("/docs/", params=filters, token=token)

    async def get_document(self, doc_id: str, *, token: str | None = None) -> dict[str, Any]:
        """Document by UUID (number_code) — `GET /docs/{id}/`."""
        return await self._get(f"/docs/{doc_id}/", token=token)

    async def list_certificates(self, **filters: Any) -> dict[str, Any]:
        """Conformity certificates — `GET /docs/cert/`."""
        token = filters.pop("token", None)
        return await self._get("/docs/cert/", params=filters, token=token)

    async def list_declarations(self, **filters: Any) -> dict[str, Any]:
        """Declarations of conformity — `GET /docs/decl/`."""
        token = filters.pop("token", None)
        return await self._get("/docs/decl/", params=filters, token=token)

    async def list_by_kind(self, kind: str, **filters: Any) -> dict[str, Any]:
        """Per-kind list — `GET /docs/{kind}/`."""
        token = filters.pop("token", None)
        return await self._get(f"/docs/{kind}/", params=filters, token=token)

    # ---------- planned endpoints (require P1 work on hpt_su) ----------

    async def search_by_vin(self, vin: str, **filters: Any) -> dict[str, Any]:
        """Aggregated VIN search across ОТТС / СБКТС / ЗОЕТС — `GET /docs/by_vin/{vin}/`.

        Not yet live (see docs/integration-hpt-su.md §5). The server will 404
        until upstream lands.
        """
        token = filters.pop("token", None)
        return await self._get(f"/docs/by_vin/{vin}/", params=filters, token=token)

    async def fulltext_search(self, q: str, **filters: Any) -> dict[str, Any]:
        """Full-text search inside PDF bodies — `GET /docs/fulltext/?q=...`.

        Requires an active `use_fulltext` feature on the calling key
        (premium). Not yet live — see docs/integration-hpt-su.md §6.
        """
        token = filters.pop("token", None)
        return await self._get("/docs/fulltext/", params={"q": q, **filters}, token=token)

    async def list_document_files(self, document_id: str,
                                  *, token: str | None = None) -> list[dict[str, Any]]:
        """Список файлов документа — `GET /docs/{document_uuid}/files/`.

        Используется для resolve Document UUID → набор DocumentFile UIDs
        перед скачиванием через `download_document_file(file_uid)`.
        """
        return await self._get(f"/docs/{document_id}/files/", token=token)

    async def download_document_file(self, file_uid: str,
                                     *, token: str | None = None) -> dict[str, Any]:
        """Issue a signed download URL — `POST /files/{file_uid}/download/`.

        `file_uid` — DocumentFile UID (получить через `list_document_files`,
        не Document UUID). Возвращает `{download_url, file_name, kind, ...}`.
        URL зашифрован под текущего пользователя — счётчик списывается на
        сайте hpt.su при реальном скачивании.

        Требует активной подписки covering kind документа или DOC_PURCHASE.
        На free MCP вернёт 403 с upgrade-сообщением.
        """
        return await self._post(f"/files/{file_uid}/download/", token=token)

    # ---------- reference / NSI dictionaries ----------

    async def list_brands(self, **filters: Any) -> dict[str, Any]:
        """Vehicle brands — `GET /nsi/brands/` (planned, P1 §7)."""
        token = filters.pop("token", None)
        return await self._get("/nsi/brands/", params=filters, token=token)

    async def list_vehicle_models(self, **filters: Any) -> dict[str, Any]:
        """Vehicle models — `GET /nsi/vehicle-models/` (planned, P1 §7)."""
        token = filters.pop("token", None)
        return await self._get("/nsi/vehicle-models/", params=filters, token=token)

    async def list_test_labs(self, **filters: Any) -> dict[str, Any]:
        """Accredited testing laboratories — `GET /nsi/test-labs/`.

        AccreditedPerson kind=TEST_LAB. Filters: ``name`` (icontains
        по name_short/name_full/name_en), ``status``, ``country_code``.
        """
        token = filters.pop("token", None)
        return await self._get("/nsi/test-labs/", params=filters, token=token)

    async def list_certification_bodies(self, **filters: Any) -> dict[str, Any]:
        """Certification bodies — `GET /nsi/certification-bodies/`.

        AccreditedPerson kind=CERT_BODY. Same filters as ``list_test_labs``.
        """
        token = filters.pop("token", None)
        return await self._get("/nsi/certification-bodies/", params=filters, token=token)

    async def list_tnved_codes(self, **filters: Any) -> dict[str, Any]:
        """TN VED EAEU classifier — `GET /nsi/tnved/`.

        Only relevant for certificates and declarations; not linked to
        vehicle type-approvals. Filters: ``prefix`` (code startswith),
        ``query`` (name icontains), ``level``, ``is_active``.
        """
        token = filters.pop("token", None)
        return await self._get("/nsi/tnved/", params=filters, token=token)

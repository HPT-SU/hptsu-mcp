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
    """Async client over /api/v1/. One instance per MCP-session is enough."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
        }
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        # Optional client attribution — populated lazily via set_mcp_client().
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.timeout,
            headers=headers,
        )

    def set_mcp_client(self, name: str | None, version: str | None) -> None:
        """Attach X-MCP-Client header sourced from the MCP `clientInfo` block."""
        if not name:
            return
        tag = f"{name}/{version}" if version else name
        self._client.headers["X-MCP-Client"] = tag

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HptSuClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---------- raw plumbing ----------

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        resp = await self._client.get(path, params=clean)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HptSuApiError(resp.status_code, str(detail))
        return resp.json()

    async def _post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        resp = await self._client.post(path, json=json or {})
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HptSuApiError(resp.status_code, str(detail))
        return resp.json()

    # ---------- public endpoints (live) ----------

    async def list_documents(self, **filters: Any) -> dict[str, Any]:
        """Global cross-kind document search — `GET /docs/`."""
        return await self._get("/docs/", params=filters)

    async def get_document(self, doc_id: str) -> dict[str, Any]:
        """Document by UUID (number_code) — `GET /docs/{id}/`."""
        return await self._get(f"/docs/{doc_id}/")

    async def list_certificates(self, **filters: Any) -> dict[str, Any]:
        """Conformity certificates — `GET /docs/cert/`."""
        return await self._get("/docs/cert/", params=filters)

    async def list_declarations(self, **filters: Any) -> dict[str, Any]:
        """Declarations of conformity — `GET /docs/decl/`."""
        return await self._get("/docs/decl/", params=filters)

    async def list_by_kind(self, kind: str, **filters: Any) -> dict[str, Any]:
        """Per-kind list — `GET /docs/{kind}/`."""
        return await self._get(f"/docs/{kind}/", params=filters)

    # ---------- planned endpoints (require P1 work on hpt_su) ----------

    async def search_by_vin(self, vin: str, **filters: Any) -> dict[str, Any]:
        """Aggregated VIN search across ОТТС / СБКТС / ЗОЕТС — `GET /docs/by_vin/{vin}/`.

        Not yet live (see docs/integration-hpt-su.md §5). The server will 404
        until upstream lands.
        """
        return await self._get(f"/docs/by_vin/{vin}/", params=filters)

    async def fulltext_search(self, q: str, **filters: Any) -> dict[str, Any]:
        """Full-text search inside PDF bodies — `GET /docs/fulltext/?q=...`.

        Requires an active `use_fulltext` feature on the calling key
        (premium). Not yet live — see docs/integration-hpt-su.md §6.
        """
        return await self._get("/docs/fulltext/", params={"q": q, **filters})

    async def download_document_file(self, doc_id: str, file_id: str | None = None) -> dict[str, Any]:
        """Issue a signed download URL — `POST /docs/{uuid}/download/`.

        Requires either an active subscription covering the document's kind
        or a stand-alone DOC_PURCHASE. Not yet live — see
        docs/integration-hpt-su.md §9.
        """
        payload: dict[str, Any] = {}
        if file_id:
            payload["file_id"] = file_id
        return await self._post(f"/docs/{doc_id}/download/", json=payload)

    # ---------- reference / NSI dictionaries ----------

    async def list_brands(self, **filters: Any) -> dict[str, Any]:
        """Vehicle brands — `GET /nsi/brands/` (planned, P1 §7)."""
        return await self._get("/nsi/brands/", params=filters)

    async def list_vehicle_models(self, **filters: Any) -> dict[str, Any]:
        """Vehicle models — `GET /nsi/vehicle-models/` (planned, P1 §7)."""
        return await self._get("/nsi/vehicle-models/", params=filters)

    async def list_test_labs(self, **filters: Any) -> dict[str, Any]:
        """Accredited testing laboratories — `GET /nsi/test-labs/` (planned)."""
        return await self._get("/nsi/test-labs/", params=filters)

    async def list_certification_bodies(self, **filters: Any) -> dict[str, Any]:
        """Certification bodies — `GET /nsi/certification-bodies/` (planned)."""
        return await self._get("/nsi/certification-bodies/", params=filters)

    async def list_tnved_codes(self, **filters: Any) -> dict[str, Any]:
        """TN VED EAEU classifier — `GET /nsi/tnved/` (planned).

        Only relevant for certificates and declarations; not linked to
        vehicle type-approvals.
        """
        return await self._get("/nsi/tnved/", params=filters)

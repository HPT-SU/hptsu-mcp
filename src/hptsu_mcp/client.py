"""HTTP client for hpt.su public API (`/api/v1/`).

Wraps the DRF endpoints documented at https://hpt.su/api/v1/docs/ (OpenAPI
schema at /api/v1/schema/). All endpoints are read-only ViewSets that require
Bearer ApiKey auth backed by an active API_TIER subscription, **or** a free
MCP scope key obtained on https://hpt.su/user/mcp/.

Some endpoints below are not yet live in hpt_su — see
`docs/integration-hpt-su.md` (P1 §5-9). The MCP-server returns a polite
"not yet available" error if the upstream answers 404 / 501.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from . import __version__
from .config import Settings

logger = logging.getLogger(__name__)


# HIGH#216 path traversal: httpx.URL нормализует `../`, поэтому slug='../../admin'
# в `f"/docs/{kind}/{slug}/"` превращался в /api/v1/admin/. Реальные slug'и
# документов кириллические и содержат точки (`тс-ru-e-ru.нв23.00256`), поэтому
# charset совпадает с эталоном `tech_docs.urls.kinds` (`[a-zA-Zа-яА-Я0-9.-]`):
# буквы латиницы/кириллицы (через `\w`, Unicode по умолчанию), цифры, `.`, `-`,
# плюс `_` для file_uid. `/` в набор не входит → traversal невозможен; отдельно
# запрещаем `..` (схлопывание сегмента). kind — из закрытого списка.
_SAFE_PATH_RE = re.compile(r'^[\w.-]+$')
# Recheck#blocker1 (search_by_vin path-traversal): VIN — только буквы и цифры,
# разрешать точки/дефисы здесь нельзя, иначе `vin="1/../admin/"` (len=11) пройдёт
# по серверной длинной-проверке 5..17 и httpx нормализует `../` в путь.
_SAFE_VIN_RE = re.compile(r'^[A-Za-z0-9]+$')
_ALLOWED_KINDS = frozenset({
    'otts', 'otch', 'zotts', 'zotch', 'zoets', 'sbkts', 'sout', 'cert', 'decl',
})


# ──── Проверка минимальной версии клиента ────────────────────────────────────
#
# Бэкенд hpt.su отдаёт в каждом ответе /api/v1/* заголовок с минимальной
# рекомендуемой версией hptsu-mcp (см. hpt_mcp.middleware). Если наша версия
# ниже — предупреждаем: в stdio-режиме одноразовой припиской к ответу
# ближайшего тула (server._format), в hosted — только в лог (пользователь
# hosted-инстанс не обновляет).

MIN_VERSION_HEADER = 'x-hptsu-mcp-min-version'

_UPDATE_NOTICE: str | None = None


def consume_update_notice() -> str | None:
    """Забрать одноразовое предупреждение об устаревшем клиенте (или None)."""
    global _UPDATE_NOTICE
    notice, _UPDATE_NOTICE = _UPDATE_NOTICE, None
    return notice


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = re.findall(r'\d+', v)
    if not parts:
        raise ValueError(f'unparsable version: {v!r}')
    return tuple(int(p) for p in parts[:3])


def _safe(part: str, label: str) -> str:
    if not part or '..' in part or not _SAFE_PATH_RE.match(part):
        raise ValueError(
            f'{label} contains forbidden characters '
            f'(allowed: letters, digits and ._- ; no "..").',
        )
    return part


def _safe_kind(kind: str) -> str:
    if kind not in _ALLOWED_KINDS:
        raise ValueError(
            f'Unknown kind {kind!r}. Allowed: {sorted(_ALLOWED_KINDS)}.',
        )
    return kind


def _safe_vin(vin: str) -> str:
    if not vin or not _SAFE_VIN_RE.match(vin):
        raise ValueError(
            'vin contains forbidden characters (allowed: [A-Za-z0-9]+).',
        )
    return vin


class RefResolution:
    """Итог резолва человекочитаемого имени в id справочной сущности.

    kind-фильтры документов ссылаются на справочники по внутреннему pk
    (`issuer`, `brand`, `eco_class`, …), а LLM/пользователь знает только имя.
    `resolve_ref` бьёт по `/nsi/`-эндпоинту и возвращает:

    * ``id`` заполнен → однозначное совпадение (готово к подстановке в фильтр);
    * ``candidates`` непусто → неоднозначно (имя совпало с несколькими) —
      single-pk фильтр несколько id не примет, поэтому выбор за клиентом;
    * оба пусты → не найдено.
    """

    __slots__ = ('id', 'candidates', 'query', 'truncated')

    def __init__(self, *, id=None, candidates=None, query='', truncated=False):
        self.id = id
        self.candidates = candidates or []
        self.query = query
        self.truncated = truncated

    @property
    def found(self) -> bool:
        return self.id is not None

    @property
    def ambiguous(self) -> bool:
        return self.id is None and bool(self.candidates)


class HptSuApiError(RuntimeError):
    """Raised when the hpt.su API returns a non-2xx response.

    Поддерживает canonical envelope формат `{code, message, upgrade_url,
    retry_after, fields}` (Recheck-H5). Старый формат `{detail}` тоже
    парсится — backward compat на время миграции.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str | None = None,
        upgrade_url: str | None = None,
        retry_after: int | None = None,
        available_filters: list[str] | None = None,
    ) -> None:
        super().__init__(f"hpt.su API {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        # Canonical envelope-поля (могут быть None если бэк не вернул envelope).
        self.code = code
        self.upgrade_url = upgrade_url
        self.retry_after = retry_after
        # code=NO_FILTERS (autocomplete-контракт) кладёт рядом список
        # доступных фильтров — пробрасываем до LLM-сообщения.
        self.available_filters = available_filters


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
        # LOW#621: SecretStr.get_secret_value() — иначе при logging settings объект
        # отдаёт SecretStr('**********'), а тут нужен голый строковой токен.
        self._default_token = (
            settings.api_key.get_secret_value() if settings.api_key else None
        )
        self._mcp_client_tag: str | None = None
        self._min_version_checked = False
        # Кэш резолвов имя→id справочников. Справочные данные user-independent
        # (публичны), поэтому кэшируем на весь процесс; сброс при переполнении.
        self._ref_cache: dict[tuple[str, str, str], RefResolution] = {}
        # LOW#617: бесконечный пул keep-alive утечёт коннекты в hosted-режиме
        # при ALB-style клиентах, бьющих по mcp.hpt.su с одного IP.
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.timeout,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30.0,
            ),
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

    def _note_min_version(self, resp: httpx.Response) -> None:
        """Сверить свою версию с минимальной, объявленной бэкендом.

        Проверка одна на процесс (после первого ответа с заголовком).
        Ответ без заголовка не финализирует проверку: до апгрейда бэкенда
        заголовка нет вовсе, а после — он есть на каждом ответе.
        """
        if self._min_version_checked:
            return
        raw = resp.headers.get(MIN_VERSION_HEADER, '').strip()
        if not raw:
            return
        self._min_version_checked = True
        try:
            outdated = _version_tuple(__version__) < _version_tuple(raw)
        except ValueError:
            logger.warning('Неразборчивый %s: %r', MIN_VERSION_HEADER, raw)
            return
        if not outdated:
            return
        msg = (
            f"⚠️ hptsu-mcp {__version__} is older than the minimum version "
            f"{raw} recommended by the hpt.su API — some tools may return "
            f"incomplete or malformed results. Please update: "
            f"`pip install -U hptsu-mcp` (uvx picks up the latest release "
            f"automatically on next start)."
        )
        logger.warning(msg)
        if os.getenv('HPTSU_TRANSPORT', 'stdio').lower() == 'stdio':
            global _UPDATE_NOTICE
            _UPDATE_NOTICE = msg

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """Поднять HptSuApiError из envelope или legacy-detail."""
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        # Canonical envelope (Recheck-H5): {code, message, upgrade_url?, retry_after?}.
        if isinstance(body, dict) and 'code' in body and 'message' in body:
            filters = body.get('available_filters')
            raise HptSuApiError(
                resp.status_code,
                str(body.get('message', '')),
                code=str(body['code']),
                upgrade_url=body.get('upgrade_url'),
                retry_after=body.get('retry_after'),
                available_filters=(
                    [str(f) for f in filters] if isinstance(filters, list) else None
                ),
            )
        # Legacy DRF-format {detail: '...'}.
        if isinstance(body, dict) and 'detail' in body:
            raise HptSuApiError(resp.status_code, str(body['detail']))
        raise HptSuApiError(resp.status_code, str(body))

    async def _get(self, path: str, *, params: dict[str, Any] | None = None,
                   token: str | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        resp = await self._client.get(path, params=clean, headers=self._build_headers(token))
        self._note_min_version(resp)
        self._raise_for_status(resp)
        return resp.json()

    async def _post(self, path: str, *, json: dict[str, Any] | None = None,
                    token: str | None = None) -> Any:
        resp = await self._client.post(path, json=json or {},
                                       headers=self._build_headers(token))
        self._note_min_version(resp)
        self._raise_for_status(resp)
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

    async def get_document(self, slug: str, kind: str, *,
                           token: str | None = None) -> dict[str, Any]:
        """Document by slug — `GET /docs/{kind}/{slug}/`.

        Slug — URL-form of document number (тот же что в site URL).
        `kind` обязателен: slug не уникален между kinds (например, два
        сертификата + одна декларация могут иметь одинаковый «numeric» slug).
        """
        return await self._get(
            f"/docs/{_safe_kind(kind)}/{_safe(slug, 'slug')}/", token=token,
        )

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
        return await self._get(f"/docs/{_safe_kind(kind)}/", params=filters, token=token)

    # ---------- planned endpoints (require P1 work on hpt_su) ----------

    async def search_by_vin(self, vin: str, **filters: Any) -> dict[str, Any]:
        """Aggregated VIN search across ОТТС / СБКТС / ЗОЕТС — `GET /docs/by_vin/{vin}/`.

        Not yet live (see docs/integration-hpt-su.md §5). The server will 404
        until upstream lands.
        """
        token = filters.pop("token", None)
        return await self._get(f"/docs/by_vin/{_safe_vin(vin)}/", params=filters, token=token)

    async def fulltext_search(self, q: str, **filters: Any) -> dict[str, Any]:
        """Full-text search inside PDF bodies — `GET /docs/fulltext/?q=...`.

        Requires an active `use_fulltext` feature on the calling key
        (premium). Not yet live — see docs/integration-hpt-su.md §6.
        """
        token = filters.pop("token", None)
        return await self._get("/docs/fulltext/", params={"q": q, **filters}, token=token)

    async def list_document_files(self, document_slug: str, kind: str,
                                  *, token: str | None = None) -> list[dict[str, Any]]:
        """Список файлов документа — `GET /docs/{kind}/{document_slug}/files/`.

        Используется для resolve Document slug → набор DocumentFile UIDs
        перед скачиванием через `download_document_file(file_uid)`.

        `kind` обязателен: slug не уникален между kinds (тот же no_ws_slug
        может быть и у сертификата, и у декларации). См. LOW#625.
        """
        return await self._get(
            f"/docs/{_safe_kind(kind)}/{_safe(document_slug, 'document_slug')}/files/",
            token=token,
        )

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
        return await self._post(
            f"/files/{_safe(file_uid, 'file_uid')}/download/", token=token,
        )

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

    # ---------- reference name→id resolution ----------

    _REF_CACHE_MAX = 512

    async def resolve_ref(
        self,
        path: str,
        value: str,
        *,
        param: str = "name",
        token: str | None = None,
        label_keys: tuple[str, ...] = (
            "title", "name", "name_short", "name_full", "label", "code",
        ),
        max_candidates: int = 12,
    ) -> RefResolution:
        """Резолв имени справочной сущности в её id через `/nsi/`-эндпоинт.

        `path` — endpoint (`/nsi/brands/`, `/nsi/certification-bodies/`, …);
        `value` — человекочитаемое имя; `param` — имя query-фильтра поиска.
        Результат кэшируется по (path, param, value) — справочники публичны.
        """
        q = (value or "").strip()
        if not q:
            return RefResolution(query=value)
        key = (path, param, q.casefold())
        hit = self._ref_cache.get(key)
        if hit is not None:
            return hit

        data = await self._get(path, params={param: q}, token=token)
        rows = (data.get("results") if isinstance(data, dict) else None) or []
        total = data.get("count", len(rows)) if isinstance(data, dict) else len(rows)

        if total == 0 or not rows:
            res = RefResolution(query=q)
        elif total == 1 and len(rows) == 1:
            res = RefResolution(id=rows[0].get("id"), query=q)
        else:
            cands = [
                {
                    "id": r.get("id"),
                    "label": next(
                        (r[k] for k in label_keys if r.get(k)), str(r.get("id")),
                    ),
                }
                for r in rows[:max_candidates]
            ]
            res = RefResolution(
                candidates=cands, query=q,
                truncated=total > len(cands),
            )

        if len(self._ref_cache) >= self._REF_CACHE_MAX:
            self._ref_cache.clear()
        self._ref_cache[key] = res
        return res

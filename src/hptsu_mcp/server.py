"""MCP server exposing the hpt.su document registry as tools.

Source registry: https://hpt.su — Russian/EAEU vehicle compliance documents
(type-approvals ОТТС/ОТШ, safety-of-design certificates СБКТС, conformity
certificates, declarations, type-approval notifications СУТ and more).

Authentication:

* Free MCP scope key — issue at https://hpt.su/user/mcp/, 50 requests/day.
* Paid API tiers (`scope=API_TIER`, Lite/Standard/Pro at
  https://hpt.su/pricing/#api-tiers) — 500–10 000 requests/day, include
  full-text search and direct downloads.

The server speaks Model Context Protocol over stdio by default — point any
MCP-aware client (Claude Desktop, Cursor, Cline, Continue, Goose, Cherry,
5ire, LM Studio, …) at the ``hptsu-mcp`` executable. A streamable HTTP
transport mode is provided via ``HPTSU_TRANSPORT=http`` for hosted setups
(e.g. ``https://mcp.hpt.su``).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Literal
from urllib.parse import urlparse

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import __version__
from .client import HptSuApiError, HptSuClient, consume_update_notice
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


# ──── OAuth resource-server gate (streamable-http / sse hosted mode) ──────
#
# См. docs/ai/plans/mcp_oauth_chatgpt.md (репо hpt_su), фазы 1-2. Задача
# этого блока — закрыть discovery- и валидационные блокеры для OAuth-
# коннекторов (ChatGPT и т.п.), не трогая существующий X-API-Key путь:
#
#   (а) RFC9728 `/.well-known/oauth-protected-resource/mcp` с
#       resource=<этот сервер>, authorization_servers=[hpt.su] — байт-в-байт
#       совпадающий со своим же issuer (см. `_protected_resource_metadata`,
#       SDK-роут даёт "https://hpt.su/" с хвостовым слешем, ломая клиентов,
#       сверяющих issuer буквально по RFC 8414/9728);
#   (б) 401 + `WWW-Authenticate: Bearer resource_metadata="..."` на /mcp,
#       если нет ни Bearer, ни X-API-Key;
#   (в) настоящая проверка Bearer-токена через `GET {base_url}/mcp/whoami/`
#       на hpt.su (см. `HptSuTokenVerifier`) — невалидный/просроченный/
#       отозванный токен теперь получает транспортный 401 (SDK сам шлёт
#       WWW-Authenticate), а не 200 с текстом ошибки внутри JSON-RPC.
#
# Используем штатный OAuth-RS механизм SDK (`AuthSettings` + `TokenVerifier`,
# см. mcp.server.auth) вместо ручных well-known/401 хендлеров для (б);
# (а) переопределяем собственным роутом поверх того же места, см.
# `_protected_resource_route` и `_build_http_app`.
#
# DOT-токены (django-oauth-toolkit) непрозрачны для этого сервиса — полная
# интроспекция RFC7662 не сделана; вместо неё бэк даёт лёгкий эндпоинт
# `/mcp/whoami/`, который `HptSuTokenVerifier` дёргает по каждому новому
# (некэшированному) Bearer-токену. При недоступности бэка (5xx/timeout/
# connection-error/битый JSON) — fail-OPEN на "слепой" AccessToken: реальная
# проверка всё равно происходит на hpt.su при проксировании самого запроса,
# а рестарт/деградация Django не должны рвать все активные MCP-сессии.
_X_API_KEY_BYPASS_TOKEN = "hptsu-x-api-key-bypass"  # nosec: не секрет, просто маркер

# Взводится `_XApiKeyBypassMiddleware` на время обработки запроса, для
# которого сентинел подставлен ей самой (см. её докстринг). `HptSuTokenVerifier`
# принимает сентинел только пока этот флаг взведён — иначе клиент, приславший
# сентинел сам (без реального X-API-Key), получил бы дармовой доступ.
_SENTINEL_INJECTED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hptsu_sentinel_injected", default=False,
)

_WHOAMI_PATH = "/mcp/whoami/"
_WHOAMI_TIMEOUT = 2.5  # секунды
_WHOAMI_CONCURRENCY = 20
_CACHE_MAX_ENTRIES = 10_000
_TTL_POSITIVE = 60.0  # секунды; но не дольше `exp` токена
_TTL_NEGATIVE = 10.0
_TTL_FAIL_OPEN = 5.0


def _positive_cache_ttl(exp: int | None) -> float:
    """TTL кэша для валидного токена — 60с, но не дольше его `exp`."""
    if exp is None:
        return _TTL_POSITIVE
    return max(0.0, min(_TTL_POSITIVE, exp - time.time()))


def _canonical_resource(url: str) -> str:
    """Канонизировать resource-URL для сравнения `aud` с `HPTSU_RESOURCE_URL` (RFC 8707).

    Схема и хост — в нижний регистр, дефолтный порт (`:443` для https,
    `:80` для http) и хвостовой `/` убираются: `https://MCP.hpt.su:443/mcp/`
    и `https://mcp.hpt.su/mcp` — один и тот же resource, а сверка строкой
    "как есть" (без канонизации) ложно отклоняла бы валидный токен из-за
    несущественной разницы в записи URL.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


class HptSuTokenVerifier(TokenVerifier):
    """Проверяет Bearer через `GET {base_url}/mcp/whoami/` на hpt.su.

    Контракт бэкенда (hpt_mcp): 200 `{"active": true, "exp": <unix>,
    "aud": "<resource>"|null, "scope": "mcp"}` или 401. См. блок-комментарий
    выше про fail-open и кэш. `aud`, если задан, обязан совпадать (после
    канонизации, см. `_canonical_resource`) с resource этого сервера
    (`HPTSU_RESOURCE_URL`) — иначе токен выпущен для другого ресурса
    (RFC 8707) и отклоняется.

    Экземпляр — модульный синглтон (см. `_build_auth`), живёт весь процесс:
    `self._client`/`self._cache` не закрываются и не чистятся явно на
    остановке. Это ок для текущего деплоя (один uvicorn-процесс на
    контейнер, см. Dockerfile/compose) — ОС освобождает сокеты при выходе
    процесса. Если когда-нибудь перейти на `uvicorn --workers N>1`: важно,
    каким механизмом создаются воркеры. `_TOKEN_VERIFIER` строится один раз
    при импорте модуля (см. низ файла) — если воркеры создаются через
    `fork()` уже после этого импорта (а не через повторный импорт в новом
    процессе), унаследованный `httpx.AsyncClient` (с его event-loop-
    привязанными транспортами/локами) не переживёт fork безопасно; в этом
    случае verifier нужно будет строить лениво (per-worker), а не на
    импорте.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] | None = None,
        cache_max_entries: int = _CACHE_MAX_ENTRIES,
    ) -> None:
        self._resource = _canonical_resource(settings.resource_url) if settings.resource_url else None
        self._clock = clock or time.monotonic
        self._cache_max_entries = cache_max_entries
        self._sem = asyncio.Semaphore(_WHOAMI_CONCURRENCY)
        # sha256(token) → (AccessToken | None, valid_until). None = "невалиден"
        # (негативный кэш), не отсутствие записи.
        self._cache: dict[str, tuple[AccessToken | None, float]] = {}
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=_WHOAMI_TIMEOUT,
            headers={
                "Accept": "application/json",
                # Тот же User-Agent, что у HptSuClient (см. client.py), плюс
                # пометка — на бэке в логах/аналитике эти запросы отличимы
                # от обычных API-вызовов от имени этого же MCP-сервера.
                "User-Agent": f"{settings.user_agent} token-verifier",
            },
            limits=httpx.Limits(
                max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0,
            ),
        )

    async def aclose(self) -> None:
        """Явно закрыть httpx-клиент — сегодня не вызывается (см. докстринг
        класса про процессную модель), оставлено для будущего graceful-
        shutdown хука или тестов.
        """
        await self._client.aclose()

    def _cache_get(self, key: str) -> tuple[bool, AccessToken | None]:
        hit = self._cache.get(key)
        if hit is None:
            return False, None
        token, valid_until = hit
        if valid_until < self._clock():
            del self._cache[key]
            return False, None
        return True, token

    def _cache_put(self, key: str, token: AccessToken | None, ttl: float) -> None:
        if key not in self._cache and len(self._cache) >= self._cache_max_entries:
            del self._cache[next(iter(self._cache))]  # вытесняем самый старый.
        self._cache[key] = (token, self._clock() + ttl)

    @staticmethod
    def _blind(token: str, *, client_id: str) -> AccessToken:
        return AccessToken(token=token, client_id=client_id, scopes=["mcp"], expires_at=None)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None

        # X-API-Key bypass-сентинел (см. `_XApiKeyBypassMiddleware`) — не
        # ходит в сеть, не кэшируется, принимается только пока флаг взведён.
        if token == _X_API_KEY_BYPASS_TOKEN:
            if _SENTINEL_INJECTED.get():
                return self._blind(token, client_id="x-api-key")
            return None

        cache_key = hashlib.sha256(token.encode()).hexdigest()
        hit, cached = self._cache_get(cache_key)
        if hit:
            return cached

        try:
            async with self._sem:
                resp = await self._client.get(
                    _WHOAMI_PATH, headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            log.warning("whoami check errored (%s) — failing open", exc)
            blind = self._blind(token, client_id="blind-oauth")
            self._cache_put(cache_key, blind, _TTL_FAIL_OPEN)
            return blind

        if resp.status_code == 401:
            self._cache_put(cache_key, None, _TTL_NEGATIVE)
            return None

        if resp.status_code != 200:
            log.warning("whoami unexpected status %s — failing open", resp.status_code)
            blind = self._blind(token, client_id="blind-oauth")
            self._cache_put(cache_key, blind, _TTL_FAIL_OPEN)
            return blind

        try:
            data = resp.json()
        except ValueError:
            log.warning("whoami returned malformed JSON — failing open")
            blind = self._blind(token, client_id="blind-oauth")
            self._cache_put(cache_key, blind, _TTL_FAIL_OPEN)
            return blind

        if not isinstance(data, dict) or not data.get("active"):
            self._cache_put(cache_key, None, _TTL_NEGATIVE)
            return None

        aud = data.get("aud")
        if aud is not None and (self._resource is None or _canonical_resource(aud) != self._resource):
            # RFC 8707 — токен выпущен для другого resource, не для нас.
            self._cache_put(cache_key, None, _TTL_NEGATIVE)
            return None

        exp = data.get("exp")
        access = AccessToken(
            token=token, client_id="oauth", scopes=["mcp"], expires_at=exp, resource=aud,
        )
        self._cache_put(cache_key, access, _positive_cache_ttl(exp))
        return access


class _XApiKeyBypassMiddleware:
    """Пропускает `X-API-Key` мимо OAuth-гейта SDK.

    `RequireAuthMiddleware` (mcp.server.auth) требует `Authorization: Bearer`
    на любой запрос к /mcp, иначе 401 — это ломает существующих клиентов со
    статичным `X-API-Key` (кабинетный ключ). В SDK нет ручки «пропустить по
    другому заголовку», поэтому при наличии `X-API-Key` без `Authorization`
    подставляем сентинел-Bearer и взводим `_SENTINEL_INJECTED` на время
    обработки запроса — так `HptSuTokenVerifier` отличает наш сентинел от
    того же значения, присланного клиентом напрямую (клиентский вариант
    отклоняется). `_request_token()` ниже всё равно читает `X-API-Key`
    напрямую и приоритетно — сентинел никогда не долетает до upstream.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
        has_api_key = any(k.lower() == b"x-api-key" for k, _ in headers)
        has_authorization = any(k.lower() == b"authorization" for k, _ in headers)
        if has_api_key and not has_authorization:
            scope = dict(scope)
            scope["headers"] = [
                *headers,
                (b"authorization", f"Bearer {_X_API_KEY_BYPASS_TOKEN}".encode()),
            ]
            reset_token = _SENTINEL_INJECTED.set(True)
            try:
                await self.app(scope, receive, send)
            finally:
                _SENTINEL_INJECTED.reset(reset_token)
            return
        await self.app(scope, receive, send)


def _protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    """RFC9728-метаданные — тот же состав полей, что строит SDK
    (`create_protected_resource_routes`), но `authorization_servers`
    подставляем строкой без хвостового слеша.

    RFC 8414/9728 требуют байт-в-байт совпадение с issuer в AS-metadata;
    pydantic `AnyHttpUrl` молча добавляет `/` к голому хосту, поэтому
    `oauth_issuer_url="https://hpt.su"` через SDK превращался в
    `"https://hpt.su/"` — клиенты, сверяющие issuer строкой (а не как URL),
    не находят совпадения и обрывают discovery.
    """
    assert settings.resource_url, "resource_url must be set (caller-enforced, see _build_http_app)"
    meta = ProtectedResourceMetadata(
        resource=AnyHttpUrl(settings.resource_url),
        authorization_servers=[AnyHttpUrl(settings.oauth_issuer_url)],
        scopes_supported=["mcp"],
    )
    data = json.loads(meta.model_dump_json(exclude_none=True))
    data["authorization_servers"] = [settings.oauth_issuer_url.rstrip("/")]
    return data


def _protected_resource_route(settings: Settings) -> Route:
    """Собственный route на то же место, что и SDK-роут (см.
    `create_protected_resource_routes` в mcp.server.auth.routes) — тот же
    путь и та же CORS-обёртка (`cors_middleware`), но с исправленным
    `authorization_servers` (см. `_protected_resource_metadata`).
    """
    assert settings.resource_url, "resource_url must be set (caller-enforced, see _build_http_app)"
    metadata_url = build_resource_metadata_url(AnyHttpUrl(settings.resource_url))
    well_known_path = urlparse(str(metadata_url)).path
    content = _protected_resource_metadata(settings)

    async def handle(_request: Request) -> JSONResponse:
        return JSONResponse(content, headers={"Cache-Control": "public, max-age=3600"})

    return Route(
        well_known_path,
        endpoint=cors_middleware(handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )


def _install_protected_resource_route(app: Starlette, settings: Settings) -> None:
    """Заменить SDK-роут protected-resource на `_protected_resource_route`.

    FastMCP добавляет свой route до `_custom_starlette_routes` (см.
    `streamable_http_app`/`sse_app` в mcp.server.fastmcp.server) — тот же
    путь встретится дважды, если просто дописать свой в конец; поэтому
    сначала убираем совпадающий по `path` SDK-роут.
    """
    route = _protected_resource_route(settings)
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != route.path]
    app.router.routes.append(route)


def _build_auth() -> tuple[AuthSettings | None, TokenVerifier | None]:
    """RS-only OAuth конфигурация из `HPTSU_RESOURCE_URL` (см. config.py).

    Пусто (дефолт, локальная разработка/тесты/stdio) → OAuth-гейт выключен
    полностью, поведение не меняется относительно того, что было до Ф1.
    """
    settings = load_settings()
    if not settings.resource_url:
        return None, None
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(settings.oauth_issuer_url),
        resource_server_url=AnyHttpUrl(settings.resource_url),
        required_scopes=["mcp"],
    )
    return auth, HptSuTokenVerifier(settings)


_AUTH_SETTINGS, _TOKEN_VERIFIER = _build_auth()


mcp = FastMCP(
    "hpt-su",
    instructions=(
        "Search the hpt.su registry of Russian / EAEU vehicle compliance "
        "documents (https://hpt.su): type approvals (ОТТС/СБКТС/ОТШ), "
        "conformity certificates (ТР ТС/ТР ЕАЭС), declarations of conformity, "
        "type-approval notifications СУТ. Read-only. Free MCP key: 50 "
        "requests/day, issued at https://hpt.su/user/mcp/. Paid API tiers "
        "(Lite/Standard/Pro, 500–10 000 requests/day, "
        "https://hpt.su/pricing/#api-tiers) add full-text search and direct "
        "file downloads."
    ),
    lifespan=_lifespan,
    # Read-only proxy без состояния между запросами. Stateful (дефолт) копил
    # сессию на каждого подключившегося в StreamableHTTPSessionManager и не
    # чистил при обрыве → heap-утечка (за ~1.5д публичного трафика дополз до
    # 96% от 512M). Stateless: каждый запрос независим, сессии не накапливаются.
    stateless_http=True,
    auth=_AUTH_SETTINGS,
    token_verifier=_TOKEN_VERIFIER,
)

# FastMCP не пробрасывает `version` в низкоуровневый `mcp.server.lowlevel.Server`
# (нет такого параметра конструктора) — тот при пустом `self.version` берёт
# версию SDK-пакета `mcp` (`importlib.metadata.version("mcp")`), и клиент видит
# в ответе `initialize` не hptsu-mcp 0.x, а mcp-SDK 1.x. Ставим напрямую в то
# же поле, которое читает `Server.create_initialization_options()`.
mcp._mcp_server.version = __version__


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
    # Recheck-MED: cap на размер header'а — защита от DoS гигантским
    # Authorization (8KB hard-limit на header line в большинстве серверов,
    # но FastAPI/starlette могут принять больше через max_headers_size).
    direct = headers.get("x-api-key")
    if direct:
        if len(direct) > 4096:
            return None
        return direct
    auth = headers.get("authorization", "")
    if len(auth) > 4096:
        return None
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        return f"BEARER {token}" if token else None
    return None


def _format(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    # Одноразовое предупреждение об устаревшем клиенте (stdio-режим) —
    # см. client._note_min_version. None во всех остальных случаях.
    notice = consume_update_notice()
    if notice:
        return f"{notice}\n\n{text}"
    return text


def _prune_specs(data: Any) -> Any:
    """`specs` (detail СБКТС/ЗОЕТС) — выбрасывает null и пустые группы перед
    отдачей LLM. API возвращает стабильную схему с явными `null` (нужна для
    OpenAPI), но для контекста модели это шум — см.
    docs/ai/plans/sbkts_zoets_api_specs.md, п.8."""
    if not isinstance(data, dict):
        return data
    specs = data.get("specs")
    if not isinstance(specs, dict):
        return data

    pruned: dict[str, Any] = {}
    for group_key, group_val in specs.items():
        if isinstance(group_val, dict):
            inner = {k: v for k, v in group_val.items() if v not in (None, [], {})}
            if inner:
                pruned[group_key] = inner
        elif group_val not in (None, [], {}):
            pruned[group_key] = group_val

    return {**data, "specs": pruned or None}


# kind → NSI-эндпоинт для резолва issuer (орган сертификации vs лаборатория).
# otts/otch/zotts/zotch выдают органы (CERT_BODY); sbkts/zoets — лаборатории.
_ISSUER_ENDPOINT: dict[str, str] = {
    "otts": "/nsi/certification-bodies/",
    "otch": "/nsi/certification-bodies/",
    "zotts": "/nsi/certification-bodies/",
    "zotch": "/nsi/certification-bodies/",
    "sbkts": "/nsi/test-labs/",
    "zoets": "/nsi/test-labs/",
}


async def _resolve_or_message(
    client: HptSuClient,
    path: str,
    value: Any,
    *,
    token: str | None,
    label: str,
    param: str = "name",
    numeric_is_id: bool = True,
) -> tuple[str | None, str | None]:
    """Резолв «имя справочника → id» для подстановки в kind-фильтр.

    Возвращает ``(id_str, None)`` при успехе или ``(None, message)`` — текст
    для LLM, если резолв не удался.

    ``numeric_is_id`` — при True числовое значение считаем уже готовым pk
    (issuer/brand: клиент вправе передать id напрямую). При False цифра —
    это семантическое значение, а не id (eco_class='5', axis_count=2), и его
    всё равно надо резолвить через справочник по ``param``.
    """
    s = str(value).strip()
    if not s:
        return None, None
    if numeric_is_id and s.isdigit():
        return s, None
    res = await client.resolve_ref(path, s, param=param, token=token)
    if res.found:
        return str(res.id), None
    if res.ambiguous:
        opts = "; ".join(f"{c['label']} (id={c['id']})" for c in res.candidates)
        more = " …and more" if res.truncated else ""
        return None, (
            f"{label} '{s}' is ambiguous — several matches{more}: {opts}. "
            f"Retry with a more exact name or pass the numeric id."
        )
    return None, (
        f"{label} '{s}' not found in the reference book — check spelling or "
        f"look it up via the matching list_* tool, then pass its id."
    )


def _err(exc: HptSuApiError) -> str:
    """Преобразует upstream-ошибку в LLM-сообщение.

    Recheck-H5: бэк возвращает envelope `{code, message, upgrade_url?,
    retry_after?}` — используем `code` для логики, `message` показываем
    юзеру (он лаконичный, без stack-trace/PII).

    Backward compat: для legacy-detail (старые версии бэка) используем
    status_code → дефолт.
    """
    log.warning(
        "hpt.su upstream error %s code=%s detail=%s",
        exc.status_code, exc.code, exc.detail,
    )

    # Envelope code-based ветви — самые точные сообщения.
    if exc.code == 'PAYMENT_REQUIRED':
        url = exc.upgrade_url or 'https://hpt.su/pricing/'
        return f"Payment required. {exc.detail} Upgrade at {url}."
    if exc.code == 'RATE_LIMITED':
        wait = f' Retry after {exc.retry_after}s.' if exc.retry_after else ''
        return f"Rate limit reached.{wait} Upgrade at https://hpt.su/pricing/ for higher limits."
    if exc.code == 'NOT_FOUND':
        return "Not found."
    if exc.code == 'UNAUTHORIZED':
        return "Authentication failed — check your API key."
    if exc.code == 'FORBIDDEN':
        return f"Forbidden: {exc.detail}"
    if exc.code == 'INVALID_INPUT':
        return f"Invalid input: {exc.detail}"
    if exc.code == 'NO_FILTERS':
        # Autocomplete-контракт: list-эндпоинты отдают данные только при
        # заданном content-фильтре. Подсказываем LLM повторить вызов с фильтром.
        hint = f" Available filters: {', '.join(exc.available_filters)}." \
            if exc.available_filters else ""
        return (
            f"No filters given — this endpoint returns data only when at "
            f"least one search filter is set (page/page_size don't count). "
            f"Retry with a filter.{hint}"
        )

    # Fallback на status_code (legacy bridge для бэка до H5).
    if exc.status_code == 402:
        return (
            "Payment required. This tool needs a paid API tier — see "
            "https://hpt.su/pricing/#api-tiers."
        )
    if exc.status_code == 429:
        return (
            "Rate limit reached. The free MCP key allows 50 requests/day; "
            "paid API tiers allow 500–10 000 — see https://hpt.su/pricing/#api-tiers."
        )
    if exc.status_code == 404:
        return "Not found."
    if exc.status_code >= 500:
        return f"hpt.su upstream is unavailable (HTTP {exc.status_code}). Try again later."
    return f"hpt.su API error {exc.status_code}."


# ──── Search & retrieval ────────────────────────────────────────────────


@mcp.tool()
async def search_documents(
    ctx: Context,
    number: str = Field(description="Document number, full or partial."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search documents of every registry kind by number (full or partial).

    Use it when the registry kind is unknown. Each result carries `slug` and
    `kind` — pass them to get_document or list_document_files.
    """
    client = _get_client(ctx)
    token = _request_token(ctx)
    try:
        data = await client.list_documents(number=number, page=page, page_size=page_size, token=token)
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc
    return _format(data)


@mcp.tool()
async def get_document(
    ctx: Context,
    slug: str = Field(description="Document slug — URL-form of the number (the same as in site URL)."),
    kind: str = Field(
        description="One of: otts, otch, zotts, zotch, sbkts, zoets, sout, cert, decl. "
                    "Slug isn't globally unique between kinds, so kind is required.",
    ),
) -> str:
    """Fetch one document's full record by `slug` + `kind` (both taken from a
    search result). For sbkts/zoets the record includes `specs` — vehicle
    characteristics (engine, dimensions, masses, organizations, etc.)."""
    if kind not in REGISTRY_KINDS:
        raise ToolError(f"Invalid kind={kind!r}. Allowed: {sorted(REGISTRY_KINDS)}.")
    client = _get_client(ctx)
    try:
        data = await client.get_document(slug, kind, token=_request_token(ctx))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc
    return _format(_prune_specs(data))


@mcp.tool()
async def search_certificates(
    ctx: Context,
    number: str | None = Field(default=None, description="Certificate number, full or partial."),
    applicant: str | None = Field(default=None, description="Applicant name, full or partial."),
    applicant_inn: str | None = Field(default=None, description="Applicant INN (exact match)."),
    manufacturer: str | None = Field(default=None, description="Manufacturer name, full or partial."),
    regulations: str | None = Field(default=None, description="Technical regulation code (e.g. 'ТР ТС 018/2011')."),
    product: str | None = Field(default=None, description="Product full name, full or partial."),
    status: str | None = Field(default=None, description="Certificate status: ACTIVE, SUSPENDED, RESUMED, TERMINATED, CANCELLED, ARCHIVED, EXTENDED or UNKNOWN."),
    scheme: str | None = Field(default=None, description="Certification scheme — '1с'…'9с'."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search conformity certificates (ТР ТС / ТР ЕАЭС).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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
        raise ToolError(_err(exc)) from exc
    return _format(data)


@mcp.tool()
async def search_declarations(
    ctx: Context,
    number: str | None = Field(default=None, description="Declaration number, full or partial."),
    applicant: str | None = Field(default=None, description="Applicant name, full or partial."),
    applicant_inn: str | None = Field(default=None, description="Applicant INN (exact match)."),
    manufacturer: str | None = Field(default=None, description="Manufacturer name, full or partial."),
    regulations: str | None = Field(default=None, description="Technical regulation code (e.g. 'ТР ТС 018/2011')."),
    product: str | None = Field(default=None, description="Product full name, full or partial."),
    status: str | None = Field(default=None, description="Declaration status: ACTIVE, SUSPENDED, RESUMED, TERMINATED, CANCELLED, ARCHIVED, EXTENDED or UNKNOWN."),
    scheme: str | None = Field(default=None, description="Declaration scheme — '1д'…'6д'."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search declarations of conformity (ТР ТС / ТР ЕАЭС).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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
        raise ToolError(_err(exc)) from exc
    return _format(data)


async def _search_kind(ctx: Context, kind: str, *, page: int, page_size: int, **kw) -> str:
    """Shared body for per-kind search tools."""
    client = _get_client(ctx)
    token = _request_token(ctx)
    try:
        # Авто-резолв ссылочных фильтров: kind-фильтры ждут pk справочной
        # сущности, а LLM передаёт человекочитаемое значение. Резолвим каждое
        # через свой /nsi/-справочник; при неоднозначности вернём кандидатов.
        issuer = kw.get("issuer")
        if issuer is not None and kind in _ISSUER_ENDPOINT:
            rid, msg = await _resolve_or_message(
                client, _ISSUER_ENDPOINT[kind], issuer, token=token, label="issuer",
            )
            if msg:
                raise ToolError(msg)
            kw["issuer"] = rid

        eco = kw.get("eco_class")
        if eco is not None:
            rid, msg = await _resolve_or_message(
                client, "/nsi/eco-classes/", eco, token=token,
                label="eco_class", numeric_is_id=False,
            )
            if msg:
                raise ToolError(msg)
            kw["eco_class"] = rid

        wf = kw.get("wheel_formula")
        if wf is not None:
            rid, msg = await _resolve_or_message(
                client, "/nsi/wheel-formulas/", wf, token=token,
                label="wheel_formula", numeric_is_id=False,
            )
            if msg:
                raise ToolError(msg)
            kw["wheel_formula"] = rid

        ax = kw.get("axis_count")
        if ax is not None:
            # «2» → по числу осей (может быть неоднозначно); «2 / 4» → по имени.
            ax_s = str(ax).strip()
            ax_param = "axis_count" if ax_s.isdigit() else "name"
            rid, msg = await _resolve_or_message(
                client, "/nsi/axis-counts/", ax_s, token=token,
                label="axis_count", param=ax_param, numeric_is_id=False,
            )
            if msg:
                raise ToolError(msg)
            kw["axis_count"] = rid

        data = await client.list_by_kind(
            kind, page=page, page_size=page_size,
            token=token,
            **{k: v for k, v in kw.items() if v is not None},
        )
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc
    return _format(data)


@mcp.tool()
async def search_otts(
    ctx: Context,
    number: str | None = Field(default=None, description="ОТТС number, full or partial."),
    vin: str | None = Field(default=None, description="VIN, full or partial (5-17 characters)."),
    brand: str | None = Field(default=None, description="Vehicle brand (e.g. 'Toyota')."),
    type: str | None = Field(default=None, description="Vehicle type / model, full or partial."),
    comm_name: str | None = Field(default=None, description="Commercial name, full or partial."),
    chassis: str | None = Field(default=None, description="Chassis identifier, full or partial."),
    mods: str | None = Field(default=None, description="Modifications, full or partial."),
    category: str | None = Field(default=None, description="Vehicle category (M1, N2, L3, …)."),
    eco_class: str | None = Field(default=None, description="Ecological class — pass '5' or the name; auto-resolved to reference id."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (e.g. '4x2'); auto-resolved to reference id."),
    axis_count: str | None = Field(default=None, description="Axle count (e.g. '2') or exact 'axes / wheels' (e.g. '2 / 4') — auto-resolved to reference id. A bare axle count may match several wheel configs; then candidates are returned to pick from."),
    issuer: str | None = Field(default=None, description="Certification body name — auto-resolved to id (or pass numeric id). See list_certification_bodies."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ОТТС (vehicle type approvals, ТР ТС 018/2011).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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
    eco_class: str | None = Field(default=None, description="Ecological class — '5' or name; auto-resolved to reference id."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (e.g. '4x2'); auto-resolved to reference id."),
    axis_count: str | None = Field(default=None, description="Axle count (e.g. '2') or exact 'axes / wheels' (e.g. '2 / 4') — auto-resolved to reference id. A bare axle count may match several wheel configs; then candidates are returned to pick from."),
    issuer: str | None = Field(default=None, description="Certification body name — auto-resolved to id (or pass numeric id)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ОТШ (chassis type approvals).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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
    eco_class: str | None = Field(default=None, description="Ecological class — '5' or name; auto-resolved to reference id."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (e.g. '4x2'); auto-resolved to reference id."),
    axis_count: str | None = Field(default=None, description="Axle count (e.g. '2') or exact 'axes / wheels' (e.g. '2 / 4') — auto-resolved to reference id. A bare axle count may match several wheel configs; then candidates are returned to pick from."),
    issuer: str | None = Field(default=None, description="Certification body name — auto-resolved to id (or pass numeric id)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ЗОТТС (vehicle conformity assessment conclusion).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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
    eco_class: str | None = Field(default=None, description="Ecological class — '5' or name; auto-resolved to reference id."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (e.g. '4x2'); auto-resolved to reference id."),
    axis_count: str | None = Field(default=None, description="Axle count (e.g. '2') or exact 'axes / wheels' (e.g. '2 / 4') — auto-resolved to reference id. A bare axle count may match several wheel configs; then candidates are returned to pick from."),
    issuer: str | None = Field(default=None, description="Certification body name — auto-resolved to id (or pass numeric id)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ЗОТШ (chassis conformity assessment conclusion).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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
    engine: str | None = Field(default=None, description="ICE engine model, full or partial."),
    year: int | None = Field(default=None, description="Manufacture year (YYYY)."),
    motor: str | None = Field(default=None, description="Electric motor model, full or partial."),
    motor_power: int | None = Field(default=None, description="Motor power (kW)."),
    category: str | None = Field(default=None, description="Vehicle category."),
    eco_class: str | None = Field(default=None, description="Ecological class — '5' or name; auto-resolved to reference id."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (e.g. '4x2'); auto-resolved to reference id."),
    axis_count: str | None = Field(default=None, description="Axle count (e.g. '2') or exact 'axes / wheels' (e.g. '2 / 4') — auto-resolved to reference id. A bare axle count may match several wheel configs; then candidates are returned to pick from."),
    issuer: str | None = Field(default=None, description="Testing lab name — auto-resolved to id (or pass numeric id). See list_test_labs."),
    powertrain: str | None = Field(default=None, description="Powertrain type: 'ice' (combustion), 'hybrid', 'electric'."),
    fuel: str | None = Field(default=None, description="Engine fuel type, one of: 'бензин', 'дизель', 'газ', 'электро', 'водород'."),
    date_from: str | None = Field(default=None, description="Issue date from (YYYY-MM-DD)."),
    date_to: str | None = Field(default=None, description="Issue date to (YYYY-MM-DD)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search СБКТС (vehicle safety certificate).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
    return await _search_kind(
        ctx, "sbkts", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type, comm_name=comm_name,
        engine=engine, year=year, motor=motor, motor_power=motor_power,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
        powertrain=powertrain, fuel=fuel,
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
    eco_class: str | None = Field(default=None, description="Ecological class — '5' or name; auto-resolved to reference id."),
    wheel_formula: str | None = Field(default=None, description="Wheel formula (e.g. '4x2'); auto-resolved to reference id."),
    axis_count: str | None = Field(default=None, description="Axle count (e.g. '2') or exact 'axes / wheels' (e.g. '2 / 4') — auto-resolved to reference id. A bare axle count may match several wheel configs; then candidates are returned to pick from."),
    issuer: str | None = Field(default=None, description="Testing lab name — auto-resolved to id (or pass numeric id)."),
    powertrain: str | None = Field(default=None, description="Powertrain type: 'ice' (combustion), 'hybrid', 'electric'."),
    fuel: str | None = Field(default=None, description="Engine fuel type, one of: 'бензин', 'дизель', 'газ', 'электро', 'водород'."),
    date_from: str | None = Field(default=None, description="Issue date from (YYYY-MM-DD)."),
    date_to: str | None = Field(default=None, description="Issue date to (YYYY-MM-DD)."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Search ЗОЕТС (vehicle technical expertise conclusion).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
    return await _search_kind(
        ctx, "zoets", page=page, page_size=page_size,
        number=number, vin=vin, brand=brand, type=type, comm_name=comm_name,
        engine=engine, year=year, motor=motor, motor_power=motor_power,
        category=category, eco_class=eco_class,
        wheel_formula=wheel_formula, axis_count=axis_count, issuer=issuer,
        powertrain=powertrain, fuel=fuel,
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
    """Search СУТ (vehicle type notification — small dataset, only basic fields).

    Set at least one search filter — paging parameters alone return a NO_FILTERS error.
    """
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

    Returns documents whose VIN field contains the given fragment, newest
    first. For exact full-VIN search within a single registry use the
    per-kind tool (search_otts/search_sbkts/…) with its `vin` parameter.
    """
    if not (5 <= len(vin) <= 17):
        raise ToolError(f"Invalid VIN length: {len(vin)} chars (expected 5-17).")
    client = _get_client(ctx)
    try:
        return _format(await client.search_by_vin(vin, token=_request_token(ctx)))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


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

    Covers ОТТС / ОТШ / ЗОТТС / ЗОТШ. Other document kinds (СБКТС / ЗОЕТС /
    СУТ / cert / decl) are not covered by full-text search.

    **Premium feature** — requires a plan that includes full-text search
    (see https://hpt.su/pricing/) with a subscription covering at least
    one type-approval kind.
    """
    if kind and kind not in ("otts", "otch", "zotts", "zotch"):
        raise ToolError(f"Invalid kind={kind!r}. Allowed: otts, otch, zotts, zotch (or omit for all).")
    client = _get_client(ctx)
    try:
        data = await client.fulltext_search(query, kind=kind, page=page, page_size=page_size,
                                            token=_request_token(ctx))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc
    return _format(data)


@mcp.tool()
async def list_document_files(
    ctx: Context,
    document_slug: str = Field(description="Document slug (from search.slug field)."),
    kind: str = Field(
        description="One of: otts, otch, zotts, zotch, sbkts, zoets, sout, cert, decl. "
                    "Slug isn't globally unique between kinds, so kind is required.",
    ),
) -> str:
    """List the files attached to a document.

    Returns `[{file_uid, file_name, kind, pages_count, published_at}]` —
    each entry's `file_uid` can be passed to `download_document_file`.

    A document often has several PDFs (e.g. cleaned + original); use this
    tool to enumerate them and pick the right one before download.
    """
    if kind not in REGISTRY_KINDS:
        raise ToolError(f"Invalid kind={kind!r}. Allowed: {sorted(REGISTRY_KINDS)}.")
    client = _get_client(ctx)
    try:
        return _format(await client.list_document_files(
            document_slug, kind, token=_request_token(ctx)))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


@mcp.tool()
async def download_document_file(
    ctx: Context,
    file_uid: str = Field(description="DocumentFile UID (from list_document_files)."),
) -> str:
    """Get links for a document file: page URL for everyone, direct
    download URL when the plan allows it.

    Always returns `document_page_url` — the document's page on hpt.su
    where the user can download the file manually under their account —
    it is the user-facing link.

    When the plan includes direct downloads, the response also contains
    `download_url` — a personal temporary link to the PDF. It can be
    fetched programmatically: send a GET request with the same
    `X-API-Key: <key>` header used for this API. In a browser it works
    when signed in to hpt.su under the account that owns the key. The
    link is bound to the key owner — it does not work with anyone
    else's key or account. The download quota is charged at actual
    download time. Otherwise `download_url` is null and `download_note`
    explains why.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.download_document_file(
            file_uid, token=_request_token(ctx)))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


# ──── Reference / NSI dictionaries ──────────────────────────────────────


@mcp.tool()
async def list_brands(
    ctx: Context,
    name: str = Field(description="Brand name substring (e.g. 'KAMAZ')."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Look up vehicle brands in the reference book — canonical name and id.

    Search tools take a brand name directly; use this to find the canonical
    spelling, or the brand id for list_vehicle_models.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.list_brands(
            name=name, page=page, page_size=page_size, token=_request_token(ctx)))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


@mcp.tool()
async def list_vehicle_models(
    ctx: Context,
    brand: str | None = Field(default=None, description="Brand name (auto-resolved to id) or numeric brand id."),
    name: str | None = Field(default=None, description="Model name substring."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Resolve a vehicle model name (within a brand) to canonical record.

    Set `brand`, `name` or both — paging parameters alone return a NO_FILTERS error.
    """
    client = _get_client(ctx)
    token = _request_token(ctx)
    try:
        # brand-фильтр моделей — по id марки; принимаем и название (резолвим).
        if brand is not None:
            rid, msg = await _resolve_or_message(
                client, "/nsi/brands/", brand, token=token, label="brand",
            )
            if msg:
                raise ToolError(msg)
            brand = rid
        return _format(await client.list_vehicle_models(
            brand=brand, name=name, page=page, page_size=page_size, token=token,
        ))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


@mcp.tool()
async def list_test_labs(
    ctx: Context,
    name: str = Field(description="Lab name, full or partial."),
    page: int = Field(default=1, ge=1, description="1-based page index."),
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX, description="Rows per page (max 50)."),
) -> str:
    """Lookup accredited testing laboratories (issuers of СБКТС/ЗОЕТС)."""
    client = _get_client(ctx)
    try:
        return _format(await client.list_test_labs(
            name=name, page=page, page_size=page_size,
            token=_request_token(ctx),
        ))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


@mcp.tool()
async def list_certification_bodies(
    ctx: Context,
    name: str = Field(description="Body name substring."),
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
        raise ToolError(_err(exc)) from exc


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

    Set `prefix`, `query` or both — paging parameters alone return a NO_FILTERS error.
    """
    client = _get_client(ctx)
    try:
        return _format(await client.list_tnved_codes(
            prefix=prefix, query=query, page=page, page_size=page_size,
            token=_request_token(ctx),
        ))
    except HptSuApiError as exc:
        raise ToolError(_err(exc)) from exc


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


# MED#461: дешёвый in-process token-bucket для /readyz, чтобы внешние
# проверки не могли заставить нас бить upstream по 1k rps. Лимит ~ 30/min.
_READYZ_BUCKET = {"tokens": 30.0, "ts": 0.0}
_READYZ_RATE_PER_SEC = 0.5  # 30/min
_READYZ_CAPACITY = 30.0


def _readyz_take_token() -> bool:
    import time as _t
    now = _t.monotonic()
    if _READYZ_BUCKET["ts"] == 0.0:
        _READYZ_BUCKET["ts"] = now
    elapsed = now - _READYZ_BUCKET["ts"]
    _READYZ_BUCKET["ts"] = now
    _READYZ_BUCKET["tokens"] = min(
        _READYZ_CAPACITY,
        _READYZ_BUCKET["tokens"] + elapsed * _READYZ_RATE_PER_SEC,
    )
    if _READYZ_BUCKET["tokens"] >= 1.0:
        _READYZ_BUCKET["tokens"] -= 1.0
        return True
    return False


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
    if not _readyz_take_token():
        return JSONResponse({"status": "throttled"}, status_code=429)
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
        "Paid API tiers (Lite/Standard/Pro): 500–10 000 req/day + full-text +\n"
        "direct downloads, https://hpt.su/pricing/#api-tiers.\n\n"
        "Registries:\n"
        + "\n".join(f"  • {kind} — {title}" for kind, title in REGISTRY_KINDS.items())
        + "\n\nAPI base: https://hpt.su/api/v1/\n"
        "Auth: X-API-Key: <public_id>:<secret> (issued at /user/mcp/),\n"
        "      or OAuth2 Bearer (DCR via /.well-known/oauth-authorization-server).\n"
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


def _build_http_app(kind: Literal["streamable-http", "sse"]) -> Starlette:
    """Собрать Starlette-приложение транспорта + X-API-Key bypass поверх
    OAuth-гейта SDK (см. `_XApiKeyBypassMiddleware`), и, если задан
    `HPTSU_RESOURCE_URL`, заменить SDK-роут protected-resource на
    исправленный (см. `_install_protected_resource_route`, B1).

    Не используем `mcp.run(transport=...)` напрямую — он не отдаёт наружу
    собранное приложение, а middleware нужно добавить *до* первого запроса
    (`Starlette.add_middleware` кидает `RuntimeError` после старта).
    """
    app = mcp.streamable_http_app() if kind == "streamable-http" else mcp.sse_app()
    if mcp.settings.auth is not None:
        app.add_middleware(_XApiKeyBypassMiddleware)
        settings = load_settings()
        if settings.resource_url:
            _install_protected_resource_route(app, settings)
    return app


def _serve_http(app: Starlette) -> None:
    import asyncio

    import uvicorn

    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    asyncio.run(uvicorn.Server(config).serve())


def main() -> None:
    logging.basicConfig(
        level=os.getenv("HPTSU_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # MED#455: даже при HPTSU_LOG_LEVEL=DEBUG не светим httpx Authorization
    # в plaintext (httpx.client логирует полный request с headers на DEBUG).
    # Поднимаем уровень httpx до INFO, чтоб не показывал заголовки.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    transport = os.getenv("HPTSU_TRANSPORT", "stdio").lower()
    if transport in {"http", "streamable-http", "streamable_http"}:
        _configure_http_binding()
        _serve_http(_build_http_app("streamable-http"))
    elif transport == "sse":
        _configure_http_binding()
        _serve_http(_build_http_app("sse"))
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

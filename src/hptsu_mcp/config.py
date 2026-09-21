"""Конфиг через env. Все значения опциональны кроме API-ключа для prod-режима."""
from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__ as _PKG_VERSION


class Settings(BaseSettings):
    """Настройки MCP-сервера.

    Все переменные читаются из окружения с префиксом ``HPTSU_``.
    Пример: ``HPTSU_API_KEY=...``, ``HPTSU_BASE_URL=https://hpt.su/api/v1``.
    """

    model_config = SettingsConfigDict(
        env_prefix="HPTSU_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LOW#621: SecretStr — иначе __repr__/__str__/JSON-dump утекают токен
    # в логи / sentry / отчёты об ошибках. Использовать через .get_secret_value().
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "API key from hpt.su cabinet (https://hpt.su/user/mcp/). "
            "Format: `<public_id>:<secret>`. Required for stdio transport; "
            "in streamable-http mode the key is extracted from the incoming "
            "Authorization header per-request."
        ),
    )
    base_url: str = Field(
        default="https://hpt.su/api/v1",
        description="Base URL of the hpt.su public REST API.",
    )
    timeout: float = Field(
        default=30.0,
        description="HTTP timeout (seconds) for every API call.",
    )
    user_agent: str = Field(
        # Recheck-LOW: подтягиваем из __version__ — раньше отставало (0.1
        # при пакетной версии 0.2), upstream-аналитика видела ложные следы.
        default=f"hptsu-mcp/{_PKG_VERSION}",
        description="User-Agent header for outgoing API calls.",
    )

    # ──── HTTP-transport server binding (streamable-http / sse only) ─────
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for HTTP transports. Set to `0.0.0.0` for "
            "production hosted deploys (mcp.hpt.su)."
        ),
    )
    port: int = Field(
        default=8000,
        description="Port for HTTP transports.",
    )
    allowed_hosts: str = Field(
        default="",
        description=(
            "Comma-separated list of allowed `Host:` header values "
            "(DNS-rebinding protection). Empty = FastMCP default "
            "(localhost-only). For production set "
            "`mcp.hpt.su,mcp.hpt.su:*`."
        ),
    )

    # ──── OAuth resource-server gate (streamable-http / sse only) ────────
    # См. docs/ai/plans/mcp_oauth_chatgpt.md (hpt_su repo) — Ф1.
    resource_url: str | None = Field(
        default=None,
        description=(
            "Canonical resource identifier of this deployment, e.g. "
            "`https://mcp.hpt.su/mcp` (or `https://mcp.sk3d.ru/mcp` for the "
            "sk3d mirror — each hostname runs its own container/env, so this "
            "is set per-deployment, not derived from the request Host). "
            "Drives RFC9728 `/.well-known/oauth-protected-resource/mcp` "
            "discovery and the 401 `WWW-Authenticate` challenge on `/mcp`. "
            "Empty (default) disables the OAuth resource-server gate "
            "entirely: `X-API-Key` keeps working as before, and a missing "
            "Bearer stays a 200 JSON-RPC 'Authentication failed' response "
            "instead of a transport 401 — useful for local dev/tests "
            "without a public hostname."
        ),
    )
    oauth_issuer_url: str = Field(
        default="https://hpt.su",
        description=(
            "OAuth 2.0 Authorization Server issuer advertised in the "
            "RFC9728 protected-resource metadata (`authorization_servers`). "
            "Only used when `resource_url` is set."
        ),
    )

    # LOW#629: явный отказ от wildcard — `*` отключает защиту от DNS-rebinding
    # и позволяет любому веб-сайту через user-agent атаковать локальный MCP.
    @field_validator('allowed_hosts')
    @classmethod
    def _no_wildcard(cls, v: str) -> str:
        hosts = {h.strip() for h in v.split(',') if h.strip()}
        if '*' in hosts:
            raise ValueError(
                "HPTSU_ALLOWED_HOSTS='*' disables DNS-rebinding protection. "
                "List actual hostnames (e.g. 'mcp.hpt.su,mcp.hpt.su:*') "
                "or leave empty for localhost-only.",
            )
        return v


def load_settings() -> Settings:
    return Settings()

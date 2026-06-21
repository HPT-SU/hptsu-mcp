"""Конфиг через env. Все значения опциональны кроме API-ключа для prod-режима."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    api_key: str | None = Field(
        default=None,
        description=(
            "Bearer API key issued in the hpt.su personal cabinet "
            "(requires active API_TIER subscription)."
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
        default="hptsu-mcp/0.1",
        description="User-Agent header for outgoing API calls.",
    )


def load_settings() -> Settings:
    return Settings()

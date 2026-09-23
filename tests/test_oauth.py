"""OAuth resource-server gate: PRM route (B1), real TokenVerifier (B2),
sentinel-bypass hardening (B3), ToolError conversion (B4) and version
reporting (B5).
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hptsu_mcp import __version__
from hptsu_mcp.client import HptSuClient
from hptsu_mcp.config import Settings
from hptsu_mcp.server import (
    _SENTINEL_INJECTED,
    _WHOAMI_PATH,
    _X_API_KEY_BYPASS_TOKEN,
    _XApiKeyBypassMiddleware,
    _build_auth,
    _canonical_resource,
    _install_protected_resource_route,
    _positive_cache_ttl,
    _protected_resource_metadata,
    _protected_resource_route,
    HptSuTokenVerifier,
    get_document,
    mcp,
)


_WELL_KNOWN = "/.well-known/oauth-protected-resource/mcp"


def _oauth_settings(**overrides) -> Settings:
    defaults = dict(
        api_key="test-key",
        base_url="https://hpt.su/api/v1",
        resource_url="https://mcp.hpt.su/mcp",
        oauth_issuer_url="https://hpt.su",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _ctx(client: HptSuClient) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"client": client},
            session=SimpleNamespace(client_params=None),
            request=None,
        ),
    )


# ──── B1: RFC9728 PRM без хвостового слеша ───────────────────────────────


def test_protected_resource_metadata_authorization_servers_no_trailing_slash() -> None:
    settings = _oauth_settings()
    data = _protected_resource_metadata(settings)
    assert data["authorization_servers"] == ["https://hpt.su"]
    assert data["resource"] == "https://mcp.hpt.su/mcp"


def test_protected_resource_route_serves_metadata_with_cors() -> None:
    settings = _oauth_settings()
    app = Starlette(routes=[_protected_resource_route(settings)])
    client = TestClient(app)

    resp = client.get(_WELL_KNOWN)
    assert resp.status_code == 200
    assert resp.json()["authorization_servers"] == ["https://hpt.su"]
    assert resp.headers["cache-control"] == "public, max-age=3600"

    preflight = client.options(_WELL_KNOWN, headers={
        "Origin": "https://chatgpt.com",
        "Access-Control-Request-Method": "GET",
    })
    assert preflight.headers.get("access-control-allow-origin") == "*"


def test_install_protected_resource_route_replaces_sdk_route() -> None:
    """SDK-роут (со слешем в authorization_servers) должен исчезнуть, не задвоиться."""
    settings = _oauth_settings()
    sdk_routes = create_protected_resource_routes(
        resource_url=AnyHttpUrl(settings.resource_url),
        authorization_servers=[AnyHttpUrl(settings.oauth_issuer_url)],
        scopes_supported=["mcp"],
    )
    app = Starlette(routes=list(sdk_routes))

    _install_protected_resource_route(app, settings)

    matching = [r for r in app.router.routes if getattr(r, "path", None) == _WELL_KNOWN]
    assert len(matching) == 1
    resp = TestClient(app).get(_WELL_KNOWN)
    assert resp.json()["authorization_servers"] == ["https://hpt.su"]


# ──── B2: настоящий TokenVerifier через /mcp/whoami/ ─────────────────────


def _verifier(**overrides) -> HptSuTokenVerifier:
    return HptSuTokenVerifier(_oauth_settings(**overrides))


@pytest.mark.asyncio
async def test_verify_token_empty_returns_none() -> None:
    assert await _verifier().verify_token("") is None


@pytest.mark.asyncio
async def test_verify_token_active_returns_access_token() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        route = mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": True, "exp": 9999999999, "aud": None, "scope": "mcp"}))
        result = await _verifier().verify_token("good-token")
    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer good-token"
    assert isinstance(result, AccessToken)
    assert result.client_id == "oauth"
    assert result.scopes == ["mcp"]
    assert result.expires_at == 9999999999


@pytest.mark.asyncio
async def test_verify_token_401_returns_none() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(401, json={"detail": "no"}))
        result = await _verifier().verify_token("bad-token")
    assert result is None


@pytest.mark.asyncio
async def test_verify_token_aud_mismatch_rejected() -> None:
    """RFC 8707 — токен выпущен для другого resource."""
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": True, "exp": 9999999999, "aud": "https://mcp.other.example/mcp", "scope": "mcp"}))
        result = await _verifier().verify_token("cross-resource-token")
    assert result is None


@pytest.mark.asyncio
async def test_verify_token_aud_matches_accepted() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": True, "exp": 9999999999, "aud": "https://mcp.hpt.su/mcp", "scope": "mcp"}))
        result = await _verifier().verify_token("same-resource-token")
    assert isinstance(result, AccessToken)
    assert result.resource == "https://mcp.hpt.su/mcp"


@pytest.mark.asyncio
async def test_verify_token_aud_canonically_equivalent_accepted() -> None:
    """Разный регистр, дефолтный порт и хвостовой слеш — тот же resource."""
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={
                "active": True, "exp": 9999999999,
                "aud": "https://MCP.hpt.su:443/mcp/", "scope": "mcp",
            }))
        result = await _verifier().verify_token("case-and-port-token")
    assert isinstance(result, AccessToken)


@pytest.mark.asyncio
async def test_verify_token_aud_different_path_rejected() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": True, "exp": 9999999999, "aud": "https://mcp.hpt.su/other", "scope": "mcp"}))
        result = await _verifier().verify_token("different-path-token")
    assert result is None


def test_canonical_resource_normalizes_scheme_host_port_and_slash() -> None:
    assert _canonical_resource("https://MCP.hpt.su:443/mcp/") == _canonical_resource("https://mcp.hpt.su/mcp")
    assert _canonical_resource("http://Example.com:80/x/") == "http://example.com/x"


def test_canonical_resource_distinguishes_different_path_or_host() -> None:
    assert _canonical_resource("https://mcp.hpt.su/mcp") != _canonical_resource("https://mcp.hpt.su/other")
    assert _canonical_resource("https://mcp.hpt.su/mcp") != _canonical_resource("https://mcp.sk3d.ru/mcp")
    # Нестандартный порт — не отбрасывается.
    assert _canonical_resource("https://mcp.hpt.su:8443/mcp") != _canonical_resource("https://mcp.hpt.su/mcp")


@pytest.mark.asyncio
async def test_verify_token_sends_user_agent_with_verifier_suffix() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        route = mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(401))
        await _verifier().verify_token("some-token")
    ua = route.calls.last.request.headers["user-agent"]
    assert ua.endswith(" token-verifier")
    assert ua.startswith("hptsu-mcp/")


@pytest.mark.asyncio
async def test_verify_token_inactive_returns_none() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": False}))
        result = await _verifier().verify_token("revoked-token")
    assert result is None


@pytest.mark.asyncio
async def test_verify_token_5xx_fails_open() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(503, text="boom"))
        result = await _verifier().verify_token("some-token")
    assert isinstance(result, AccessToken)
    assert result.client_id == "blind-oauth"
    assert result.expires_at is None


@pytest.mark.asyncio
async def test_verify_token_connection_error_fails_open() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(side_effect=httpx.ConnectError("refused"))
        result = await _verifier().verify_token("some-token")
    assert isinstance(result, AccessToken)
    assert result.client_id == "blind-oauth"


@pytest.mark.asyncio
async def test_verify_token_timeout_fails_open() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(side_effect=httpx.ReadTimeout("slow"))
        result = await _verifier().verify_token("some-token")
    assert isinstance(result, AccessToken)


@pytest.mark.asyncio
async def test_verify_token_malformed_json_fails_open() -> None:
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}))
        result = await _verifier().verify_token("some-token")
    assert isinstance(result, AccessToken)
    assert result.client_id == "blind-oauth"


@pytest.mark.asyncio
async def test_verify_token_caches_positive_result() -> None:
    verifier = _verifier()
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        route = mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": True, "exp": 9999999999, "aud": None, "scope": "mcp"}))
        await verifier.verify_token("cached-token")
        await verifier.verify_token("cached-token")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_verify_token_caches_negative_result() -> None:
    verifier = _verifier()
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        route = mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(401))
        await verifier.verify_token("cached-bad-token")
        await verifier.verify_token("cached-bad-token")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_verify_token_cache_evicts_oldest_entry_when_full() -> None:
    """Лимит понижен через конструктор — не ждём 10 000 реальных токенов."""
    verifier = HptSuTokenVerifier(_oauth_settings(), cache_max_entries=2)
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        route = mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(
            200, json={"active": True, "exp": 9999999999, "aud": None, "scope": "mcp"}))
        await verifier.verify_token("token-1")
        await verifier.verify_token("token-2")
        await verifier.verify_token("token-3")  # переполнение — token-1 вытесняется.
        assert route.call_count == 3

        await verifier.verify_token("token-3")  # ещё в кэше — новый запрос не нужен.
        assert route.call_count == 3

        await verifier.verify_token("token-1")  # был вытеснен — повторный поход в сеть.
        assert route.call_count == 4


@pytest.mark.asyncio
async def test_verify_token_cache_expiry_triggers_recheck() -> None:
    """Инжектируемые часы — истёкшая запись кэша не переиспользуется."""
    clock = {"t": 0.0}
    verifier = HptSuTokenVerifier(_oauth_settings(), clock=lambda: clock["t"])
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        route = mock.get(_WHOAMI_PATH).mock(return_value=httpx.Response(401))
        await verifier.verify_token("expiring-token")
        clock["t"] += 10.1  # TTL негативного кэша — 10с.
        await verifier.verify_token("expiring-token")
    assert route.call_count == 2


def test_positive_cache_ttl_capped_at_60() -> None:
    assert _positive_cache_ttl(None) == 60.0
    import time
    far_future = int(time.time()) + 10_000
    assert _positive_cache_ttl(far_future) == 60.0


def test_positive_cache_ttl_bounded_by_exp() -> None:
    import time
    soon = int(time.time()) + 5
    ttl = _positive_cache_ttl(soon)
    assert 0.0 <= ttl <= 5.5


def test_positive_cache_ttl_past_exp_is_zero() -> None:
    import time
    assert _positive_cache_ttl(int(time.time()) - 100) == 0.0


# ──── B3: сентинел принимается только если его подставила сама middleware ─


@pytest.mark.asyncio
async def test_verify_token_sentinel_without_flag_rejected() -> None:
    """Клиент, приславший сентинел сам, не должен пройти."""
    assert await _verifier().verify_token(_X_API_KEY_BYPASS_TOKEN) is None


@pytest.mark.asyncio
async def test_verify_token_sentinel_with_flag_accepted() -> None:
    reset_token = _SENTINEL_INJECTED.set(True)
    try:
        result = await _verifier().verify_token(_X_API_KEY_BYPASS_TOKEN)
    finally:
        _SENTINEL_INJECTED.reset(reset_token)
    assert isinstance(result, AccessToken)
    assert result.client_id == "x-api-key"


async def _run_asgi(app, headers: list[tuple[bytes, bytes]]) -> tuple[dict, list]:
    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": headers}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    captured: dict = {}

    async def inner(inner_scope, inner_receive, inner_send):
        captured["flag"] = _SENTINEL_INJECTED.get()
        captured["headers"] = inner_scope["headers"]
        await inner_send({"type": "http.response.start", "status": 200, "headers": []})
        await inner_send({"type": "http.response.body", "body": b""})

    await _XApiKeyBypassMiddleware(inner)(scope, receive, send)
    return captured, sent


@pytest.mark.asyncio
async def test_bypass_middleware_sets_flag_only_when_it_injects_sentinel() -> None:
    captured, _ = await _run_asgi(None, [(b"x-api-key", b"pub:secret")])
    assert captured["flag"] is True
    assert (b"authorization", f"Bearer {_X_API_KEY_BYPASS_TOKEN}".encode()) in captured["headers"]


@pytest.mark.asyncio
async def test_bypass_middleware_leaves_flag_unset_when_authorization_present() -> None:
    headers = [(b"x-api-key", b"pub:secret"), (b"authorization", b"Bearer client-token")]
    captured, _ = await _run_asgi(None, headers)
    assert captured["flag"] is False
    assert captured["headers"] == headers


@pytest.mark.asyncio
async def test_bypass_middleware_leaves_flag_unset_without_api_key() -> None:
    headers = [(b"authorization", b"Bearer client-token")]
    captured, _ = await _run_asgi(None, headers)
    assert captured["flag"] is False
    assert captured["headers"] == headers


def test_build_auth_uses_real_verifier_when_resource_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPTSU_RESOURCE_URL", "https://mcp.hpt.su/mcp")
    auth, verifier = _build_auth()
    assert auth is not None
    assert isinstance(verifier, HptSuTokenVerifier)


def test_build_auth_disabled_without_resource_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPTSU_RESOURCE_URL", raising=False)
    assert _build_auth() == (None, None)


# ──── B4: upstream/validation ошибки — ToolError (isError=true) ─────────


@pytest.mark.asyncio
async def test_invalid_kind_raises_tool_error_with_original_text() -> None:
    with pytest.raises(ToolError) as exc_info:
        await get_document(_ctx(HptSuClient(_oauth_settings())), slug="x", kind="bogus")
    assert str(exc_info.value) == "Invalid kind='bogus'. Allowed: ['cert', 'decl', 'otch', 'otts', 'sbkts', 'sout', 'zoets', 'zotch', 'zotts']."


@pytest.mark.asyncio
async def test_upstream_error_raises_tool_error_preserving_upgrade_link() -> None:
    settings = _oauth_settings()
    with respx.mock(base_url="https://hpt.su/api/v1") as mock:
        mock.get("/docs/cert/x/").mock(return_value=httpx.Response(
            402, json={
                "code": "PAYMENT_REQUIRED", "message": "need upgrade",
                "upgrade_url": "https://hpt.su/pricing/",
            }))
        async with HptSuClient(settings) as client:
            with pytest.raises(ToolError) as exc_info:
                await get_document(_ctx(client), slug="x", kind="cert")
    assert str(exc_info.value) == "Payment required. need upgrade Upgrade at https://hpt.su/pricing/."


@pytest.mark.asyncio
async def test_tool_error_wrapped_prefix_via_fastmcp_call_tool() -> None:
    """Фиксирует фактический вид: FastMCP.Tool.run оборачивает ToolError ещё раз."""
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("search_by_vin", {"vin": "abc"})
    assert str(exc_info.value) == (
        "Error executing tool search_by_vin: Invalid VIN length: 3 chars (expected 5-17)."
    )


# ──── B5: serverInfo.version — версия пакета, не MCP SDK ─────────────────


def test_server_reports_package_version_not_sdk_version() -> None:
    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_version == __version__
    assert opts.server_version != "1.29.0"

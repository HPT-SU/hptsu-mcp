# Changelog

All notable changes to `hptsu-mcp` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
the project adheres to [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-06-20

### Added

- Initial scaffold of the hpt.su MCP server.
- 7 tools: `search_documents`, `get_document`, `search_certificates`,
  `search_declarations`, `search_type_approvals`, `search_safety_reports`,
  `list_registry_kinds`.
- Resource `hptsu://about` describing the registry.
- Async httpx client over `https://hpt.su/api/v1/` with Bearer API-key auth.
- Stdio / streamable-HTTP / SSE transports (selected via `HPTSU_TRANSPORT`).
- pydantic-settings config (`HPTSU_*` env variables).
- Catalogue manifests: `server.json` (Official MCP Registry),
  `smithery.yaml`, `glama.json`, MCPB `mcpb/manifest.json`.
- Dockerfile for `ghcr.io/hpt-su/hptsu-mcp`.
- CI workflows: lint+test, PyPI publish on tag, Docker publish on tag,
  Official MCP Registry publish on tag.
- Unit tests with respx-mocked httpx (`tests/test_client.py`,
  `tests/test_server.py`).
- Documentation: `README.md`, `docs/CLIENT_CONFIGS.md`.

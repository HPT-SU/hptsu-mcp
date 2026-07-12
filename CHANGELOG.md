# Changelog

All notable changes to `hptsu-mcp` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
the project adheres to [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-07-12

### Fixed

- README: маркер `mcp-name: su.hpt/hptsu-mcp` — Official MCP Registry проверяет
  владение PyPI-пакетом по этой строке в README пакета. `server.json`: короткий
  `description` (лимит реестра 100 символов) + hosted remote `mcp.hpt.su`.

## [0.3.0] — 2026-07-11

Первый публичный релиз (hosted `mcp.hpt.su`). Включает всё, что накопилось после
0.1.0: версия 0.2.0 существовала только как внутренний staging-билд
и отдельно не выпускалась — её изменения вошли в этот релиз.

### Changed

- **BREAKING**: `list_document_files` теперь требует обязательный `kind`
  (как `get_document`) — slug не уникален между kinds, и без `kind` могли
  вернуться файлы случайного документа. Upstream URL изменён:
  `/api/v1/docs/<kind>/<slug>/files/` (был `/api/v1/docs/<slug>/files/`).
- `axis_count` параметр search-тулов: тип `int` → `str` — можно передать число
  осей («2») или точное наименование («2 / 4»); резолвится в id справочника
  осей/колёс (число осей может дать несколько записей → кандидаты).
- `search_by_vin`: убран per-kind подписочный гейт — VIN-поиск открыт во
  всех 6 car-kinds (anon → conversion funnel к покупке документа).
- `hptsu://about` resource: обновлён список tools (per-kind), новая
  auth-схема, новые endpoints.
- Auth scheme: `X-API-Key: <public_id>:<secret>` (was `Authorization: Bearer`)
  to match `td_billing.api.auth.ApiKeyAuthentication`.
- `download_document_file` now accepts `file_uid` (was `document_id`); pair it
  with `list_document_files` to enumerate file UIDs of a document.
- `search_by_vin`: minimum VIN length lowered from 10 to 5 chars (matches upstream).
- All `next`/`previous` pagination URLs returned by API now use the public host
  (`https://hpt.su/...`) instead of the container address (`http://web-hpt-su:8000/...`).

### Fixed

- Slug-валидатор `_safe` разрешает кириллицу и точки (`^[\w.-]+$`): реальные
  номера-slug'и кириллические (`тс-ru-e-ru.нв23.00256`), из-за прежнего
  ASCII-only regex `get_document` / `list_document_files` /
  `download_document_file` не резолвились ни на одном документе. Path-traversal
  по-прежнему закрыт (нет `/`, запрещён `..`). Требует парного бэкенд-фикса
  `document.api.mixins` (точка в `lookup_value_regex` роута detail).

### Added

- **Авто-резолв имя→id справочных фильтров**: `search_*.issuer`,
  `list_vehicle_models.brand`, `search_*.eco_class` / `wheel_formula` /
  `axis_count` принимают человекочитаемое значение — сервер резолвит его в pk
  через `/nsi/`-справочники (0→ошибка, 1→подстановка, много→список кандидатов),
  кэш на процесс. `issuer`/`brand` дополнительно принимают числовой id напрямую.
  Требует бэкенд-эндпоинты `/nsi/eco-classes|axis-counts|wheel-formulas/`.

- **Per-kind search tools**: `search_otts`, `search_otch`, `search_zotts`,
  `search_zotch`, `search_sbkts`, `search_zoets`, `search_sout` — заменяют
  обобщённые `search_type_approvals(kind=...)` / `search_safety_reports(kind=...)`.
  Каждый kind теперь имеет полный набор параметров: spec-фильтры
  (`category`, `eco_class`, `wheel_formula`, `axis_count`, `issuer`),
  для sbkts/zoets — VIN substring/exact + `engine`/`year`/`motor`/`motor_power` +
  `date_from`/`date_to`.
- `search_certificates` / `search_declarations` расширены: `applicant_inn`
  (exact), `manufacturer`, `regulations` (ТР ТС / ТР ЕАЭС code),
  `product` (product_full_name icontains), `scheme` для decl.
- `fulltext_search`: расширен с otts на все 4 type-approval kinds
  (otts/otch/zotts/zotch). Опциональный `kind` параметр ограничивает один из.
- 9 new MCP tools: `search_by_vin`, `fulltext_search` (paid), `download_document_file` (paid),
  `list_document_files`, `list_brands`, `list_vehicle_models`, `list_test_labs`,
  `list_certification_bodies`, `list_tnved_codes`.
- `X-MCP-Client` attribution: client name+version extracted from MCP `InitializeParams`
  and forwarded to upstream API.
- Per-request token forwarding for hosted (streamable-HTTP) mode: incoming
  `Authorization: Bearer` is preserved as Bearer; `X-API-Key` is passed through as is.
- HTTP transport `host` / `port` / `allowed_hosts` configurable via env
  (`HPTSU_HOST=0.0.0.0`, `HPTSU_ALLOWED_HOSTS=mcp.hpt.su,mcp.hpt.su:*` for hosted).
- `/healthz` and `/readyz` probes (latter pings upstream without auth, expects 401).
- Multi-stage Dockerfile with non-root user, HEALTHCHECK.
- `deploy/` directory with docker-compose example + nginx vhost.
- `docs/CLIENT_CONFIGS.md`: snippets for Claude Desktop, Cursor, Cline, Continue,
  Goose, Cherry Studio, 5ire, LM Studio, VS Code Copilot, Zed.

### Removed

- `search_type_approvals(kind=otts|otch)` → используйте `search_otts` / `search_otch`.
- `search_safety_reports(kind=...)` → используйте `search_sbkts` /
  `search_zoets` / `search_zotts` / `search_zotch` / `search_sout`.
  MCP ещё не публиковался — legacy не поддерживаем, сразу чистая структура.

### Breaking

- Document identifier: UUID (`number_code`) **больше не возвращается** в API.
  `get_document` / `list_document_files` / `download_document_file` теперь
  работают по **slug** (URL-form of number — тот же что в site URL).
  `get_document(slug, kind)` обязательно требует kind, т.к. slug не
  гарантированно уникален между kinds.

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

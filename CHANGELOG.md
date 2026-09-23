# Changelog

All notable changes to `hptsu-mcp` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
the project adheres to [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.6.0] — 2026-09-24

### Changed

- **Tool errors are now real MCP errors** (`isError: true`), not text inside
  a normal result. Upstream errors (Payment required / Rate limit /
  Forbidden / Not found / Authentication failed / Invalid input /
  NO_FILTERS / 5xx), invalid `kind`/VIN-length validation, and ambiguous
  reference-lookup responses (`list_*` resolution) all raise
  `mcp.server.fastmcp.exceptions.ToolError` now. Message text (including
  upgrade links and ambiguity candidates) is unchanged — only how it's
  surfaced to the client changed. Note: FastMCP itself prefixes the final
  text with `Error executing tool <name>: ` — that's an SDK behaviour, not
  ours.
- **Invalid/expired/revoked Bearer tokens now get a transport 401**
  (hosted mode, `HPTSU_RESOURCE_URL` set) instead of a 200 JSON-RPC
  response with "Authentication failed" text. Implemented via a real
  `TokenVerifier` (`HptSuTokenVerifier`) that calls
  `GET {HPTSU_BASE_URL}/mcp/whoami/` on hpt.su. See "Compatibility" below
  for what happens when hpt.su itself is unreachable.
- **RFC 8707 resource-indicator check**: a Bearer token issued for a
  different MCP resource (`aud` in the `whoami` response doesn't match
  `HPTSU_RESOURCE_URL`) is now rejected (401), not silently accepted.
- **`/.well-known/oauth-protected-resource/mcp` (RFC 9728)**:
  `authorization_servers` is now `["https://hpt.su"]` — byte-for-byte equal
  to the issuer this server advertises. Previously the SDK's default route
  emitted `["https://hpt.su/"]` (pydantic's `AnyHttpUrl` appends a trailing
  slash to a bare host), which fails strict RFC 8414/9728 issuer comparison
  in some OAuth client implementations. `resource_metadata` in the 401
  `WWW-Authenticate` challenge is unchanged.
- `serverInfo.version` in the `initialize` response now reports the
  `hptsu-mcp` package version instead of the underlying MCP SDK version.
- The `hptsu-x-api-key-bypass` sentinel Bearer token (internal — used only
  to route a static `X-API-Key` through the SDK's OAuth gate) is now
  rejected if a client sends it directly, instead of being accepted as a
  valid token.

### Compatibility (действий не требуется)

- `X-API-Key` clients: unaffected. The bypass path never calls
  `/mcp/whoami/` and is unaffected by the RFC 8707 check.
- Already-connected OAuth clients with a currently-valid token: unaffected.
- If hpt.su's `/mcp/whoami/` is unreachable or errors (5xx, timeout,
  connection error, malformed JSON) the gate **fails open**: the token is
  accepted at the transport level exactly as before (real validation still
  happens on hpt.su when the tool call is proxied) — a Django restart or
  blip does not disconnect active OAuth sessions. A positive result is
  cached 60s (capped by the token's `exp`), a negative (401) result 10s, a
  fail-open result 5s — bounded in-process cache (~10k entries, oldest
  evicted first), ≤20 concurrent `whoami` requests in flight, 2.5s timeout
  per check.
- stdio mode: unaffected — none of this applies outside `streamable-http`/
  `sse` transports.

## [0.5.1] — 2026-09-24

### Changed

- Описания инструментов приведены к фактическому контракту hpt.su API:
  list-эндпоинты бэкенда отвечают `NO_FILTERS`, если не задан ни один
  фильтр. `search_documents.number`, `list_brands.name`,
  `list_test_labs.name`, `list_certification_bodies.name` — теперь
  обязательные параметры; у многофильтровых поисков требование указано
  в описании.
- Тарифы в `instructions`, текстах ошибок, ресурсе `hptsu://about` и README:
  платный доступ — API-тарифы Lite/Standard/Pro (500–10 000 запросов/день),
  а не «Paid MCP 10 000/day».
- `status` у `search_certificates`/`search_declarations` перечисляет коды
  (ACTIVE, SUSPENDED, RESUMED, TERMINATED, CANCELLED, ARCHIVED, EXTENDED, UNKNOWN).
- `search_documents`/`get_document`/`list_brands`/`download_document_file`:
  описания уточнены (что возвращают, как связаны со следующими вызовами).
- Ответы инструментов — компактный JSON (без отступов): меньше токенов в
  контексте клиента.

### Removed

- Список инструментов из ресурса `hptsu://about` (дублировал реальный
  список tools и устаревал).

## [0.5.0] — 2026-09-22

### Added

- **OAuth resource-server discovery for hosted mode** (`HPTSU_TRANSPORT=http`):
  когда задан новый env `HPTSU_RESOURCE_URL` (например
  `https://mcp.hpt.su/mcp`), сервер отдаёт RFC9728
  `/.well-known/oauth-protected-resource/mcp` и отвечает 401 +
  `WWW-Authenticate: Bearer resource_metadata="..."` на `/mcp` при
  отсутствии и `Authorization: Bearer`, и `X-API-Key` — это раскрывает
  discovery-цепочку OAuth-коннекторов, у которых нет ручного ввода
  client_id/URL (ChatGPT Developer Mode/Connectors). Реализовано через
  штатный OAuth-RS механизм MCP SDK (`AuthSettings`/`TokenVerifier`), без
  ручных well-known/401-хендлеров.

### Compatibility (обратная совместимость — действий не требуется)

- **Существующие клиенты не затронуты.** `HPTSU_RESOURCE_URL` не задан по
  умолчанию — если не установить его явно, поведение `/mcp` не меняется:
  и `X-API-Key`, и `Authorization: Bearer` продолжают работать в точности
  как раньше, запрос без креденшлов остаётся 200 (текстовая ошибка внутри
  JSON-RPC-ответа), `/healthz`/`/readyz` не тронуты.
- Даже когда `HPTSU_RESOURCE_URL` включён (планируется только на
  hosted-инстансах mcp.hpt.su/mcp.sk3d.ru): `X-API-Key`-клиенты по-прежнему
  проходят без OAuth-челленджа (внутренний bypass), уже подключённые
  OAuth-клиенты (Cursor/Smithery/Claude Desktop) продолжают работать — их
  Bearer-токен принимается транспортом «слепо» (валидность реально
  проверяет hpt.su при проксировании запроса, как и раньше).
- stdio-режим (локальные MCP-клиенты) не изменился вообще — вся эта логика
  относится только к `streamable-http`/`sse`-транспортам.
- `MCP_MIN_CLIENT_VERSION` (hpt_mcp, серверная сторона hpt.su) **не
  менялся** — обновление stdio-клиентов не требуется.

### Known limitations (см. docs/ai/plans/mcp_oauth_chatgpt.md, репо hpt_su)

- Bearer-токен на транспорте проверяется «слепо» (любой непустой токен
  принимается) — реальная валидация происходит на hpt.su при
  проксировании запроса. Полная интроспекция (RFC7662) требует отдельного
  client_credentials-клиента на AS — не сделано в этой итерации.
- Просроченный/невалидный Bearer сегодня всё ещё возвращает 200 с текстом
  «Authentication failed» внутри JSON-RPC-ответа конкретного tool-вызова
  (не транспортный 401) — конверсия upstream-401 в транспортный
  401+challenge не реализована.
- `HPTSU_RESOURCE_URL` статичен на процесс — для `mcp.hpt.su` и
  `mcp.sk3d.ru` (или другого домена) значение задаётся per-deployment
  через env, не выводится динамически из заголовка `Host` одного и того
  же процесса.

## [0.4.4] — 2026-08-18

### Fixed

- **Пагинация работала только для первой страницы**: тулы принимают
  `page`/`page_size`, но бэкенд `/api/v1/` пагинируется `limit`/`offset`
  (DRF LimitOffsetPagination) и молча игнорировал наши параметры — любой
  `page` возвращал первые 20 строк. Клиент теперь транслирует
  `page`/`page_size` → `limit`/`offset` (`offset = (page-1) × page_size`).
  Глубина листания ограничена бэкендом: `offset ≤ 1000`, дальше — явный 400
  с подсказкой сузить фильтр.

## [0.4.3] — 2026-08-10

### Security

- Бамп зависимостей в lock (Docker-образ): `cryptography` 49.0.0 → 50.0.0
  (Bleichenbacher oracle в PKCS#7 EnvelopedData — функциональность не
  используется, бамп для чистоты сканов), `mcp` 1.28.0 → 1.29.0 (отсутствие
  Host/Origin-валидации в WebSocket-транспорте — транспорт не используется,
  у нас stdio и streamable-http). Кода изменения не касаются.

## [0.4.2] — 2026-08-10

### Changed

- `download_document_file`: описание `download_url` — ссылку теперь можно
  скачать программно GET-запросом с тем же заголовком `X-API-Key`
  (бэкенд 2026-08-10 разрешил bearer-скачивание без браузерной сессии;
  ссылка по-прежнему привязана к владельцу ключа — чужим ключом или из
  чужого аккаунта не работает). Изменение только текстовое, логика клиента
  не менялась.

## [0.4.1] — 2026-07-30

### Changed

- `download_document_file`: теперь ВСЕГДА возвращает `document_page_url` —
  страницу документа на hpt.su, где файл можно скачать вручную (в т.ч. на
  бесплатном тарифе; раньше free получал 402). `download_url` — только на
  тарифах с прямым скачиванием, иначе null + причина в `download_note`.
  Требует бэкенд с изменением 2026-07-30; со старым бэкендом поведение
  прежнее.

## [0.4.0] — 2026-07-14

### Added

- Новые фасеты `search_sbkts` / `search_zoets`: `powertrain`
  (`ice`/`hybrid`/`electric`) и `fuel` (`бензин`/`дизель`/`газ`/`электро`/
  `водород`) — поиск «все электромобили», «все дизельные» и т.п.
- Проверка версии клиента: бэкенд hpt.su объявляет минимальную совместимую
  версию заголовком `X-HPTSU-MCP-Min-Version`; устаревший клиент пишет
  warning в лог и (в stdio-режиме) один раз предупреждает пользователя
  припиской к ответу ближайшего тула с инструкцией обновления.

### Removed

- **BREAKING**: `list_test_labs` больше не принимает `short_id` — параметр
  удалён из API hpt.su (вызов с ним возвращал 400). Ищите лабораторию по
  `name`.

### Changed

- Описания тулов и параметров переписаны без деталей внутренней реализации
  (DAWG, icontains, UNION, внутренние коды) — только пользовательская
  семантика.

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

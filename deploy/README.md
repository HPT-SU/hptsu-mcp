# Hosted deployment — `mcp.hpt.su`

Файлы для запуска `hptsu-mcp` как hosted-сервиса в существующей Docker-инфраструктуре hpt.su (общая сеть с postgres / redis / nginx / Django web).

## Файлы

| Файл | Назначение |
|------|------------|
| `docker-compose.example.yml` | Compose-фрагмент. Подключается к external-сети, поднимает контейнер `mcp` без публикации порта наружу. |
| `nginx-mcp.conf` | Vhost-конфиг для поддомена `mcp.hpt.su`. Terminate TLS + proxy → `mcp:8000`. SSE-friendly (буферизация выключена). |

## Что нужно настроить отдельно (на сервере)

1. **DNS**: A/AAAA-запись `mcp.hpt.su → IP сервера`.
2. **TLS**: `certbot --nginx -d mcp.hpt.su` (или другой ACME-клиент).
3. **External-сеть**: подставить реальное имя Docker-сети hpt.su в `networks.hpt_su_internal.name` в compose.
4. **Имя сервиса hpt.su web**: подставить в `HPTSU_BASE_URL` если отличается от `hpt_su_web`. Проверить `docker network inspect <network>`.
5. **nginx include**: положить `nginx-mcp.conf` в `/etc/nginx/sites-available/` и включить через симлинк.

**Сервисный ключ не нужен**: ключ юзера приходит per-request в заголовке `Authorization: Bearer <key>` или `X-API-Key: <key>` от его MCP-клиента (Claude/Cursor/Cline). Контейнер не хранит ничего.

## OAuth для ChatGPT-коннектора (опционально)

`HPTSU_RESOURCE_URL` (например `https://mcp.hpt.su/mcp`, для sk3d-зеркала — `https://mcp.sk3d.ru/mcp`) включает RFC9728/RFC8414 OAuth-discovery на `/mcp`, нужную ChatGPT Developer Mode/Connectors (у них нет ручного ввода client_id/URL — коннектор сам находит authorization server через 401-challenge). Подробности механизма и открытые вопросы — `docs/ai/plans/mcp_oauth_chatgpt.md` в репо hpt_su, CHANGELOG.md 0.5.0.

**Существующим пользователям обновление не требуется**:

- **X-API-Key stdio/hosted-клиенты** — поведение не меняется вообще, с включённым `HPTSU_RESOURCE_URL` или без него.
- **Уже подключённые OAuth-клиенты** (Cursor/Smithery/Claude Desktop через `Authorization: Bearer`) — продолжают работать без переподключения.
- Переменная нужна **только** для нового ChatGPT-подключения; без неё `/mcp` ведёт себя как раньше (никакого 401-challenge).

## Что НЕ нужно

- Volume — сервис stateless.
- Прямое подключение к postgres/redis из контейнера MCP — все обращения через `HPTSU_BASE_URL` (Django API). Если решим throttle на стороне MCP вместо upstream — тогда понадобится Redis.
- celery — у MCP нет фоновых задач.

## Healthcheck

Контейнер сам ходит на `http://127.0.0.1:8000/healthz` каждые 30 секунд — это даёт автоматический рестарт при зависании. nginx-конфиг публикует `https://mcp.hpt.su/healthz` для внешнего мониторинга.

`readyz` дополнительно проверяет доступность upstream hpt.su API (один запрос-разведка). Используй в Kubernetes или Uptime-мониторинге, но не в Docker HEALTHCHECK — это убъёт контейнер при кратком сбое upstream.

## Smoke test после запуска

```bash
# Liveness
curl https://mcp.hpt.su/healthz
# {"status":"ok","version":"0.3.0"}

# Readiness (проверяет upstream — ходит в hpt.su без auth, ждёт 401)
curl https://mcp.hpt.su/readyz
# {"status":"ready","version":"0.3.0","upstream":"auth-rejected-as-expected"}

# MCP-протокол — инициализация (для теста через mcp-cli)
npx @modelcontextprotocol/inspector https://mcp.hpt.su/mcp
```

## Ограничения ресурсов

В compose установлены лимиты:
- 256 MB RAM (типичное использование ~80 MB)
- 0.5 CPU

При резком росте нагрузки — повысить, либо горизонтально масштабировать через несколько replica.

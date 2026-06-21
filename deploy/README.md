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
5. **API-ключ**: положить `HPTSU_API_KEY=<scope=MCP key>` в окружение (.env / secrets).
6. **nginx include**: положить `nginx-mcp.conf` в `/etc/nginx/sites-available/` и включить через симлинк.

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
# {"status":"ok","version":"0.1.0"}

# Readiness (проверяет upstream)
curl https://mcp.hpt.su/readyz
# {"status":"ready","version":"0.1.0"}

# MCP-протокол — инициализация (для теста через mcp-cli)
npx @modelcontextprotocol/inspector https://mcp.hpt.su/mcp
```

## Ограничения ресурсов

В compose установлены лимиты:
- 256 MB RAM (типичное использование ~80 MB)
- 0.5 CPU

При резком росте нагрузки — повысить, либо горизонтально масштабировать через несколько replica.

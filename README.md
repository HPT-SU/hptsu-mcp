# hpt.su MCP Server

> **Model Context Protocol server for the [hpt.su](https://hpt.su) registry of Russian and EAEU vehicle compliance documents** — type approvals (ОТТС / СБКТС / ОТШ), conformity certificates (ТР ТС / ТР ЕАЭС), declarations of conformity, type-approval notifications (СУТ), and single-vehicle evaluation reports.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

## What this is

`hptsu-mcp` lets any [MCP-compatible](https://modelcontextprotocol.io) AI assistant (Claude Desktop, Cursor, Cline, Continue, Goose, Cherry Studio, 5ire, LM Studio, ChatGPT Desktop, …) search the **hpt.su** registry directly — without you copy-pasting numbers between the chat window and the website.

[**hpt.su**](https://hpt.su) is the largest aggregated database in the Russian Federation and the EAEU customs union for:

- **Сертификаты соответствия** — conformity certificates issued under TR CU / TR EAEU technical regulations. Browse at [hpt.su/documents/cert/](https://hpt.su/documents/cert/index.html).
- **Декларации о соответствии** — declarations of conformity. Browse at [hpt.su/documents/decl/](https://hpt.su/documents/decl/index.html).
- **ОТТС** — Vehicle Type Approval (Одобрение типа транспортного средства) under TR CU 018/2011 — covers automobiles, trucks, buses, trailers, motorcycles. Browse at [hpt.su/documents/otts/](https://hpt.su/documents/otts/index.html).
- **СБКТС** — Vehicle Safety-of-Design Certificate (Свидетельство о безопасности конструкции транспортного средства). Browse at [hpt.su/documents/sbkts/](https://hpt.su/documents/sbkts/index.html).
- **ОТШ** — Chassis Type Approval (Одобрение типа шасси). Browse at [hpt.su/documents/otch/](https://hpt.su/documents/otch/index.html).
- **СУТ** — Notification of Type Approval (Сообщение об утверждении типа транспортного средства). Browse at [hpt.su/documents/sout/](https://hpt.su/documents/sout/index.html).
- **ЗОЕТС / ЗОТШ / ЗОТТС** — single-vehicle / chassis / vehicle evaluation reports issued by accredited testing laboratories. Browse at [hpt.su/documents/zoets/](https://hpt.su/documents/zoets/index.html), [zotch/](https://hpt.su/documents/zotch/index.html), [zotts/](https://hpt.su/documents/zotts/index.html).

Use cases an LLM agent can solve through this server:

- *"Find the type approval for a 2024 KAMAZ-43118 truck."*
- *"What is the status of EAC declaration ЕАЭС N RU Д-RU.HA67.B.12345/24?"*
- *"List recent conformity certificates issued to applicant 'Group GAZ' under scheme 1с."*
- *"Has SBKTS been issued for VIN-derived chassis number XYZ123…?"*
- *"Fetch type-approval notifications (СУТ) issued for manufacturer X."*

## Quick start

### 1. Install

```bash
uv tool install hptsu-mcp
# or
pipx install hptsu-mcp
# or with plain pip
pip install hptsu-mcp
```

### 2. Get an API key

Two tiers exist for MCP:

| Tier | Throttle | Price | Where to get |
|------|---------|-------|--------------|
| **Free MCP** | 50 requests/day | Free, signup required | [hpt.su/user/mcp/](https://hpt.su/user/mcp/) |
| **Paid MCP** | 10 000 requests/day, fulltext + downloads | See pricing | [hpt.su/pricing/](https://hpt.su/pricing/) |

The free tier is plenty for demos and personal use. Upgrade unlocks `fulltext_search`, `download_document_file`, the VIN aggregator, and the higher daily limit.

1. Create an account at [hpt.su/accounts/signup/](https://hpt.su/accounts/signup/).
2. Click "Get free MCP key" in [hpt.su/user/mcp/](https://hpt.su/user/mcp/).
3. (Optional) Upgrade on [hpt.su/pricing/](https://hpt.su/pricing/).

API documentation (Swagger UI): https://hpt.su/api/v1/docs/ · OpenAPI schema: https://hpt.su/api/v1/schema/

### 3. Wire it into your MCP client

#### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "hpt-su": {
      "command": "hptsu-mcp",
      "env": {
        "HPTSU_API_KEY": "your-key-here"
      }
    }
  }
}
```

#### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "hpt-su": {
      "command": "hptsu-mcp",
      "env": { "HPTSU_API_KEY": "your-key-here" }
    }
  }
}
```

#### Cline / Continue / Goose / etc.

Any MCP client that supports stdio transport works the same way — point it at the `hptsu-mcp` executable and pass `HPTSU_API_KEY` in the environment.

#### HTTP / SSE transport

For hosted MCP gateways set `HPTSU_TRANSPORT=streamable-http` (or `sse`) and call the server over the network.

## Tools

### Search & retrieval

| Tool | What it does | Tier |
|------|-------------|------|
| `search_documents` | Cross-registry search by document number (any kind). | Free |
| `get_document` | Fetch a single Document by UUID. | Free |
| `search_certificates` | Conformity certificates — filter by number, applicant, status, scheme, TN VED code. | Free |
| `search_declarations` | Declarations of conformity. | Free |
| `search_type_approvals` | Vehicle (ОТТС) or chassis (ОТШ) type approvals — filter by brand / model / year / applicant. | Free |
| `search_safety_reports` | СБКТС / СУТ / ЗОЕТС / ЗОТШ / ЗОТТС feeds. | Free |
| `search_by_vin` | Aggregated VIN search across ОТТС / СБКТС / ЗОЕТС / ЗОТТС. | Free |
| `fulltext_search` | Full-text search inside PDF bodies. | **Paid** |
| `download_document_file` | Issue a time-limited signed PDF download URL. | **Paid** |

### Reference dictionaries

| Tool | What it does |
|------|-------------|
| `list_brands` | Resolve vehicle brand names. |
| `list_vehicle_models` | Resolve vehicle model names within a brand. |
| `list_test_labs` | Accredited testing laboratories. |
| `list_certification_bodies` | Accredited certification bodies. |
| `list_tnved_codes` | TN VED EAEU classifier (cert/decl context only). |
| `list_registry_kinds` | Enumerate registry kinds the API exposes. |

> **Reference filters accept names, not internal ids.** The search tools resolve
> human-readable values automatically — pass `issuer="НАМИ"`, `brand="KAMAZ"`,
> `eco_class="5"`, `wheel_formula="4x2"`, `axis_count="2"` and the server looks up
> the reference id for you (`issuer` / `brand` also take a numeric id directly).
> An ambiguous name returns the matching candidates to pick from.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `HPTSU_API_KEY` | _required_ | Bearer key from hpt.su cabinet. |
| `HPTSU_BASE_URL` | `https://hpt.su/api/v1` | Override for staging / on-prem deployments. |
| `HPTSU_TRANSPORT` | `stdio` | `stdio` · `streamable-http` · `sse`. |
| `HPTSU_TIMEOUT` | `30.0` | Per-request HTTP timeout (s). |
| `HPTSU_USER_AGENT` | `hptsu-mcp/0.1` | User-Agent header. |
| `HPTSU_LOG_LEVEL` | `INFO` | Standard `logging` levels. |

## SEO description (for catalogue listings)

> **hpt.su MCP server** — let your AI assistant search the hpt.su database of Russian and EAEU vehicle compliance documents directly. Covers type approvals (ОТТС, ОТШ), safety-of-design certificates (СБКТС), conformity certificates and declarations under TR CU / TR EAEU technical regulations, and type-approval notifications (СУТ). Source registry: [https://hpt.su](https://hpt.su). API documentation: [https://hpt.su/api/v1/docs/](https://hpt.su/api/v1/docs/). Read-only, Bearer-key auth, MIT licence.

Russian-language version:

> **MCP-сервер hpt.su** — поиск по крупнейшей базе российских и ЕАЭС документов соответствия для автотранспорта: ОТТС, СБКТС, ОТШ, СУТ (сообщения об утверждении типа), сертификаты соответствия и декларации (ТР ТС / ТР ЕАЭС). Источник: [https://hpt.su](https://hpt.su). REST API: [https://hpt.su/api/v1/docs/](https://hpt.su/api/v1/docs/). Только чтение, Bearer-аутентификация, лицензия MIT.

## Keywords

`mcp` · `model-context-protocol` · `hpt.su` · `vehicle certification` · `type approval` · `ОТТС` · `СБКТС` · `EAEU` · `TR CU 018` · `conformity certificate` · `declaration of conformity` · `сертификат соответствия` · `декларация о соответствии` · `СУТ` · `Russian compliance database` · `AI agent` · `Claude` · `Cursor` · `Cline`

## Development

```bash
git clone https://github.com/hpt-su/hptsu-mcp
cd hptsu-mcp
uv sync --all-groups
uv run pytest
uv run hptsu-mcp           # launch via stdio
```

## License

MIT — see [LICENSE](./LICENSE).

## Links

- Source registry: **https://hpt.su**
- MCP landing page: **https://hpt.su/mcp/**
- MCP key cabinet: **https://hpt.su/user/mcp/**
- API documentation: **https://hpt.su/api/v1/docs/**
- OpenAPI schema: **https://hpt.su/api/v1/schema/**
- Public pricing: **https://hpt.su/pricing/**
- Issue tracker: **https://github.com/hpt-su/hptsu-mcp/issues**
- Contact: **mcp@hpt.su**
- Model Context Protocol: **https://modelcontextprotocol.io**

mcp-name: su.hpt/hptsu-mcp

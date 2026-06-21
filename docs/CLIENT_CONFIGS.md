# Copy-paste configs for popular MCP clients

Все примеры ниже подставляют один и тот же исполняемый `hptsu-mcp` (после `uv tool install hptsu-mcp` / `pipx install hptsu-mcp`). Замени `YOUR-KEY` на ключ из [кабинета hpt.su → API keys](https://hpt.su/cabinet/api-keys/).

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
или `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

```json
{
  "mcpServers": {
    "hpt-su": {
      "command": "hptsu-mcp",
      "env": { "HPTSU_API_KEY": "YOUR-KEY" }
    }
  }
}
```

## Claude Code

```bash
claude mcp add hpt-su -e HPTSU_API_KEY=YOUR-KEY -- hptsu-mcp
```

## Cursor

`~/.cursor/mcp.json` или per-project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "hpt-su": {
      "command": "hptsu-mcp",
      "env": { "HPTSU_API_KEY": "YOUR-KEY" }
    }
  }
}
```

## Cline (VS Code extension)

Через UI: «Configure MCP Servers» → добавить server, command `hptsu-mcp`, env `HPTSU_API_KEY=YOUR-KEY`.
Либо в файле `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "hpt-su": {
      "command": "hptsu-mcp",
      "env": { "HPTSU_API_KEY": "YOUR-KEY" }
    }
  }
}
```

## Continue.dev

В `~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: hpt-su
    command: hptsu-mcp
    env:
      HPTSU_API_KEY: YOUR-KEY
```

## Goose (Block)

`~/.config/goose/config.yaml`:

```yaml
extensions:
  hpt-su:
    type: stdio
    cmd: hptsu-mcp
    envs:
      HPTSU_API_KEY: YOUR-KEY
```

## Cherry Studio / 5ire / LM Studio

В UI настроек MCP добавить server:
- command: `hptsu-mcp`
- env: `HPTSU_API_KEY=YOUR-KEY`

## VS Code + GitHub Copilot

`.vscode/mcp.json`:

```json
{
  "servers": {
    "hpt-su": {
      "type": "stdio",
      "command": "hptsu-mcp",
      "env": { "HPTSU_API_KEY": "YOUR-KEY" }
    }
  }
}
```

## Zed editor

В `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "hpt-su": {
      "command": { "path": "hptsu-mcp", "args": [], "env": { "HPTSU_API_KEY": "YOUR-KEY" } }
    }
  }
}
```

## Hosted MCP (streamable HTTP)

Для self-hosted deploy на VPS:

```bash
HPTSU_TRANSPORT=streamable-http HPTSU_API_KEY=YOUR-KEY hptsu-mcp
```

или Docker:

```bash
docker run --rm -p 8000:8000 \
  -e HPTSU_API_KEY=YOUR-KEY \
  -e HPTSU_TRANSPORT=streamable-http \
  ghcr.io/hpt-su/hptsu-mcp:latest
```

# Connecting the Dota Insights server

The server is free and public, and needs no account, API key or OAuth. It speaks MCP over
**Streamable HTTP** (not SSE) at:

```
https://dota-insights.vercel.app/mcp
```

`https://dota-insights.vercel.app/health` returns `{"status":"ok",...}` when the server is up.
Opening `/mcp` in a browser returns 405, which is expected.

## Claude Code

```bash
claude mcp add --transport http --scope user dota-insights https://dota-insights.vercel.app/mcp
```

Check it with `claude mcp list` or `/mcp` inside a session.

## Claude Desktop and claude.ai

Go to **Settings → Connectors → Add custom connector**, use the name `Dota Insights` and the
URL above, and leave OAuth empty. If custom connectors are not available on the plan, add
this bridge to `claude_desktop_config.json` instead (it needs Node.js 18+), then restart the
app:

```json
{
  "mcpServers": {
    "dota-insights": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://dota-insights.vercel.app/mcp"]
    }
  }
}
```

## Cursor

Add this to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "dota-insights": {
      "url": "https://dota-insights.vercel.app/mcp"
    }
  }
}
```

## VS Code (Copilot agent mode)

Add this to `.vscode/mcp.json`:

```json
{
  "servers": {
    "dota-insights": {
      "type": "http",
      "url": "https://dota-insights.vercel.app/mcp"
    }
  }
}
```

## Windsurf

Add this to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "dota-insights": {
      "serverUrl": "https://dota-insights.vercel.app/mcp"
    }
  }
}
```

## Other clients

Use any client that supports remote Streamable HTTP servers with the URL above. Clients that
only run local commands can use `npx -y mcp-remote https://dota-insights.vercel.app/mcp`.

Some clients cap the total number of tools across servers (around 40). If the client warns,
disable the Dota Insights tools that are not needed.

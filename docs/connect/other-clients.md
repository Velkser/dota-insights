# Other MCP clients

Any client that supports remote MCP servers over **Streamable HTTP** (sometimes labelled "HTTP" or
"streamable-http") takes this URL; no headers or auth are needed:

```
https://dota-insights.vercel.app/mcp
```

The server does not offer the older SSE transport, so do not pick `sse`.

## VS Code (GitHub Copilot agent mode)

Run **MCP: Add Server** from the command palette and choose HTTP, or create `.vscode/mcp.json` in
the workspace (or edit the user-level `mcp.json` via **MCP: Open User Configuration**):

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

Then switch Copilot Chat to *Agent* mode and enable the server's tools in the tools picker.

## Windsurf

Add to `~/.codeium/windsurf/mcp_config.json` (or Windsurf Settings → Cascade → MCP servers):

```json
{
  "mcpServers": {
    "dota-insights": {
      "serverUrl": "https://dota-insights.vercel.app/mcp"
    }
  }
}
```

Windsurf caps the total number of enabled tools across servers; disable the ones you do not need
if it warns.

## Clients that only run local (stdio) servers: `mcp-remote`

[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) is a small Node.js bridge that the client
starts as a local command and that forwards to the remote server. It needs Node.js 18+.

Command line:

```bash
npx -y mcp-remote https://dota-insights.vercel.app/mcp
```

Typical JSON config (the `mcpServers` shape most clients use):

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

On Windows, if the client cannot find `npx`, use `"command": "cmd"` with
`"args": ["/c", "npx", "-y", "mcp-remote", "https://dota-insights.vercel.app/mcp"]`.

## Your own code

With the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) or
[FastMCP](https://gofastmcp.com):

```python
import asyncio
from fastmcp import Client


async def main():
    async with Client("https://dota-insights.vercel.app/mcp") as client:
        result = await client.call_tool("get_hero_overview", {"hero": "Pudge"})
        print(result.structured_content)


asyncio.run(main())
```

Or raw JSON-RPC (stateless; no session id needed):

```bash
curl -s https://dota-insights.vercel.app/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Troubleshooting

- `https://dota-insights.vercel.app/health` must answer `{"status":"ok",...}`.
- 405 on `GET /mcp` is expected; the endpoint takes POST only.
- 406 Not Acceptable: the request must send `Accept: application/json, text/event-stream`.
- A tool result with `status: "error"` and a retry hint means a data source or this server's
  per-client rate limit was hit; wait for the time the hint gives.

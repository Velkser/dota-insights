# Connecting to Dota Insights

The public server speaks MCP over **Streamable HTTP** at:

```
https://dota-insights.vercel.app/mcp
```

No account, API key or OAuth is needed. Pick your client:

- [Claude Code](claude-code.md)
- [Claude Desktop (and claude.ai)](claude-desktop.md)
- [Cursor](cursor.md)
- [VS Code, Windsurf and other MCP clients, or the `mcp-remote` bridge](other-clients.md)

The landing page at <https://dota-insights.vercel.app> has the same snippets in English and Russian.

## Checks that apply to every client

- `https://dota-insights.vercel.app/health` should return `{"status":"ok",...}`. If it does not,
  the server is down; try again later.
- Opening `/mcp` in a browser returns **405 Method Not Allowed**. That is expected: the endpoint
  only accepts MCP POST requests. The server is stateless and answers with JSON, so it does not
  offer the older SSE transport; configure the client for HTTP / Streamable HTTP, not `sse`.
- Tool results can say `status: "error"` with a hint when OpenDota or STRATZ is rate-limited or
  when you exceed this server's per-client limits (30 tool calls a minute, 1,000 a day; fewer for
  heavy tools). Wait for the time the hint gives.
- `https://dota-insights.vercel.app/status` shows source quota use and data freshness.
- Player tools need a public Dota 2 profile ("Expose Public Match Data"). Ask with your 32-bit
  account ID (the number in your OpenDota or STRATZ profile URL).

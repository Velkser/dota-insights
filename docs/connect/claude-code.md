# Claude Code

## Add the server

```bash
claude mcp add --transport http dota-insights https://dota-insights.vercel.app/mcp
```

This adds it for the current project only (scope `local`). Other scopes:

```bash
# every project on this machine
claude mcp add --transport http --scope user dota-insights https://dota-insights.vercel.app/mcp

# shared with everyone who clones the repository (writes .mcp.json)
claude mcp add --transport http --scope project dota-insights https://dota-insights.vercel.app/mcp
```

The project scope writes this `.mcp.json`, which you can also create by hand:

```json
{
  "mcpServers": {
    "dota-insights": {
      "type": "http",
      "url": "https://dota-insights.vercel.app/mcp"
    }
  }
}
```

## Check it

```bash
claude mcp list
claude mcp get dota-insights
```

Inside a session, `/mcp` lists the server, its status and its tools. Then just ask, for example
"What counters Huskar in Ancient?" or "Разбери мой последний матч, аккаунт 56939869".

## Remove it

```bash
claude mcp remove dota-insights
```

Add `--scope user` or `--scope project` if you added it with that scope.

## Troubleshooting

- **`Failed to connect` / status `failed` in `/mcp`.** Check
  `curl https://dota-insights.vercel.app/health`. If that works, make sure the URL ends in `/mcp`
  and the transport is `http` (not `sse` or `stdio`).
- **The server is listed but its tools are missing.** Restart Claude Code after adding the server;
  project-scope servers from `.mcp.json` must be approved the first time (Claude Code asks).
- **Claude answers without calling the tools.** Name the data you want ("use dota-insights",
  "check the stats") or ask about a specific hero, item, match or account.
- **A tool returns `error` with a retry hint.** A data source or this server's rate limit was hit;
  wait for the time the hint gives. See [the shared checks](README.md#checks-that-apply-to-every-client).
- **Behind a corporate proxy.** Claude Code honours `HTTPS_PROXY`; the server must be reachable
  from your network.

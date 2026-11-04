# Cursor

## Add the server

Either open **Cursor Settings → MCP** (called *Tools & Integrations* in newer versions) and click
**Add new MCP server** / **New MCP Server**, or edit the config file directly:

- global, every project: `~/.cursor/mcp.json`
- one project: `.cursor/mcp.json` in the project root

```json
{
  "mcpServers": {
    "dota-insights": {
      "url": "https://dota-insights.vercel.app/mcp"
    }
  }
}
```

Cursor detects Streamable HTTP from the `url` field. Save, then check the MCP settings page: the
server should show a green dot and its list of tools.

Use it from the Agent chat (tools are not used in plain Ask/Edit modes in older versions): for
example "Which carries are the strongest this patch?".

## Without remote HTTP support

Older Cursor versions only start local commands. Use the `mcp-remote` bridge (needs Node.js 18+):

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

## Troubleshooting

- **Red dot / "Client closed".** Check `https://dota-insights.vercel.app/health`, make sure the URL
  ends in `/mcp`, then toggle the server off and on or restart Cursor. Output → *MCP Logs* (or
  *Cursor MCP*) shows the error.
- **Too many tools.** The server has 58 tools. Some Cursor versions pass only a limited number of
  tools (around 40, across all servers) to the model and warn about it. Turn off the tools you do
  not need on the MCP settings page, or other servers you are not using.
- **The agent does not call the tools.** Mention the data ("use the Dota stats tools") and make
  sure the server is enabled for the chat.
- **A tool returns `error` with a retry hint.** A data source or this server's rate limit was hit;
  wait for the time the hint gives.

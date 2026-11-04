# Claude Desktop (and claude.ai)

There are two ways in. Use the first if your Claude plan and app version offer custom
connectors; otherwise use the bridge.

## Option 1: custom connector (remote, no install)

1. Open **Settings → Connectors** (in Claude Desktop or on claude.ai; the list syncs between them).
2. Click **Add custom connector**.
3. Name: `Dota Insights`. URL:

   ```
   https://dota-insights.vercel.app/mcp
   ```

4. Leave the OAuth fields empty (the server needs no sign-in) and click **Add**.
5. In a chat, open the tools menu (the sliders / "+" button under the message box) and make sure
   Dota Insights is enabled.

On Team and Enterprise plans an owner may have to add the connector for the organization first
(organization settings → Connectors). Which plans allow custom connectors, and how many, is up to
Anthropic and changes over time; if you do not see **Add custom connector**, use option 2.

## Option 2: `mcp-remote` bridge (local config)

This runs a small local process that forwards Claude Desktop's stdio connection to the remote
server. It needs [Node.js](https://nodejs.org) 18 or newer (`node --version`).

1. Open **Settings → Developer → Edit Config**. This opens `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add the server (merge with any `mcpServers` you already have):

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

3. Quit Claude Desktop completely (not just close the window) and start it again.
4. The tools menu in a chat should list `dota-insights`.

## Troubleshooting

- **Connector shows an error or "couldn't connect".** Check
  `https://dota-insights.vercel.app/health` in a browser. Make sure the URL ends in `/mcp`.
  Opening `/mcp` itself in a browser gives 405; that is normal.
- **Bridge: server not listed or "Server disconnected".** Look at the logs (Settings → Developer,
  or `~/Library/Logs/Claude/mcp-server-dota-insights.log` on macOS,
  `%APPDATA%\Claude\logs\` on Windows). Common causes:
  - `npx` not found: install Node.js, or use the full path to `npx` as `command`
    (`which npx` on macOS/Linux).
  - Windows: if `npx` still fails, use `"command": "cmd"` and
    `"args": ["/c", "npx", "-y", "mcp-remote", "https://dota-insights.vercel.app/mcp"]`.
  - Invalid JSON (a missing comma or brace) makes Claude Desktop ignore the whole file.
- **Claude answers without the tools.** Check the connector is enabled for the chat, then ask about
  a concrete hero, item, match or account.
- **A tool returns `error` with a retry hint.** A data source or this server's rate limit was hit;
  wait for the time the hint gives.

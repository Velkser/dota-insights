# Dota Insights skill

Teaches an AI agent to answer Dota 2 questions with data from the free, public
[Dota Insights](https://dota-insights.vercel.app) MCP server. The server has 60 tools covering
heroes, items, builds, matchups and drafts, match analysis, players, the pro scene, patches and
ad-hoc statistics. Every answer includes the sample size, patch and caveats.

## What it does

- Picks the right tool for the question (a hero's strength, counters, a draft pick, a build,
  item timing, a match ID, an account ID, a tournament).
- Reads the result envelope and reports the sample size and patch. It says when data is
  insufficient instead of presenting noise as fact, and relays retry hints when a source is
  down.
- Answers in the user's language. Hero and item names work in English and Russian.

## Setup

Add the server to your agent once. It needs no account or key. For example, in Claude Code:

```bash
claude mcp add --transport http --scope user dota-insights https://dota-insights.vercel.app/mcp
```

`references/connect.md` covers Claude Desktop, claude.ai, Cursor, VS Code, Windsurf and others.

## Files

| File | Contents |
| --- | --- |
| `SKILL.md` | When to use the skill, how to pick tools, read results and format answers |
| `references/tools.md` | Every tool, with its purpose, required arguments and an example question |
| `references/reading-results.md` | The result envelope, common caveats, error handling |
| `references/connect.md` | Connection snippets per client |
| `examples/questions.md` | Sample questions with the calls that answer them |

## Privacy and network

The skill contains no code and no credentials. The agent only talks to the Dota Insights MCP
server you connected. Player tools use public Dota 2 profiles only. See the server's
[privacy policy](https://dota-insights.vercel.app/privacy).

Source code: <https://github.com/Velkser/dota-insights>. Data comes from OpenDota and STRATZ.

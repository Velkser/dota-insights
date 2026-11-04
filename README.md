# Dota Insights

AI-accessible analytics for Dota 2. A public MCP server you connect to Claude Code, Claude Desktop, Cursor
and other MCP clients, so your assistant can answer questions about heroes, items, economy and the meta
from real match data.

> Status: early MVP. The server runs, but the analytics tools are not implemented yet.

## Planned tools

| Tool | Purpose |
| --- | --- |
| `compare_heroes` | Compare heroes by metric, rank, position and patch |
| `get_farming_stats` | GPM, XPM, net worth and farm over time |
| `analyze_item` | Item stats, purchase timings and outcomes |
| `analyze_death` | Gold, time and net worth lost after a death |
| `find_meta_trends` | Rising and falling heroes |
| `analyze_match` | Statistical breakdown of a single match |
| `get_player_stats` | Player stats, where the source allows it |

Every result includes the sample size, patch, filters and caveats.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
uv run dota-insights          # serves MCP over Streamable HTTP at http://127.0.0.1:8000/mcp
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Connect a local server to Claude Code:

```bash
claude mcp add --transport http dota-insights http://127.0.0.1:8000/mcp
```

## Layout

```
src/dota_insights/
  mcp/         MCP server and tools
  sources/     OpenDota, STRATZ and Steam Web API clients
  collector/   scheduled data collection jobs
  db/          PostgreSQL schema and access
  analytics/   metric calculations and sample-size checks
  telegram/    insight generator and channel publishing
  config.py    settings from DOTA_INSIGHTS_* environment variables
```

# Writing an MCP tool

Conventions every tool in `src/dota_insights/mcp/tools/` follows.

## Layout

- One domain per module: `tools/<domain>.py` with a `register(mcp)` function that defines the
  domain's tools with `@mcp.tool`. Modules are discovered automatically; there is no list to edit.
- Data fetching and calculations go in `src/dota_insights/analytics/<domain>.py`, not in the
  tool function. The tool parses arguments, calls analytics, and wraps the result.
- Tests: `tests/test_<domain>.py`. Recorded source responses go in `tests/fixtures/<domain>/`.
  Tests never call live APIs.

## Inputs

- Hero and item names: accept what a person would type (`"PA"`, `"Фантомка"`, `"bkb"`) and
  resolve with `dota_insights.reference.resolve_hero` / `resolve_item`. When the resolution is
  ambiguous or empty, return `error(...)` listing the candidates instead of guessing.
- Filters: use `dota_insights.models.Filters` and its enums (`Position`, `RankBracket`,
  `GameMode`, `Outcome`) rather than inventing parameter names.
- Every parameter gets a `Field(description=...)` written for an AI caller.

## Outputs

- Always return `dota_insights.mcp.envelope.ToolResult` built with `ok(...)`,
  `insufficient_data(...)` or `error(...)`.
- Fill `sample_size`, `filters`, `period` or patch, `definitions` for every metric,
  `confidence` where a rate or median is reported (`analytics.stats.wilson_interval`,
  `bootstrap_ci`), `caveats`, `source` and `data_freshness`.
- Below `filters.min_sample`, return `insufficient_data` with a hint on how to widen the query.
- Win rate of an item or a build is correlational; say so in `caveats`.

## Sources

- Aggregate questions (hero rates, matchups, item timings, GPM/XPM percentiles) are answered from
  the collector's database first: `await dota_insights.db.access.read(fn, ...)` with a reader from
  `db/readers.py`, falling back to the APIs when it returns None or the sample is below
  `min_sample`. Such envelopes list `"dota-insights-db"` first in `source`, then the upstream
  origin; see [collector.md](collector.md#tools-reading-aggregates).
- Get clients with `dota_insights.sources.get_opendota()` and `get_stratz()` so the server
  shares one rate limiter per source. Tests swap them with `sources.use_clients(...)` and an
  `httpx.MockTransport`.
- Wrap source calls in `@cached(ttl=...)` from `dota_insights.cache` (`TTL_AGGREGATE` for hero
  and item stats, `TTL_REFERENCE` for constants, `TTL_MATCH` for finished matches).
- OpenDota free tier: 60 calls/min, 3,000/day, no paid key. Prefer STRATZ for aggregates by
  position and rank (STRATZ also separates Immortal, OpenDota does not), and OpenDota for
  matches, players and pro data. See `docs/research/`.
- STRATZ requires a link back to stratz.com: put `"stratz"` in `source` and the tool's
  caveats mention it where its data is used.

## Errors and timeouts

- `ErrorEnvelopeMiddleware` (`dota_insights.mcp.errors`) wraps every tool call. A source exception
  that escapes a tool (OpenDota/STRATZ rate limit, auth, outage, query error, HTTP timeout) becomes an
  `error` envelope with a retry hint; any other exception becomes a generic error that names only its
  type. Tools do not need a catch-all `try`.
- Catch a source error only to keep context such as `filters` or to fall back to another source;
  then return `source_error(exc, filters=..., source=...)`, or use `retry_hint(exc)` when wording the
  message yourself. An `error(...)` without a hint gets a generic one from the middleware, but write
  your own when you know what the caller should change.
- Every call is cancelled after `DEFAULT_TOOL_TIMEOUT` seconds. A tool that fans out to many source
  requests on a cold cache goes in `TOOL_TIMEOUTS` with `HEAVY_TOOL_TIMEOUT`.
- `insufficient_data(reason, hint)` requires a non-empty hint naming the filter to widen.

## Docs

- After adding or changing a tool, add an example question to `EXAMPLES` in
  `scripts/gen_tool_docs.py` and run `uv run python scripts/gen_tool_docs.py`. `tests/test_docs.py`
  fails when README.md is out of date or a tool has no example.
- MCP prompts live in `dota_insights/mcp/prompts.py`; name tools in backticks so the tests can check
  they exist.

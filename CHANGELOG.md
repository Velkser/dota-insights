# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/) and has not had a tagged release yet.

## [Unreleased]

### Added

- Vercel deployment: stateless ASGI entry point `dota_insights.mcp.asgi:app` (loaded by the
  root `app.py`), and
  `DATABASE_URL` from the Neon integration as a fallback for `DOTA_INSIGHTS_DATABASE_URL`.
- Central error handling: `ErrorEnvelopeMiddleware` turns OpenDota and STRATZ rate limits, auth
  failures, outages, rejected queries, timeouts and unexpected exceptions into `error` envelopes with
  a retry hint, for every tool including ones added later. Internal exception details are not sent to
  clients.
- Per-tool time limits: 60 seconds by default, 150 seconds for tools that aggregate over many heroes,
  matches or patches (`dota_insights.mcp.errors.TOOL_TIMEOUTS`).
- MCP prompts: `analyze_last_match`, `pick_hero_for_style`, `patch_meta_overview` and
  `check_dota_myth`, each steering the client through the right tools.
- README: tool reference (purpose, key parameters, example question) generated from the registered
  tools by `scripts/gen_tool_docs.py`, with a test that fails when it is out of date; data
  limitations; how to connect from Claude Code and Claude Desktop.
- This changelog.

### Changed

- `insufficient_data` envelopes must carry a non-empty `hint` on how to widen the filters.
- Tool-call logs record the envelope status (`ok`, `insufficient_data`, `error`).
- Source errors caught inside tools now use the same messages and retry hints as the middleware
  (`source_error`, `retry_hint`); several tools previously returned errors without a hint.

## Tools added before this changelog

- Shared foundation: result envelope, filters, OpenDota and STRATZ clients with rate limiting and
  retries, TTL cache, reference data and name resolver, structured logging, `/health`.
- Heroes, matchups and drafts, single matches, items, builds, farming, trends, players, pro scene,
  general statistics and reference lookup (48 tools).

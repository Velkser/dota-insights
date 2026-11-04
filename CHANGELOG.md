# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/) and has not had a tagged release yet.

## [Unreleased]

### Changed

- Insight and Weekly Meta Report drafts name heroes in Russian ("Антимаг", "Пудж") through
  the shared RU name table, falling back to English; draft file names stay English slugs.

### Added

- Russian hero and item names (p9-11): `reference/data/names_ru.json` lists the usual Russian
  name and community nicknames for all 127 heroes and 114 common items ("Пудж", "Инвокер", "АМ",
  "БКБ", "Блинк"). Every hero and item argument accepts them, case-insensitively and with ё/е
  treated alike; a test checks that each Russian name and alias resolves to exactly one hero or
  item. Every ok envelope carries `names_ru` (English name -> Russian name for the heroes and
  items in its result), and `lookup_reference`, `heroes://list` and `items://list` records gain
  `name_ru`. Hero and item parameter descriptions say Russian spellings are accepted.
- Insight Generator and the Weekly Meta Report rubric (`dota_insights.insights`,
  `dota-insights insights weekly`): week-over-week win-rate and pick-rate risers and fallers per
  rank bracket from the collector's matches (or OpenDota `/heroStats` snapshots), anomalies flagged
  by two-proportion z-tests with a Benjamini-Hochberg correction and a minimum change, Russian post
  drafts that always state the sample and patch, and PNG charts. Output is a dated folder of
  Markdown, PNG and JSON for manual moderation; nothing is published. The weekly `Insights`
  workflow (Mondays, and `workflow_dispatch`) uploads it as an artifact.
- Landing page at `/` and privacy policy at `/privacy`: self-contained HTML (no JavaScript),
  light and dark, English and Russian (`?lang=`, else `Accept-Language`). The landing page's tool
  list is rendered from the registered tools (`dota_insights.mcp.catalog`, shared with the README
  generator).
- Connection guides for Claude Code, Claude Desktop, Cursor and other clients
  (`docs/connect/`), and a privacy policy template (`docs/privacy.md`) for the owner to complete.
- Usage analytics: one `usage_events` row per tool call (argument shape without player ids,
  status, error kind, latency, sources, cache hit, client name, weekly-salted client hash; no
  raw IPs), queued in Redis and flushed by the collector's new `usage` job, which also applies a
  90-day retention. `dota-insights usage report` and the token-protected `GET /admin/usage`.
- `report_wrong_answer` and `suggest_feature` tools: wrong answers (linked to the client's
  latest call of the tool) and unmet questions, stored in `usage_feedback`, rate-limited per
  client and listed in the usage report.
- Player privacy: an opt-out list (`player_opt_outs`, `dota-insights privacy opt-out|opt-in|list`)
  that player tools refuse and match tools mask; anonymous players are masked too.
- Evals (`evals/`): 63 English and Russian player questions with expected tools, argument
  matchers and answer checks; a harness that lets a Claude model answer them with the in-process
  server or a remote URL and scores tool choice, arguments, envelope status and answer caveats;
  JSON and Markdown reports; an opt-in `Live evals` workflow (`workflow_dispatch`).
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

- A hero or item query that is already an alias as typed is no longer also read in the other
  keyboard layout, so "АМ", "ВК", "ЦК", "ДС" and "ТП" resolve instead of coming back ambiguous
  ("ам" is "fv" on a QWERTY layout). Russian slang moved from `aliases.py` to `names_ru.json`.
- `insufficient_data` envelopes must carry a non-empty `hint` on how to widen the filters.
- Tool-call logs record the envelope status (`ok`, `insufficient_data`, `error`).
- Source errors caught inside tools now use the same messages and retry hints as the middleware
  (`source_error`, `retry_hint`); several tools previously returned errors without a hint.

## Tools added before this changelog

- Shared foundation: result envelope, filters, OpenDota and STRATZ clients with rate limiting and
  retries, TTL cache, reference data and name resolver, structured logging, `/health`.
- Heroes, matchups and drafts, single matches, items, builds, farming, trends, players, pro scene,
  general statistics and reference lookup (48 tools).

# Collector

Tracker tasks: p2-01 (jobs), p2-03 (aggregates), p2-09 (patch versioning). Code:
`src/dota_insights/collector/`, read helpers in `src/dota_insights/db/aggregates.py`, schema in
migration `0002` (see [db-schema.md](db-schema.md) for the tables it extends).

The collector is a separate process (`dota-insights collect scheduler`, the `collector` service
in `docker-compose.yml`) that fills PostgreSQL from OpenDota and STRATZ and keeps the aggregate
tables current. MCP tools still call the APIs live; wiring them to read aggregates first is the
next step (see [What remains](#what-remains)).

```
OpenDota publicMatches ─┐                      ┌─ patch_bracket_totals
OpenDota explorer ──────┼─> matches ───────────┤  hero_draft_aggregates
  (draft level)         │   match_players      │  hero_matchup_aggregates   draft level
                        │   match_events ──────┤  hero_aggregates
STRATZ match(id) ───────┘   (detail level)     │  hero_stat_histograms      detail level
                                               └─ item_timing_aggregates
OpenDota /constants, /heroStats ──> patches, reference_snapshots, hero_stats_daily
```

## Data levels

OpenDota's free tier cannot pay one `/matches/{id}` call per match (3,000/day), so matches are
collected in two levels:

| Level | Source | Per call | What we get |
| --- | --- | --- | --- |
| Draft | OpenDota Explorer `public_matches` (up to 2,000 rows); `publicMatches` only to seed | up to 2,000 matches | start, duration, `avg_rank_tier`, ten hero ids, result |
| Detail | STRATZ aliased `match(id:)` (10 matches per request) | 10 matches | position, lane, KDA, GPM/XPM, net worth, damage, final items, purchase times |

Draft rows are `match_players` rows with NULL stats; enrichment overwrites them and sets
`matches.has_details`. Draft-level aggregates (win rate, pick rate, matchups, synergies) use every
match; detail-level aggregates (per position, percentiles, item timings) use the enriched
subset and report their own denominator (`patch_bracket_totals.detailed_games`).

**Why Explorer, not `publicMatches`.** Measured 2026-11-04: the newest `publicMatches` page for
`min_rank=50&max_rank=59` (the bracket filter works) had 90 unranked lobby-0 matches, 6 bot
matches and only **2 Ranked** of 100. One Explorer query
(`lobby_type = 7 AND avg_rank_tier BETWEEN 50 AND 59`, ascending from a match id) returned 1,810
Ranked Legend matches in 0.3 s. Explorer reads the same `public_matches` table, so freshness is the
same.

Only ranked matches (`lobby_type = 7`) with a duration and ten heroes are kept. Brackets come
from `avg_rank_tier` (tens digit); Immortal is not separable there (OpenDota caps the average at
75), so the default brackets are 1–7.

**STRATZ notes (checked live 2026-11-04 with a Default token).** `matches(ids: [...])` answers
"User is not an admin"; aliased `match(id:)` fields in one document work and count as one call.
Public-match `pickBans` entries have `heroId: null` for bans, so ban rate is only known where a
source exposes bans (pro matches, OpenDota `/heroStats` `pro_ban`). The aggregates keep a
separate denominator (`ban_sample_games`) so tools can say when ban rate is unknown.

## Jobs

| Job | Every | Calls per run | Writes |
| --- | --- | --- | --- |
| `patches` | 6 h | 1 (OpenDota) | `patches`; re-labels matches, marks stale aggregates |
| `constants` | 24 h | 6 (OpenDota) | `reference_snapshots` (a row only when a payload's hash changes) |
| `hero-stats` | 24 h | 1 (OpenDota) | `hero_stats_daily` (pub brackets 1–8, pro with bans, turbo) |
| `public-matches` | 10 min | paced (OpenDota Explorer; 1 `publicMatches` page to seed) | draft matches, `collector_cursors`, `collector_gaps` |
| `backfill` | 30 min | paced (OpenDota Explorer) | draft matches inside gaps |
| `enrich` | 15 min | paced (STRATZ) | detail level for matches 1 h – 14 days old (retries at 3 h, 12 h) |
| `aggregate` | 10 min | 0 | all aggregate tables |

Run one with `dota-insights collect <job>`; `collect status` shows budgets, last runs, stale
patches and open gaps; `collect add-gap --bracket 5 --low X --high Y` queues a historical id range
for backfill.

### Idempotency and gaps

- Matches and players are inserted with `ON CONFLICT DO NOTHING` on their primary keys, and only
  newly inserted matches get draft rows, so overlapping pages are harmless.
- `public-matches` keeps one cursor per bracket stream (`public_matches:<bracket>` in
  `collector_cursors`) and reads ascending from it with Explorer, up to
  `collector_explorer_rows_per_call` rows per call, until a short page says it has caught up. If
  the allowance ends first, the cursor just lags; the next run continues from it, so nothing is
  skipped.
- **Settle window.** Match ids are assigned at game start but rows appear when the game ends, so a
  70-minute game shows up after shorter games with higher ids. The forward query only takes matches
  that started at least 2 h ago (`start_time < now - SETTLE`; cheap as an extra filter on an id
  range, although `start_time` alone times out on Explorer), so the cursor never passes a game that
  is still running. Data is therefore 2 h behind live; tools that need fresher numbers keep using
  the live APIs.
- **Seeding and outages.** A stream without a cursor is seeded from one `publicMatches` page (its
  rows are stored, the cursor starts just below the oldest of them). A stream more than 6 h behind
  (`MAX_LAG`, after an outage) is re-seeded the same way and the skipped range goes to
  `collector_gaps`, so fresh data resumes at once and the backlog is filled with spare budget.
- `backfill` fills gaps newest first with Explorer, descending inside the gap, moving its upper
  bound down after each chunk and setting `filled_at` when a chunk comes back short.
- Enrichment counts lookups that found no parsed match in `matches.detail_attempts` and retries
  when the match is 3 h and then 12 h old; after that the match stays draft-only. In a live check
  3 of 10 matches about 1–2 h old were parsed on STRATZ.
- Before inserting into a month without a partition, the loader calls
  `ensure_month_partitions()` (PostgreSQL only).

### Budgets

Every call is reserved first in `collector_api_usage` (per source, UTC day and hour windows), so
restarts don't reset counts and the collector stops for the window when a limit is hit
(`BudgetExceededError` → run recorded as `skipped`).

| Source | Provider quota | Collector default | Setting |
| --- | --- | --- | --- |
| OpenDota | 60/min, 3,000/day per IP | 20/min, **1,800/day** | `DOTA_INSIGHTS_COLLECTOR_OPENDOTA_DAILY_BUDGET`, `..._OPENDOTA_PER_MINUTE` |
| STRATZ | 250/min, 2,000/h, 10,000/day | 100/min, 1,000/h, 5,000/day | `..._STRATZ_DAILY_BUDGET`, `..._STRATZ_HOURLY_BUDGET`, `..._STRATZ_PER_MINUTE` |

The remaining ~1,200 OpenDota calls/day and the per-minute headroom are for the MCP server's live
tool calls, which share the IP. Paced jobs ask `ApiBudget.pace()` for their share: calls left in the
window divided by the runs still to come before it resets, so a day's budget is spread over the
day instead of being spent at midnight. Collector clients retry at most twice (a retry is a call
the budget can't see). With the defaults, a 10-minute `public-matches` run early in the day may spend
1,800 / 144 ≈ 12 calls; once caught up it needs one Explorer call per bracket (7 per run, ~1,000 a
day), which covers ranked volume (~2,000 Legend matches per 15 min at peak, measured). `backfill`
draws on what is left of the same OpenDota budget. Enrichment can reach ~5,000 calls × 10
= 50k matches/day if the draft volume is there.

### Scheduler

An asyncio loop (`collector/scheduler.py`, no extra dependency) runs due jobs one at a time in
priority order every 30 s. A job is due when its last run in `collector_job_runs` started at
least one interval ago (5 min after a failure). Failures are logged and recorded; they never stop
the loop. SIGTERM/SIGINT stop it between jobs. **Run one scheduler per database**: budgets are
checked and incremented in separate transactions, so two schedulers could overspend.

## Aggregates (p2-03)

All aggregate tables hold additive counters. Each `aggregate` run folds pending matches in batches
(`DOTA_INSIGHTS_COLLECTOR_AGGREGATE_BATCH_SIZE`, default 2,000); each batch adds its deltas with
`INSERT ... ON CONFLICT DO UPDATE SET x = x + excluded.x` and stamps the matches in the same
transaction, so a batch is counted fully or not at all.

| Table | Key | From | Level |
| --- | --- | --- | --- |
| `patch_bracket_totals` | patch, bracket | `games`/`wins` (radiant) per match; `detailed_games`, `ban_sample_games` | both |
| `hero_draft_aggregates` | patch, bracket, hero | games, wins; `bans` from known bans | draft (+ bans) |
| `hero_matchup_aggregates` | patch, bracket, hero, `with`/`vs`, other hero | games, wins of `hero` | draft |
| `hero_aggregates` | patch, bracket, position, hero | games, wins, KDA/GPM/XPM/LH/NW/damage/healing/duration sums | detail |
| `hero_stat_histograms` | patch, bracket, position, hero, stat, bucket | games, wins per GPM (25), XPM (25), net worth (1,000) bin | detail |
| `item_timing_aggregates` | patch, bracket, hero, position, item, minute | first purchase per player and item; minute −1 = before the horn | detail |

Markers: `matches.aggregated_at` (draft level) and `matches.details_aggregated_at` (detail level),
with partial indexes on the pending rows. Derived numbers (read helpers in `db/aggregates.py`):

- win rate = wins / games; pick rate = hero games / `patch_bracket_totals.games` (per match, so a
  hero's pick rate is at most 1).
- ban rate = bans / `ban_sample_games`, None while no match in the filter has known bans.
- averages = sums / `hero_aggregates.games`; percentiles (p25/p50/p75) interpolated from the
  histogram, mergeable across brackets and positions.

`hero_patch_stats(session, hero_id, patch_id, rank_brackets=, positions=)` returns all of these
with sample sizes; `compare_hero_patches()` gives before/after/delta for two patches.

## Patch versioning (p2-09)

- `patches` is filled from OpenDota `/constants/patch` (`patches` and `constants` jobs) and seeded
  from the bundled `reference/data/patches.json` when empty, so ingestion never waits on an API.
  Patch ids are OpenDota's (7.41 = 60). OpenDota lists major patches only (7.41, not 7.41a), so a
  letter patch does not split aggregates.
- A match belongs to the latest patch released at or before its `start_time`. When the patch list
  changes (new patch, moved date), `reassign_matches()` moves matches (and the denormalised
  `match_players.patch_id`) whose label disagrees, and every patch whose match set changed is
  marked stale in `aggregate_status` with a reason.
- The `aggregate` job rebuilds stale patches first: delete that patch's aggregate rows, clear the
  markers on its matches, re-fold them, mark it fresh (`rebuilt_at`). Incremental batches skip stale
  patches meanwhile. A rebuild runs in one transaction, so readers see either the old or the new
  numbers; `hero_patch_stats(...).stale` tells tools when numbers are being rebuilt.
- Before/after: `compare_hero_patches(session, hero_id)` defaults to the latest two patches.
  Aggregates of past patches are kept indefinitely (db-schema.md, Retention).

## Testing

`tests/test_collector.py` runs every job, the scheduler and the aggregation on SQLite (the models
map PostgreSQL arrays and JSONB to JSON there) with `httpx.MockTransport` and a recorded STRATZ
match (`tests/fixtures/collector/stratz_match.json`). `tests/test_db_models.py` has a PostgreSQL
round trip (migrations, on-demand partitions, rebuild after a patch split) that runs when
`DOTA_INSIGHTS_TEST_DATABASE_URL` points at an empty scratch database.

## What remains

- **Tools reading aggregates.** `compare_heroes`, `find_meta_trends`, `get_farming_stats`,
  `analyze_item` and the matchup tools still call the APIs live. Wire them to `db/aggregates.py`
  (and add readers for matchups and item timings) with a live fallback while samples are small.
- **Retention job** (drop old monthly partitions, db-schema.md) is not implemented.
- **Immortal bracket**: needs per-player `rank_tier` from match details; not collected.
- **Replay-level data** (`player_timeseries`, deaths, wards) is not collected; STRATZ purchases are
  the only events stored.
- **Multiple collectors**: budgets assume one scheduler; a PostgreSQL advisory lock would make a
  second instance safe.

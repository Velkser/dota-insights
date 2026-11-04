# Database schema

PostgreSQL (12+; developed against 14). Models: `src/dota_insights/db/models.py`. Migrations:
`migrations/` (Alembic), initial revision `0001`. Connection: `DOTA_INSIGHTS_DATABASE_URL`
(`postgresql://` URLs are switched to the psycopg 3 driver by `db/session.py`).

```bash
uv run alembic upgrade head          # apply
uv run alembic upgrade head --sql    # print the SQL without a database
uv run alembic -x url=postgresql://localhost/other upgrade head
```

The schema has two layers:

- **Raw per-match data** — `matches`, `match_players`, `player_timeseries`, `match_events`.
  Written by the collector, large, partitioned by month, subject to retention.
- **Aggregates** — `patch_bracket_totals`, `hero_aggregates`, `hero_matchup_aggregates`,
  `item_timing_aggregates`. Rebuilt from the raw layer by refresh jobs, small, kept forever.
  MCP tools read these first and fall back to raw tables only for match-level questions.

## Conventions

- Times are `timestamptz`; game times inside a match are integer seconds from the horn
  (negative before it).
- Hero, item, game mode, lobby type and region ids are the numeric ids used by OpenDota/Steam;
  names come from reference data, not from this database.
- `slot` is 0-9 (0-4 Radiant, 5-9 Dire), normalised from OpenDota's `player_slot` (0-4, 128-132).
- `position` is 1 (carry) .. 5 (hard support). `lane` and `lane_role` keep OpenDota's meaning
  (`lane`: 1 bot, 2 mid, 3 top, 4/5 jungle; `lane_role`: 1 safe, 2 mid, 3 off, 4 jungle).
- `rank_bracket` in aggregates is the medal: 1 Herald .. 8 Immortal, 0 = unknown. It is the tens
  digit of OpenDota's `rank_tier` (`avg_rank_tier` 54 = Legend 4 → bracket 5).
- Enumerations (`matches.source`, `match_events.event_type`, matchup `relation`) are `VARCHAR`
  with a `CHECK`, so adding a value is a constraint change rather than `ALTER TYPE`.
- Constraint names follow the naming convention in `models.py` (`pk_<table>`,
  `fk_<table>_<col>_<ref>`, `ck_<table>_<name>`, `uq_<table>_<col>`).

## Tables

### `patches`

| Column | Type | Notes |
| --- | --- | --- |
| `patch_id` | smallint PK | our id, assigned when the patch is registered |
| `name` | varchar(16) unique | e.g. `7.39d` |
| `released_at` | timestamptz unique | a match belongs to the latest patch released before its `start_time` |

### `matches` — partitioned

PK `(match_id, start_time)`. Columns: `duration` (s), `patch_id` → `patches`, `game_mode`,
`lobby_type`, `avg_rank_tier` (nullable, 10-85), `radiant_win`, `region`, `is_parsed`
(replay-derived events and timeseries exist), `source` (`opendota` | `stratz` | `replay`),
`ingested_at`.

Index `ix_matches_patch_rank (patch_id, avg_rank_tier)` — aggregate refresh selects one patch and
bracket at a time.

### `match_players` — partitioned

PK `(match_id, start_time, slot)`, FK `(match_id, start_time)` → `matches` `ON DELETE CASCADE`.
Columns: `patch_id` (copied from `matches`), `account_id` (nullable: anonymous profiles),
`hero_id`, `is_radiant`, `position`, `lane`, `lane_role`, `kills`, `deaths`, `assists`, `gpm`,
`xpm`, `last_hits`, `denies`, `net_worth`, `hero_damage`, `tower_damage`, `healing`, `items`
(int[6], final inventory, 0 = empty slot), `backpack` (int[]), `item_neutral`, `win`.

`patch_id` is denormalised so hero/position scans don't join `matches`.

Indexes:

- `ix_match_players_patch_hero_position (patch_id, hero_id, position)` — hero stats per patch.
- `ix_match_players_account_start_time (account_id, start_time) WHERE account_id IS NOT NULL` —
  a player's recent matches.

### `player_timeseries` — partitioned

PK `(match_id, start_time, slot)`, FK → `matches` cascade. Arrays indexed by minute
(PostgreSQL arrays are 1-based: `gold_t[1]` = 0:00, `gold_t[11]` = 10:00): `gold_t`, `xp_t`,
`lh_t`, `dn_t`. Only parsed matches have rows.

**Why arrays, not one row per minute.** OpenDota and STRATZ deliver these curves as arrays, and
tools either read a whole curve (one match's farm graph) or one minute across many players
(gold at 10:00). Both are a single row read with arrays. One row per minute would be ~40× the row
count, each value paying a ~24-byte tuple header plus the repeated key, for no query we plan. If a
tool later needs "all players' gold at minute N" at scale, it should go into an aggregate table,
not a per-minute raw table.

### `match_events` — partitioned

PK `(match_id, start_time, seq)`, FK → `matches` cascade. `seq` is the loader-assigned order within
the match. Columns: `event_type`, `time` (s), `slot` (actor), `target_slot`, `item_id`
(purchases), `key` (type-specific discriminator: rune type, ward type, killer unit), `payload`
(jsonb, defaults to `{}`).

`event_type` values and what goes where:

| `event_type` | `slot` | `target_slot` | `item_id` / `key` | `payload` (examples) |
| --- | --- | --- | --- | --- |
| `purchase` | buyer | — | item bought | `charges` |
| `death` | killer (if a hero) | victim | `key` = killer unit if not a hero | `gold_lost`, `respawn_s`, `net_worth` |
| `ward_placed` / `ward_destroyed` | owner / destroyer | — | `key` = `observer` \| `sentry` | `x`, `y` |
| `buyback` | player | — | — | `cost` |
| `rune` | player | — | `key` = rune type | — |
| `roshan` / `tormentor` | killer | — | — | `team`, `drop` |
| `teamfight` | — | — | — | `end`, `deaths`, per-player `gold_delta`/`xp_delta` |

**Why one table.** All event kinds share the same axes — match, game time, an acting player —
and the questions tools ask are "events of type X (optionally item Y) in a time window, joined to
the player's hero/position". One table with a typed `event_type` answers all of them with one
index and one partitioning scheme. Nine near-identical tables would mean nine loaders, nine sets
of partitions and `UNION`s for match timelines. The columns that tools filter or join on are typed
(`event_type`, `slot`, `item_id`, `key`, `time`); the rest, which only gets returned or summed, is
in `payload`. If one kind becomes hot enough to need its own typed columns, it can move to its own
table without changing the others.

Index `ix_match_events_type_item_time (event_type, item_id, time)` — item purchase timings. Lookups
of one match's events use the primary key prefix `match_id`.

### Aggregates

All aggregate tables carry `games`, `wins` and `updated_at`, and store **sums, not averages**, so
refresh jobs can add a batch of new matches with an upsert and the tool layer can merge buckets
(e.g. all brackets, or positions 4+5) and report the true sample size.

| Table | Primary key | Extra columns |
| --- | --- | --- |
| `patch_bracket_totals` | `patch_id, rank_bracket` | — (`games` = matches; the pick-rate denominator) |
| `hero_aggregates` | `patch_id, rank_bracket, position, hero_id` | `sum_kills`, `sum_deaths`, `sum_assists`, `sum_gpm`, `sum_xpm`, `sum_last_hits`, `sum_net_worth`, `sum_hero_damage`, `sum_tower_damage`, `sum_healing`, `sum_duration` |
| `hero_matchup_aggregates` | `patch_id, rank_bracket, hero_id, relation, other_hero_id` | — (`relation`: `with` = same team, `vs` = opposing) |
| `item_timing_aggregates` | `patch_id, rank_bracket, hero_id, position, item_id, minute` | — (histogram of first-purchase minute; medians come from the histogram) |

`position = 0` in aggregates means the position was not determined. Secondary indexes:
`ix_hero_aggregates_hero_patch (hero_id, patch_id)` for one hero across patches,
`ix_item_timing_aggregates_item_patch (item_id, patch_id)` for one item across heroes.

## Partitioning

`matches`, `match_players`, `player_timeseries` and `match_events` are `PARTITION BY RANGE
(start_time)`, one partition per **UTC calendar month**, named `<table>_YYYY_MM`.

- **Why `start_time`, not `patch_id`.** Patches have irregular lengths (weeks to months) and are
  only known once released, so patch partitions can't be created ahead of time. Monthly partitions
  are predictable, and since a patch is a `start_time` range (`patches.released_at`), queries
  filtering on patch also add the `start_time` bounds and get partition pruning.
- **Why all four tables.** Children share the parent's partition key and bounds, so a month of
  data — the match and everything hanging off it — is detached or dropped together, and joins on
  `(match_id, start_time)` can be partition-wise.
- **Keys.** PostgreSQL requires the partition key in every unique constraint, so `start_time` is
  part of each primary key and of the `(match_id, start_time)` foreign keys. `match_id` alone is
  therefore not enforced unique across months; the loader upserts on `(match_id, start_time)`,
  and a source always reports the same `start_time` for a match.
- **Creating partitions.** Migration `0001` defines `ensure_month_partitions(ts timestamptz)`,
  which idempotently creates the month containing `ts` for all four tables (bounds computed in UTC
  whatever the session time zone), and pre-creates January 2026 – December 2027. The collector
  must call `SELECT ensure_month_partitions(:start_time)` before inserting a match from a month it
  hasn't seen; an insert into a month without a partition fails rather than landing in a catch-all.
  There is deliberately no `DEFAULT` partition: rows in it would block creating that month's
  partition later.
- Alembic autogenerate ignores the `<table>_YYYY_MM` partitions and the per-partition foreign
  keys PostgreSQL creates (`migrations/env.py`), so `alembic check` stays clean.

## Collector additions (migration `0002`)

See [collector.md](collector.md) for how these are written.

- `match_players` player stats (`kills` … `denies`, `items`) are nullable: draft-only rows from
  `publicMatches` carry hero, side and result only, until enrichment fills them in.
- `matches` gains `has_details`, `detail_attempts`, `banned_hero_ids` (NULL = unknown),
  `aggregated_at` and `details_aggregated_at` (incremental-aggregation markers, with partial
  indexes on the pending rows).
- `patch_bracket_totals` gains `detailed_games` and `ban_sample_games` (denominators for detail
  aggregates and ban rate).
- New aggregates: `hero_draft_aggregates` (patch, bracket, hero: games, wins, bans) and
  `hero_stat_histograms` (patch, bracket, position, hero, stat, bucket: games, wins).
- New state: `aggregate_status` (stale patches), `hero_stats_daily`, `reference_snapshots`,
  `collector_job_runs`, `collector_api_usage`, `collector_cursors`, `collector_gaps`.
- Models map `ARRAY`/`JSONB` to `JSON` on SQLite so collector logic is testable without
  PostgreSQL; the PostgreSQL DDL is unchanged.

## Which tool reads what

| Tool | Reads | Notes |
| --- | --- | --- |
| `compare_heroes` | `hero_aggregates`, `patch_bracket_totals` | win rate = wins/games, pick rate = games / bracket matches; filters map to the PK |
| `get_farming_stats` | `hero_aggregates` (GPM, XPM, net worth, LH); `player_timeseries` (curves) | curves only from parsed matches; report that subset's sample size |
| `analyze_item` | `item_timing_aggregates`; `match_players.items` for final-build rates | timings only from parsed matches |
| `analyze_death` | `match_events` (`death`, `buyback`) joined to `match_players`; `player_timeseries` | parsed matches only |
| `find_meta_trends` | `hero_aggregates` across consecutive `patch_id`s | within-patch trends would need a time-bucketed aggregate (not in this revision) |
| `analyze_match` | `matches`, `match_players`, `match_events`, `player_timeseries` by `match_id` | |
| `get_player_stats` | `match_players` by `account_id`, joined to `matches` | only public profiles have `account_id` |

## Retention

Proposed policy (not enforced by the migration; a scheduled job will do it):

- **Raw tables:** keep the current and two previous patches, and at least 6 months. Older months
  are removed with `ALTER TABLE <parent> DETACH PARTITION <parent>_YYYY_MM` then `DROP TABLE`,
  children first (`match_events`, `player_timeseries`, `match_players`) then `matches` — no row-by-row
  `DELETE`, no vacuum debt.
- **`player_timeseries` and `match_events`** are the largest per match and may get a shorter window
  once volumes are known; dropping their partitions leaves `matches`/`match_players` intact, with
  `is_parsed` still describing what the source had.
- **Aggregates and `patches`:** kept indefinitely; they are small next to the raw tables
  and are what historical comparisons read.

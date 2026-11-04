# OpenDota API research

Tracker: p1-01 (endpoints), p1-06 (parsed vs unparsed), p1-08 (freshness and rank coverage).

All numbers below were measured by real calls to `https://api.opendota.com/api` without an API key
on **2026-11-04, 16:32–16:52 UTC**, unless stated otherwise. API spec version reported by
`GET /api` (OpenAPI 3.0.3 document): **31.1.1**. About 170 rate-limit units were used in total
(`x-rate-limit-remaining-day` went from 2999 to ~2830; parse requests count as 10 each).

Trimmed example responses: [`samples/opendota/`](samples/opendota/)

- `match_parsed_trimmed.json`: `GET /matches/9073999945` (parsed, one player, arrays cut)
- `public_matches_min_rank70.json`: `GET /publicMatches?min_rank=70` (5 rows)
- `herostats_benchmarks.json`: one `GET /heroStats` row and `GET /benchmarks?hero_id=1` (3 metrics)

## TL;DR

- Free tier: 60 calls/min, 3000 calls/day, no key. Paid key: 3000 calls/min, no daily cap,
  $0.0001/call ($0.01 per 100). That 3000/day cap rules out bulk match collection on the free tier.
- Only a small share of public matches is parsed: **4 of 50** sampled matches from about 1 day ago
  (3 of 30 Ranked All Pick, 1 of 20 other modes). Without parsing, there is no per-minute data
  (`gold_t`, `xp_t`, `lh_t`), no `purchase_log`, `kills_log` or `deaths_log`, no wards, no
  `teamfights`, and no `lane_role`.
- Parsing can be requested (`POST /request/{match_id}`). A 1-day-old match parsed within
  ~20 s of the request. A match that had ended 5 min earlier did not parse on the first request;
  a second request at 9 min after the end succeeded within ~1 min.
- Freshness: matches appear in `publicMatches` about **5–6 min after they end** (the freshest
  rows had ended 4.7–5.8 min before the call). Unparsed `/matches/{id}` data is available at
  the same time.
- Immortal is not visible as its own bracket in aggregates. `avg_rank_tier` tops out at **75**
  (a match where 9 of 10 players were rank tier 80 showed `avg_rank_tier` 75), `min_rank=80` still
  returns rows with avg 75, `heroStats` `8_pick` is 0 for every hero, and `benchmarks?bracket=8`
  returns null values. Per-player `rank_tier` 80 and `leaderboard_rank` do exist.

## 1. Endpoints

Base URL `https://api.opendota.com/api`. All are `GET` unless noted. Sizes and timings are from
single calls on 2026-11-04 (all returned 200 in 0.22–0.49 s).

### Hero meta

| Endpoint | Returns | Key fields | Observed |
|---|---|---|---|
| `/heroStats` | One row per hero: static attributes plus pick and win counts per rank bracket | `id`, `localized_name`, `roles`, `{1..8}_pick`, `{1..8}_win` (1 = Herald … 8 = Immortal), `pub_pick`/`pub_win` and `*_trend` (7 daily values), `turbo_picks`/`turbo_wins`, `pro_pick`/`pro_win`/`pro_ban` | 127 heroes, 165 KB. Summed picks/10 = about 4.13M public matches across the 7 trend days (daily 371k–700k; the last value is a partial day). `8_pick` = 0 for every hero |
| `/benchmarks?hero_id=&bracket=&role=` | Percentile values (p10…p99) of per-minute stats for one hero | `result.{gold_per_min, xp_per_min, kills_per_min, deaths_per_min, assists_per_min, last_hits_per_min, denies_per_min, hero_damage_per_min, hero_healing_per_min, tower_damage}[] = {percentile, value}` | AM GPM p50 = 666 (all ranks), 710 (`bracket=7&role=core`). `bracket=8` returns all-null values. `role` ∈ `core`/`support` |
| `/heroes/{id}/matchups` | Games played and wins against each other hero | `hero_id`, `games_played`, `wins` | AM: 126 rows, 3245 games summed, max 80 per opponent. The sample is small, so treat it as pro or limited data |
| `/heroes/{id}/itemPopularity` | Item counts by game phase | `start_game_items`, `early_game_items`, `mid_game_items`, `late_game_items` (item_id → count) | The spec says it is computed "from professional games" |
| `/heroes/{id}/durations` | Win rate per duration bin | `duration_bin` (s), `games_played`, `wins` | AM: 12,732 games summed |
| `/heroes/{id}/matches`, `/heroes/{id}/players` | Recent pro/league matches with the hero; players on the hero | | not called |
| `/scenarios/itemTimings?hero_id=&item=` | Win rate when an item (cost ≥ 1400) is bought before time T | `hero_id`, `item` (name), `time` (450/600/720/900/1200/1500/1800 s), `games`, `wins` (strings) | AM: 252 rows, 15,141 games summed |
| `/scenarios/laneRoles?hero_id=&lane_role=` | Win rate by lane role (1–4) and game-duration bucket | `lane_role`, `time` (900…5400), `games`, `wins` | AM: 14 rows, 2061 games |
| `/scenarios/misc?scenario=` | Team scenarios: `first_blood`, `courier_kill`, `pos_chat_1min`, `neg_chat_1min` by region and side | `scenario`, `is_radiant`, `region`, `games`, `wins` | 106 rows |
| `/rankings?hero_id=` | Top 100 players on a hero | `account_id`, `score`, `personaname`, `rank_tier` | 100 rows |

### Matches

| Endpoint | Returns | Key fields | Observed |
|---|---|---|---|
| `/matches/{match_id}` | Full match: always the Steam API fields, plus replay-derived fields when parsed (see §3) | `od_data.{has_api, has_gcdata, has_parsed}`, `version` (non-null = parsed; 22 observed), `players[]`, `picks_bans`, `radiant_gold_adv`, `teamfights`, `objectives`, `replay_url`, `patch`, `region` | ~0.3 s. Works for unparsed matches. `players[].benchmarks` is present even when unparsed |
| `/publicMatches?less_than_match_id=&min_rank=&max_rank=` | 100 most recent sampled public matches (descending `match_id`) | `match_id`, `start_time`, `duration`, `lobby_type`, `game_mode`, `avg_rank_tier`, `num_rank_tier`, `radiant_team`/`dire_team` (hero id arrays), `radiant_win` | Unfiltered, the newest 100 rows covered only 20 min of start times. 36 had `duration` 0, most of them with hero arrays `[0,0,0,0,0]`, and only 6 were Ranked All Pick. Use `min_rank` and filter on `lobby_type=7` |
| `/parsedMatches?less_than_match_id=` | IDs of recently parsed matches | `match_id` | Newest row had ended 8.6 min before the call |
| `/proMatches?less_than_match_id=` | 100 most recent league matches | `match_id`, `leagueid`, `league_name`, `radiant_name`/`dire_name`, team ids, scores, `version` | The 100 rows spanned about 9 days. 72 of 100 had team names. Newest had ended 33 min earlier |
| `/findMatches?teamA=1&teamB=2` | Recent matches containing the given heroes on opposite teams | `match_id`, `teama`, `teamb`, `teamawin`, `start_time` | 10 rows, all started 28–37 min before the call |
| `/live` | Currently ongoing top games | `match_id`, `game_time`, `average_mmr`, `radiant_lead`, scores, `building_state`, `league_id`, team names, `players[]{account_id, hero_id}` | 100 games (94 lobby_type 7, 5 league). `hero_id` can be 0 during the draft |
| `/records/{field}` | Top 100 single-match performances in a stat (e.g. `kills`) | `match_id`, `start_time`, `hero_id`, `score` | 100 rows (top `kills` = 119) |
| `POST /request/{match_id}` / `GET /request/{jobId}` | Queue a replay parse / job state | `{job: {jobId}}`; the job state returned `null` once it was gone | Counts as 10 calls for rate limiting (spec) |

### Players

| Endpoint | Returns | Notes |
|---|---|---|
| `/players/{id}` | Profile, `rank_tier`, `leaderboard_rank`, `computed_mmr`, aliases | Example: rank_tier 80, leaderboard_rank 4169 |
| `/players/{id}/wl`, `/heroes`, `/matches`, `/peers`, `/pros`, `/totals`, `/counts`, `/histograms/{field}`, `/wardmap`, `/wordcloud` | Aggregates over the player's match history | All accept the common filters `limit, offset, win, patch, game_mode, lobby_type, region, date (days back), lane_role, hero_id, is_radiant, included/excluded_account_id, with_hero_id, against_hero_id, significant, having, sort`; `/matches` also takes `project` |
| `/players/{id}/recentMatches` | Last 20 matches with KDA, GPM/XPM, `lane_role`, `average_rank`, `version` | 20 rows |
| `/players/{id}/ratings`, `/rankings` | Rank-tier history; hero rankings | not called |
| `POST /players/{id}/refresh` | Re-fetch match history (up to 500), rank and name | not called |
| `/topPlayers`, `/proPlayers`, `/search?q=` | Highly ranked players (`computed_mmr`, `rank_tier`, `team_name`, `is_pro`); pro list; name search | `topPlayers`: 100 rows |

### Reference data and other

| Endpoint | Notes |
|---|---|
| `/constants/{resource}` | dotaconstants JSON: `heroes` (93 KB), `items` (344 KB, 501 entries with `id`, `dname`, `cost`, `abilities`, …), also `abilities`, `patch`, `game_mode`, `lobby_type`, `region`, … |
| `/heroes`, `/leagues`, `/teams`, `/teams/{id}/{matches,players,heroes}` | Lists and team data |
| `/distributions` | `ranks.rows[] = {bin (rank_tier), count, cumulative_sum}` over player profiles. Total 8,781,877; tier 80 = 344,770 (3.9%) |
| `/metadata` | `freeCallLimit: 3000, freeRateLimit: 60, premRateLimit: 3000`, scenario timing buckets |
| `/health` | Internal pipeline metrics (`parseDelay`, `gcDelay`, `seqNumDelay`, storage usage) |
| `/explorer?sql=` | Read-only SQL against the Postgres DB. Tables: `heroes, items, leagues, matches, match_patch, notable_players, picks_bans, player_matches, public_matches, team_match, team_rating, teams`. Filters on `start_time` timed out (`Query read timeout`); ranges on `match_id` returned in under 1 s. The `matches` / `player_matches` tables hold league matches only: 0 of ~64.8k `public_matches` rows in the ID window 9073900000–9074000000 joined to `matches`, although 4 parsed matches from my sample fall in that window |

## 2. Rate limits and pricing

From <https://www.opendota.com/api-keys> (read 2026-11-04) and `/metadata`:

| | Free | Premium (key) |
|---|---|---|
| Price | free | $0.0001 per call ($0.01 per 100), billed monthly to a card; responses with status 404/429/500 are not billed |
| Key | not needed | required; requires a linked payment method |
| Daily cap | 3000 calls | unlimited |
| Rate | 60 calls/min | 3000 calls/min |

- The key is passed as `?api_key=KEY` or the header `Authorization: Bearer KEY` (from the OpenAPI
  `securitySchemes`).
- Every response carries `x-rate-limit-remaining-minute` and `x-rate-limit-remaining-day`. The
  minute counter reset each minute as expected. The response also echoes `x-ip-address`, which suggests keyless limits are per IP
  (not verified).
- `POST /request/{match_id}` counts as 10 calls for rate limiting (not billing).
- Cost example: collecting 100k match details per month costs about $10 with a key. That volume is
  impossible on the free tier (3000/day ≈ 90k/month, and nothing else would fit in the budget).

## 3. Parsed vs unparsed matches

`od_data.has_parsed` and a non-null `version` mark a parsed match. `has_gcdata` (replay salt from
the game coordinator) was true for all sampled Ranked matches and false for the unparsed Turbo
matches.

**Fields present only when parsed.** Diff of non-empty keys, parsed `9073999945` vs unparsed
`9073999950`, both Ranked AP from the same minute:

- Match level: `radiant_gold_adv`, `radiant_xp_adv`, `teamfights`, `objectives`, `chat`,
  `all_word_counts`, `pauses`, `throw`, `loss`, `version`.
- Player level: `gold_t`, `xp_t`, `lh_t`, `dn_t`, `networth_t`, `purchase_log`, `purchase`,
  `purchase_time`, `first_purchase_time`, `item_uses`, `item_usage`, `item_win`, `kills_log`,
  `deaths_log`, `killed`, `killed_by`, `buyback_log`, `runes_log`, `runes`, `obs_log`, `sen_log`,
  `obs_left_log`, `sen_left_log`, `obs_placed`, `sen_placed`, `obs`, `sen`, `lane`, `lane_role`,
  `lane_pos`, `lane_efficiency(_pct)`, `is_roaming`, `damage`, `damage_taken`,
  `damage_inflictor(_received)`, `damage_targets`, `hero_hits`, `ability_uses`, `ability_targets`,
  `hero_damage_t`, `hero_healing_t`, `tower_damage_t`, `stuns`, `actions`, `actions_per_min`,
  `camps_stacked(_t)`, `creeps_stacked`, `teamfight_participation`, `multi_kills`, `kill_streaks`,
  `max_hero_hit`, `life_state(_dead)`, `connection_log`, `pings`, `neutral_item_history`,
  `times`, and the kill counters (`hero_kills`, `tower_kills`, `roshan_kills`, …).
- Present either way: KDA, `last_hits`, `denies`, GPM/XPM, `net_worth`, `hero_damage`,
  `tower_damage`, `hero_healing`, final `item_0..5`, backpack, neutral item, Aghanim's flags,
  `ability_upgrades_arr`, `rank_tier`, `party_size`, `picks_bans`, `benchmarks`.

**Share parsed.** Measured 2026-11-04 on the first rows of `publicMatches?less_than_match_id=9074000000`
(matches started around 2026-11-03 03:50 UTC, so about 1 day old):

| Sample | Parsed |
|---|---|
| 30 Ranked All Pick (lobby 7, mode 22) | 3 (10%); avg_rank_tier of the parsed ones 52, 74, 75 |
| 20 other matches >10 min (17 Turbo, 2 Single Draft, 1 Ability Draft) | 1 (5%; the Ability Draft match) |
| Total | **4 / 50 (8%)** |

The sample is small and covers a single minute of match IDs, so it gives the order of magnitude, not
a precise rate.

**Requesting a parse.** `POST /request/{match_id}` returns `{"job":{"jobId":N}}`, and
`GET /request/{jobId}` returns the job state or `null` once the job is gone.

- `9073999950` (about 1 day old, unparsed): `has_parsed: true`, `version: 22` within ~20 s of the
  POST.
- `9076156738` (Ranked, avg 75): first POST at ~300 s after the match ended. The job was `null`
  after 15 s, and the match was still unparsed at 545 s. A second POST at 552 s after the end gave
  `has_parsed: true` by 608 s. The likely reason is that the replay was not yet downloadable on the
  first attempt (inferred, not confirmed). A collector should retry.

## 4. Freshness and rank coverage

**Delay.** Fetched `publicMatches` at 16:33 UTC: the freshest rows with `duration > 0` had ended
4.7, 4.8 and 4.9 min earlier (unfiltered); 5.8 min (`min_rank=70`) and 5.7 min (`min_rank=80`). Five
minutes later, with `min_rank=60`, the freshest Ranked matches had ended 4.8–6.8 min before.
`/matches/{id}` for a match that had ended 304 s earlier returned full unparsed data at once. The
newest `parsedMatches` row had ended 8.6 min before the call. The SQL-side tables lag further:
`proMatches` newest was 33 min old (that depends on pro schedule).

**Rank distribution of available matches.** Explorer query over `public_matches` with
`match_id` 9073900000–9074000000 (start times 2026-11-03 00:48–03:52 UTC, about 3 h; this window
favours SEA/CN/RU prime time):

| Bracket (`floor(avg_rank_tier/10)`) | Ranked AP (lobby 7) | Share |
|---|---|---|
| 1 Herald | 435 | 1.2% |
| 2 Guardian | 1,825 | 5.0% |
| 3 Crusader | 3,755 | 10.3% |
| 4 Archon | 7,315 | 20.1% |
| 5 Legend | 10,221 | 28.0% |
| 6 Ancient | 6,257 | 17.2% |
| 7 Divine (incl. Immortal) | 4,827 | 13.2% |
| no rank | 1,815 | 5.0% |
| **total** | **36,450** | |

The same window also had 25,929 unranked lobby 0 matches (mostly Turbo), 1,305 bot matches
(lobby 4) and 1,113 lobby 14 matches. `heroStats` bracket totals (picks/10, 7 days) follow the same
shape: 1: 81k, 2: 274k, 3: 527k, 4: 865k, 5: 1.02M, 6: 580k, 7: 399k, 8: 0.

**Divine and Immortal.** Divine has good volume (13% of ranked, about 400k matches/week in
`heroStats`). Immortal cannot be separated in aggregates: `avg_rank_tier` maxes at 75, and bracket 8
is empty in `heroStats` and `benchmarks`. For Immortal-only stats we would need to compute from
per-player `rank_tier` (80) in `/matches/{id}` ourselves, or use `/topPlayers` / `/rankings` /
`leaderboard_rank`. Player-population share of Immortal from `/distributions`: 3.9%.

## 5. Planned tools → OpenDota coverage

Legend: **available** means one or two calls answer it directly; **partial** means the data exists
but is pre-aggregated in an incompatible way, the sample is small, parsing is required, or
Immortal is missing; **missing** means OpenDota has no endpoint for it.

| Tool | Endpoint(s) | Status | Notes |
|---|---|---|---|
| `compare_heroes` | `/heroStats`, `/benchmarks`, `/heroes/{id}/matchups` | available | Win/pick per bracket 1–7 and 7-day trend; no Immortal split; matchups sample small |
| `get_farming_stats` | `/benchmarks` (GPM, XPM, LH/min by percentile, `bracket`, `role`); per-minute `gold_t`/`lh_t` from parsed `/matches` | partial | Percentiles available; minute-by-minute farm curves only from parsed matches (~8%) |
| `analyze_item` | `/scenarios/itemTimings`, `/heroes/{id}/itemPopularity`, `/constants/items`; `purchase_log`/`item_win` from parsed matches | partial | Timing win rates only for items ≥1400 gold at fixed timings; popularity is pro-only |
| `analyze_death` | parsed `/matches/{id}`: `deaths_log`, `killed_by`, `kills_log`, `teamfights` | partial | Requires parsing (request it, ~1 min); no aggregated death stats |
| `find_meta_trends` | `/heroStats` `*_trend` (7 days), `pro_pick/ban`, `/explorer` on `public_matches` (hero arrays + rank), `/proMatches` | partial | Only 7 daily points; longer history needs our own collection via `publicMatches` |
| `analyze_match` | `/matches/{id}`, `POST /request/{id}` | available | Unparsed data at ~5 min; full parse after requesting it |
| `get_player_stats` | `/players/{id}`, `/wl`, `/heroes`, `/recentMatches`, `/totals`, `/counts`, `/histograms` | available | Rich filters (date, hero, lane_role, patch) |
| Hero matchups | `/heroes/{id}/matchups`, `/findMatches`, `/explorer` on `public_matches` | partial | Matchups endpoint small (AM: 3.2k games); compute from `public_matches` hero arrays for volume |
| Hero builds (items/skills) | `/heroes/{id}/itemPopularity`, parsed `purchase_log`, `ability_upgrades_arr` | partial | Pro-only popularity; pub builds need parsed matches |
| Hero benchmarks | `/benchmarks?hero_id&bracket&role` | available | Bracket 8 (Immortal) returns nulls |
| Lane stats | `/scenarios/laneRoles`, parsed `lane_role`, `lane_efficiency_pct` | partial | Win rate by lane role and duration only; no laning-phase outcomes |
| Item stats (global) | `/scenarios/itemTimings`, `/constants/items` | partial | Per hero only; no global item win rate endpoint |
| Draft helper | `/heroStats`, `/heroes/{id}/matchups`, `/findMatches`, `/live` | partial | No synergy (with-hero) endpoint; build from `public_matches` |
| Player profile | `/players/{id}`, `/ratings`, `/rankings`, `/peers`, `/search` | available | |
| Pro matches | `/proMatches`, `/leagues/*`, `/teams/*`, `/matches/{id}` (pro matches are parsed) | available | |
| Live games | `/live` | available | Top ~100 games; scores, lead, buildings, players; no per-player live stats |
| Records | `/records/{field}` | available | Top 100 per field, all-time/recent mix |
| Rank distribution | `/distributions` | available | Player population by rank tier including 80 |

## Implications for the collector

1. Keep under 3000 calls/day without a key. One `publicMatches` call gives 100 match summaries
   (hero ids, rank, result), which is enough for hero/draft/meta aggregates at volume (about 30
   calls per 1000 ranked matches after filtering). For per-match details, budget a paid key.
2. Page with `less_than_match_id` and `min_rank`, and keep only `lobby_type=7` with `duration > 0`.
   Wait at least 6 min before reading new matches.
3. Do not rely on `avg_rank_tier` for Immortal. Read player `rank_tier` from `/matches/{id}` if
   Immortal-only stats are a goal.
4. For tools that need replay data, request a parse and retry once if `has_parsed` is still
   false after ~1 min.

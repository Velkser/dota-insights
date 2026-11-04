# Pro features and the cost of a request

Tracker: p5-05. Research only: no production code changed. Prices were checked on
**2026-11-04**. Re-check them before any Pro milestone, as `data-rights.md` asks for terms.

**Summary.** One tool call costs us about **$0.0002 cold** (sources called) and **$0.00003
warm** (served from cache), counting Vercel Pro compute, Upstash commands and the paid OpenDota
tier. Money is not what limits growth. The **STRATZ daily quota** fills up first, at around
300k calls a month, and the OpenDota free quota fills up soon after. A Pro tier would
pay for itself with a handful of subscribers. The real preconditions are commercial-use
clearance (Vercel Hobby forbids it; STRATZ terms are silent) and auth (p4-09).

## 1. How the per-call numbers were measured

- **Source requests (cold)** were counted, not guessed. A pytest plugin wrapped
  `metrics.record_source_call` (every OpenDota `_request` and STRATZ `query` goes through it)
  and attributed each request to the tool and endpoint on the stack. The whole suite
  (`PYTHONPATH=docs/research/samples/pro-costs uv run pytest -q -p countplug`, plugin in
  [`samples/pro-costs/countplug.py`](samples/pro-costs/countplug.py)) ran against the recorded fixtures with mocked transports,
  so **no real API quota was spent**. Fixtures are small (a few heroes, a few matches), so the
  fan-out that depends on the data was read from the code and scaled to default arguments:
  item batches of 25 heroes (`analytics/items.py:HERO_BATCH`), STRATZ match details per 100
  matches (`players.py:STRATZ_PAGE`), aliased pro-match batches of 10 (`pro.py:STRATZ_BATCH`),
  ≤5 benchmark heroes, up to 3 team pages, and 1 `/records` call per metric.
- **Warm** means every cached function hits Redis or instance memory: 0 source requests. TTLs
  come from `cache.py`: aggregates 6 h, reference 24 h, players 15 min, live 60 s, finished
  matches 7 days. The exception is an **unparsed** match. It is fetched again on each call
  until it is parsed, and its `POST /request` counts as **10 OpenDota rate-limit units**
  (`opendota.md`), deduplicated for 5 min.
- **DB first** tools read the collector's Postgres aggregates (`db/access.read`) and call no
  source when the aggregate covers the filters (`db-backed-tools`, PR #30). The cold column
  shows the fallback cost when the aggregate does not apply (letter patch, Immortal-only, stale).
- **Latency class**, read from the code (explorer SQL is slow; STRATZ and plain REST are not):
  **A** < 1 s, **B** 1–5 s, **C** 5–20 s (explorer SQL or 4+ paced requests; heavy tools may
  run up to 150 s, `errors.TOOL_TIMEOUTS`). Production latencies could not be read: the
  `usage_events` table (p4-08) holds `latency_ms`, but the local `.env` points at an empty local
  Postgres, not Neon. The first follow-up is re-running this table from
  `dota-insights usage report --json` (p50/p95 per tool).
- **Compute.** With mocked I/O the tool tests take 0.2–0.4 s each, including pytest overhead,
  so CPU per call is taken as 0.05 s (A), 0.15 s (B), 0.3 s (C), 0.5 s for `render_chart`.
  Memory is billed for the whole wall time at Vercel's default 2 GB, assuming one call per
  instance. That is pessimistic, since Fluid compute runs concurrent calls on one instance.
- **Redis commands per call**: about 4 fixed (rate-limit window, usage push, cache generation,
  cache read), plus 2 per source request (shared quota counter, cache write).
- **Heavy** is `ratelimit.HEAVY_TOOLS`: 5/min and 150/day per IP.

### Unit prices used

| Item | Price (2026-11-04) | Source |
|---|---|---|
| Vercel Hobby | $0; 4 h Active CPU, 360 GB-h memory, 1M invocations / month; **non-commercial use only**, no paid overage | [vercel.com/pricing](https://vercel.com/pricing), [fair-use guidelines](https://vercel.com/docs/limits/fair-use-guidelines) |
| Vercel Pro | $20/month with $20 usage credit; Active CPU $0.128/h, memory $0.0106/GB-h (iad1, the cheapest region), invocations $0.60/M, 1 TB transfer | [vercel.com/pricing](https://vercel.com/pricing), [Fluid compute pricing](https://vercel.com/docs/fluid-compute/pricing) |
| Neon Free | 100 CU-h / project / month, 1 GB storage per project; compute suspends at the cap | [neon.com/pricing](https://neon.com/pricing) |
| Neon Launch | no minimum; $0.106/CU-h, $0.35/GB-month; Neon's own "small intermittent DB" example is about $15/month | [neon.com/pricing](https://neon.com/pricing) |
| Upstash Redis | Free 500K commands/month, 250 MB; pay-as-you-go $0.20 per 100K commands; fixed 250 MB plan $10/month | [upstash.com/pricing/redis](https://upstash.com/pricing/redis) |
| OpenDota API | Free 60/min, 3,000/day, no key; Premium $0.0001/call ($0.01 per 100), 3,000/min, no daily cap; 404/429/500 are not billed | [opendota.com/api-keys](https://www.opendota.com/api-keys) (read in a browser; the page is rendered by JS) |
| STRATZ API | Free tokens only: Default 10,000/day, Individual 20,000/day (application + 1,000 referral clicks/month); no paid API tier is published | [`stratz.md`](stratz.md), [stratz.com/api](https://stratz.com/api) |

## 2. Cost per tool (default arguments)

"$ per 1k calls" = Vercel Pro compute + invocation + Upstash commands + OpenDota Premium
requests, computed by [`samples/pro-costs/costs.py`](samples/pro-costs/costs.py). STRATZ is free in money but limited by quota, so its column is the number to watch.

| Domain | Tool | Heavy | OD cold | STRATZ cold | DB first | Cache | Latency cold / warm | $ per 1k calls, cold | $ per 1k, warm | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| analysis | `analyze_correlation` | yes | 1 | 0 |  | 6h | C / A | 0.182 | 0.014 | 1 explorer SQL (≤500 player rows / 3000 hero rows) |
| analysis | `explore_query` | yes | 1 | 0 |  | 6h | C / A | 0.182 | 0.014 | 1 explorer SQL, limit ≤500 |
| analysis | `find_matches` | yes | 2 | 0 |  | 6h | C / A | 0.286 | 0.014 | 2 explorer SQL (pub window + rows) |
| analysis | `test_hypothesis` | yes | 1 | 0 |  | 6h | C / A | 0.182 | 0.014 | 1 explorer SQL |
| builds | `get_ability_stats` |  | 0 | 6 |  | 6h | B / A | 0.056 | 0.014 | Builds + 4 aliased batches + guides (≤60 guides / 20 per query) |
| builds | `get_hero_builds` |  | 0 | 6 |  | 6h | B / A | 0.056 | 0.014 | same queries as get_ability_stats, shared cache |
| builds | `get_neutral_items` | yes | 2 | 1 |  | 6h | C / A | 0.290 | 0.014 | STRATZ Neutral; OpenDota item constants (24h) + 1 explorer fallback |
| charts | `render_chart` |  | 0 | 0 |  | — | A / A | 0.028 | 0.030 | no source; CPU for the PNG (~0.3–0.7 s) |
| economy | `analyze_death` |  | 2 | 0 |  | 6h | C / A | 0.286 | 0.014 | 2 explorer SQL |
| economy | `get_comeback_stats` | yes | 2 | 1 |  | 6h | C / A | 0.290 | 0.014 | 2 explorer SQL + STRATZ Comeback |
| economy | `get_farm_distribution` | yes | 2 | 0 |  | 6h | C / A | 0.286 | 0.014 | 2 explorer SQL |
| economy | `get_objective_impact` | yes | 2 | 0 |  | 6h | C / A | 0.286 | 0.014 | 2 explorer SQL |
| farming | `get_farming_stats` | yes | 1 | 0 | yes | 6h | C / A | 0.182 | 0.014 | DB histograms first; else 1 explorer SQL (≤300 league matches) |
| farming | `get_gold_xp_curves` | yes | 2 | 0 |  | 6h | C / A | 0.286 | 0.014 | 2 explorer SQL |
| farming | `get_hero_benchmarks` |  | 1 | 0 | yes | 6h | B / A | 0.136 | 0.014 | DB first; else /benchmarks |
| farming | `get_hero_lane_stats` |  | 1 | 0 |  | 6h | C / A | 0.182 | 0.014 | 1 explorer SQL (league fallback) |
| farming | `get_hero_power_curve` |  | 1 | 1 |  | 6h | B / A | 0.140 | 0.014 | STRATZ first, /heroes/{id}/durations fallback |
| feedback | `report_wrong_answer` |  | 0 | 0 |  | — | A / A | 0.012 | 0.014 | no source; Redis push + DB row via collector |
| feedback | `suggest_feature` |  | 0 | 0 |  | — | A / A | 0.012 | 0.014 | no source; Redis push |
| general | `get_general_stats` |  | 4 | 1 |  | 6h | C / A | 0.498 | 0.014 | STRATZ Regions; explorer + 3 constants (24h) |
| general | `get_rank_distribution` |  | 1 | 1 |  | 6h | B / A | 0.140 | 0.014 | /distributions + STRATZ |
| general | `get_records` | yes | 11 | 0 |  | 6h | C / A | 1.222 | 0.014 | 1 /records/{metric} per metric, 11 when all are asked |
| heroes | `compare_heroes` |  | 0 | 3 | yes | 6h | B / A | 0.044 | 0.014 | DB first; GameVersions + HeroWinDay + HeroAverages |
| heroes | `find_hidden_gems` | yes | 0 | 2 |  | 6h | B / A | 0.040 | 0.014 | GameVersions + HeroWinDay (all heroes in one query) |
| heroes | `get_hero_overview` |  | 1 | 4 | yes | 6h | B / A | 0.152 | 0.014 | DB first; 4 STRATZ + /heroStats for pro bans |
| heroes | `get_tier_list` | yes | 1 | 2 |  | 6h | B / A | 0.144 | 0.014 | 2 STRATZ; /heroStats when STRATZ fails |
| items | `analyze_item` | yes | 1 | 6 | yes | 6h | C / A | 0.206 | 0.014 | DB first; all heroes = 6 batches of 25, one hero = 1; OpenDota scenarios fallback |
| items | `compare_items` | yes | 0 | 1 | yes | 6h | B / A | 0.036 | 0.014 | DB first; 1 batch per 25 heroes |
| items | `get_item_overview` | yes | 0 | 6 | yes | 6h | C / A | 0.102 | 0.014 | DB first; 6 batches of 25 heroes |
| items | `get_item_payback` | yes | 1 | 0 |  | 6h | C / A | 0.182 | 0.014 | 1 explorer SQL |
| live | `predict_live_win` | yes | 1 | 0 |  | 60s | B / A | 0.136 | 0.014 | /live (60 s cache) |
| matches | `analyze_laning_phase` |  | 2 | 0 |  | 7d | B / B | 0.240 | 0.134 | /matches/{id}; unparsed adds POST /request (10 rate units, deduped 5 min) |
| matches | `analyze_match` |  | 2 | 0 |  | 7d | B / B | 0.240 | 0.134 | as above |
| matches | `analyze_teamfights` |  | 2 | 0 |  | 7d | B / B | 0.240 | 0.134 | as above |
| matches | `analyze_vision` |  | 2 | 0 |  | 7d | B / B | 0.240 | 0.134 | as above |
| matches | `find_turning_points` |  | 2 | 0 |  | 7d | B / B | 0.240 | 0.134 | as above |
| matches | `get_match_timeline` |  | 2 | 0 |  | 7d | B / B | 0.240 | 0.134 | as above |
| matchups | `analyze_draft` |  | 0 | 2 | yes | 6h | B / A | 0.040 | 0.014 | DB first; Matchups + Positions; OpenDota fallback ≤10 heroes |
| matchups | `find_hero_combos` | yes | 0 | 2 | yes | 6h | B / A | 0.040 | 0.014 | DB first; Matchups + Positions |
| matchups | `get_hero_matchups` |  | 0 | 2 | yes | 6h | B / A | 0.040 | 0.014 | DB first; Matchups + Positions |
| matchups | `suggest_pick` |  | 0 | 2 | yes | 6h | B / A | 0.040 | 0.014 | DB first; Matchups + Positions |
| meta | `server_info` |  | 0 | 0 |  | — | A / A | 0.012 | 0.014 | reads quota counters from Redis only |
| players | `compare_players` | yes | 4 | 2 |  | 15m | C / A | 0.502 | 0.014 | 2 × (profile + matches + 1 STRATZ per 100 matches) |
| players | `find_player_weaknesses` | yes | 8 | 1 |  | 15m | C / A | 0.914 | 0.014 | profile, matches, /heroStats, ≤5 /benchmarks, STRATZ details per 100 matches |
| players | `get_player_hero_pool` | yes | 8 | 0 |  | 15m | C / A | 0.910 | 0.014 | profile, matches, /heroStats, ≤5 /benchmarks |
| players | `get_player_peers` |  | 3 | 0 |  | 15m | B / A | 0.344 | 0.014 | profile, matches, peers |
| players | `get_player_profile` |  | 3 | 0 |  | 15m | B / A | 0.344 | 0.014 | profile, wl, recentMatches |
| players | `get_player_stats` |  | 2 | 1 |  | 15m | B / A | 0.244 | 0.014 | profile + matches; STRATZ details per 100 matches (≤5 at 500) |
| players | `get_player_trends` | yes | 2 | 0 |  | 15m | B / A | 0.240 | 0.014 | profile + matches |
| pro | `get_head_to_head` | yes | 2 | 0 |  | 6h | C / A | 0.286 | 0.014 | /teams (24h) + 1 explorer SQL |
| pro | `get_live_matches` |  | 2 | 0 |  | 60s | B / A | 0.240 | 0.014 | /live (60 s) + /leagues (24h) |
| pro | `get_pro_builds` | yes | 1 | 4 |  | 6h | C / A | 0.198 | 0.014 | hero pro matches + 3 aliased batches of 10 (30 matches) + public builds; a failed batch retries match by match |
| pro | `get_pro_matches` |  | 3 | 2 |  | 6h | B / A | 0.352 | 0.014 | /proMatches, /leagues, /teams + drafts in batches of 10 |
| pro | `get_pro_player` | yes | 1 | 1 |  | 6h | B / A | 0.140 | 0.014 | /proPlayers (24h) + STRATZ matches |
| pro | `get_team_stats` | yes | 1 | 3 |  | 6h | B / A | 0.148 | 0.014 | /teams + up to 3 STRATZ pages of 100 |
| pro | `get_tournament_stats` | yes | 3 | 0 |  | 6h | C / A | 0.390 | 0.014 | /leagues, 1 explorer SQL, /heroStats |
| reference | `lookup_reference` |  | 0 | 0 |  | — | A / A | 0.012 | 0.014 | bundled reference data, no request |
| trends | `find_meta_trends` | yes | 0 | 2 |  | 6h | B / A | 0.040 | 0.014 | patch list + HeroTrendSeries |
| trends | `get_hero_patch_history` | yes | 0 | 2 |  | 6h | B / A | 0.040 | 0.014 | patch list + HeroTrendSeries |
| trends | `get_patch_summary` | yes | 0 | 3 |  | 6h | B / A | 0.044 | 0.014 | patch list + HeroTrendSeries + PatchNotes |

**Totals.** Across the 60 tools, a cold call averages **1.6 OpenDota + 1.2 STRATZ requests**
(unweighted) and **$0.22 per 1,000 calls**. The most expensive is `get_records` with all 11
metrics, at $1.22 per 1k. A warm call averages **$0.026 per 1,000**. Of the cold figure, about
$0.04 per 1k is Vercel, and the rest is OpenDota Premium requests plus Redis.

**What a cold call uses up of the daily server quota** (OpenDota 2,500/day, STRATZ 4,000/day):

| Tool | Share of daily quota per cold call |
|---|---|
| `get_records` (all metrics) | 0.44 % OpenDota |
| `find_player_weaknesses`, `get_player_hero_pool` | 0.32 % OpenDota |
| `get_ability_stats` / `get_hero_builds`, `analyze_item` (all heroes), `get_item_overview` | 0.15 % STRATZ |
| match tools on an **unparsed** match | 1 request + 10 rate units for the parse request |
| typical B-class tool | 0.04–0.08 % of one source |

### Heavy tools

`HEAVY_TOOLS` holds 31 tools. They fall into four groups:

1. **Explorer SQL** (`analysis`, `economy`, `get_farming_stats`, `get_gold_xp_curves`,
   `get_item_payback`, `get_head_to_head`, `get_tournament_stats`). These make few requests but
   take seconds to tens of seconds of OpenDota database time. They are the most likely to
   time out or 429 and the most expensive in Vercel wall time.
2. **All-hero fan-out** (`get_item_overview`, `analyze_item`, `compare_items`, `get_tier_list`,
   `find_hidden_gems`, `find_meta_trends`, `get_hero_patch_history`, `get_patch_summary`,
   `get_neutral_items`, `find_hero_combos`). These are cheap once Postgres aggregates or the 6 h
   cache cover them.
3. **Players** (`compare_players`, `find_player_weaknesses`, `get_player_hero_pool`,
   `get_player_trends`). These run up to 8 OpenDota requests, and with a 15 min TTL they are
   usually cold, because every player is a new cache key.
4. **Pro / live** (`get_pro_builds`, `get_team_stats`, `get_pro_player`, `predict_live_win`).

Some tools are not heavy but deserve watching: the match tools (unparsed matches and parse
requests), `get_records` (11 requests, but cached 6 h), and `get_player_stats` with `matches=500`
(5 STRATZ queries).

### Monthly cost scenarios

Assumptions: 30 % of calls are cold, the tool mix is uniform, and the paid OpenDota tier is used.

| Calls / month | Variable cost | OpenDota requests / day | STRATZ requests / day | What breaks first |
|---|---|---|---|---|
| 30k (today's scale) | ≈ $2.5 (fits Hobby + free tiers: $0) | ≈ 490 | ≈ 350 | nothing |
| 300k | ≈ $25 | ≈ 4,900 (**over the 2,500 free cap → paid OpenDota needed**) | ≈ 3,500 (**at the 4,000 server cap**) | STRATZ quota, then OpenDota free cap |
| 3M | ≈ $250 | ≈ 48,500 | ≈ 35,000 (**over any STRATZ token, Individual = 20k**) | STRATZ, impossible without the Postgres aggregates serving most hero/item/matchup calls |

Fixed costs once Pro exists: Vercel Pro $20 (needed for any commercial use), Neon Launch about
$15–20 (the collector keeps compute awake), Upstash pay-as-you-go a few dollars, and payment-provider fees
(not researched here). That comes to **about $40–50 a month** before any variable cost.

Two more cost-related findings:

- **Vercel egress IPs are shared** with other Vercel customers. OpenDota counts its free limits
  per IP (`opendota.md`), so the 2,500/day server cap may effectively be lower. An OpenDota API key
  takes per-IP limits out of the question for about $0.15 a day at today's volume.
- **Neon Free** (100 CU-h/month) is the first free tier at risk from the collector itself, not
  from the tools: an always-on 0.25 CU uses about 180 CU-h. Check the Neon dashboard before
  counting on it.

## 3. What comparable products charge

| Product | Price | What the money buys | Source |
|---|---|---|---|
| Dotabuff Plus | **$6/month or $56/year** | TrueSight replay analysis (damage/CC breakdowns, vision map, combat log, death map, farm charts), personal hero rankings, dashboard, lifetime data retention, no ads | [dotabuff.com/plus](https://www.dotabuff.com/plus), read 2026-11-04 |
| STRATZ+ | no price on the page | Draft-assistant desktop app (neural-net pick grades, win probability by minute, lane outcomes). The page warns it currently only works in spectator mode after a Dota update | [stratz.com/plus](https://stratz.com/plus), read 2026-11-04 |
| STRATZ API | free, quota tiers by application | 10k–20k calls/day | [`stratz.md`](stratz.md) |
| OpenDota API | $0.01 per 100 calls | unlimited daily calls, 3,000/min | [opendota.com/api-keys](https://www.opendota.com/api-keys) |
| Valve Dota Plus (in-game, for scale) | $3.99/month at launch | in-game assistant, hero progression | [Gaming Bolt, 2018](https://gamingbolt.com/?p=328840) |

The anchors are a consumer subscription at about $4–6 a month and a developer API priced per
call at about $1 per 10k calls.

## 4. Candidate Pro features (ranked)

Ranking = value to users × feasibility, discounted by our cost and risk. Dependencies refer to
tracker tasks: replay parsing **p2-08**, auth **p4-09**, Developer API **p5-06** (prototype in
progress in a parallel session), Telegram **p4-11**.

| # | Feature | Value to users | Cost to us | Depends on | Risks |
|---|---|---|---|---|---|
| 1 | **Higher limits** (e.g. 10× calls and heavy calls per day, longer heavy timeouts) | High for power users and agent builders. Free limits (30/min, 1,000/day, heavy 150/day) are most likely to be hit by agents making many calls | Low: about $0.22 per 1k cold calls; quota is the real cost, so Pro limits must sit inside the server caps | **p4-09** (identity instead of IP) | Pro users using up shared quota and degrading the free tier. Mitigate with a separate Pro share of the server caps |
| 2 | **API keys / Developer API** (REST + MCP with a key, usage dashboard) | High for developers; a known market (OpenDota sells calls) | Low to medium: same per-call cost; support and docs | **p5-06**, **p4-09** | Reselling STRATZ/Steam data via a paid API is the least clear legal case (`data-rights.md` risks 1–2). Serve our own aggregates first |
| 3 | **Private player tracking** (follow your own and your friends' accounts, longer history, weekly progress) | High, and the core of Dotabuff Plus | Medium: per tracked player about 2–3 OpenDota requests per refresh plus storage; 1k players refreshed daily ≈ 3k requests/day (paid OpenDota ≈ $9/month) | **p4-09**, collector job | Personal data: opt-out already exists (p4-10); needs consent and deletion. The 15 min player TTL means no cache sharing |
| 4 | **Scheduled reports** (weekly personal or meta digest by Telegram/email) | Medium to high; builds on Weekly Meta Report (p5-04) | Low: reports are precomputed once per week from aggregates; per-user parts are like #3 | **p4-11**, p5-04, **p4-09** | Sending messages at scale (Telegram bot limits); deliverability |
| 5 | **Alerts** (a tracked player finished a game, a hero's win rate moved, a patch landed) | Medium | Low to medium: polling per tracked player is the expensive part | **p4-11**, #3 | Noisy alerts reduce value; polling cost grows with users |
| 6 | **Replay-parsed stats** for any match (no waiting for OpenDota parse; laning, vision, fights for every game) | High (TrueSight is Dotabuff's flagship) | **High**: a VPS (`replay-parsing.md`: one 4 vCPU box ≈ 8,600 replays/day, 60 MB download per replay) plus ops; Valve CDN keeps replays only about 7–14 days | **p2-08** | Ops burden, Valve CDN behaviour, storage; biggest build. Do it last |
| 7 | **Priority / reliability** (Pro calls get the paid OpenDota key and a reserved quota share, longer cache bypass, fresher data) | Medium, mostly invisible | Low | p4-09 | Hard to market on its own; bundle it with #1 |

Not recommended for Pro: a live in-game draft assistant (fair-play risk, `data-rights.md` §6,
and STRATZ+ already struggles there), and paywalling STRATZ-proprietary metrics (IMP, win
predictions).

## 5. Recommendation

1. **Don't charge yet.** At today's scale everything fits free tiers. Money becomes a factor
   only near 300k calls a month, and at that point the STRATZ quota, not the bill, is the limit.
   Keep the free tier as is.
2. **Unblock before building:**
   (a) ask STRATZ in writing whether a paid tier may use STRATZ-derived data (`data-rights.md`
   risk 1);
   (b) plan the move to **Vercel Pro ($20)**, because Hobby is non-commercial only and even a
   payment form counts as commercial;
   (c) buy an **OpenDota key** now. At current volume it costs well under $1 a month, removes
   the shared-IP risk and raises the minute limit;
   (d) push more tools onto the **Postgres aggregates**. Each tool moved there takes its STRATZ
   cost to zero for the common filters, which is what lets the free tier grow.
3. **First Pro bundle**, once auth (p4-09) lands, priced near the anchors (**$3–5 a month**;
   Dotabuff Plus is $6): #1 higher limits + #7 priority + #4 weekly personal report, all cheap
   to run. Fixed costs of about $40–50 a month are covered by about 10–15 subscribers.
4. **Developer API (#2)** as a separate per-call or per-key plan once p5-06 is stable and the
   legal questions are answered. Start with our own aggregates only.
5. **Then** private tracking and alerts (#3, #5), and **replay parsing (#6) last**, only if the
   survey shows demand for TrueSight-style analysis strong enough to pay for a VPS and ops.
6. **Validate with the survey below** before writing any billing code.

## 6. User survey draft (Russian, not published)

To post later in the Telegram channel or on the landing page. 9 questions, about 2 minutes.

1. **Как вы пользуетесь Dota Insights?**
   ☐ Через Claude / другой AI-клиент (MCP) ☐ Через Telegram ☐ Через сайт ☐ Пишу свой
   инструмент/бота поверх API ☐ Пока не пользуюсь
2. **Какой у вас ранг?** Herald–Crusader / Archon–Legend / Ancient–Divine / Immortal / Не играю
   ранкед
3. **Как часто вы задаёте вопросы серверу?** Каждый день / Несколько раз в неделю / Несколько
   раз в месяц / Реже
4. **Упирались ли вы в лимиты** (сообщение «слишком много запросов» или таймаут)? Да, часто /
   Иногда / Ни разу
5. **Что из этого вы бы реально использовали?** (выберите до трёх)
   ☐ Более высокие лимиты запросов ☐ Разбор любого своего матча без ожидания парсинга
   (линия, вижен, драки) ☐ Отслеживание своих аккаунтов и друзей с историей прогресса
   ☐ Еженедельный персональный отчёт в Telegram ☐ Уведомления (сыгранная игра, изменения
   меты, новый патч) ☐ API-ключ для своих проектов ☐ Ничего из этого
6. **Какая одна функция самая важная для вас** из списка выше и почему? (свободный ответ)
7. **Платите ли вы сейчас за статистику по Dota?** ☐ Dotabuff Plus ☐ STRATZ+ ☐ Dota Plus
   ☐ Другое: ___ ☐ Нет
8. **Сколько вы готовы платить в месяц за выбранные функции?** 0 (только бесплатно) / до $2 /
   $2–5 / $5–10 / больше $10
9. **Как вам удобнее платить?** Банковская карта / Telegram Stars / Криптовалюта / Другое: ___

Optional last field: «Оставьте Telegram-ник, если хотите первыми попробовать Pro» (stored
only with consent, per `docs/privacy.md`).

## 7. Open questions and follow-ups

- Re-run section 2 with real p50/p95 and cold/warm shares from `usage_events` on Neon (the
  report already computes them), and weight the averages by actual tool mix.
- Measure actual Vercel Active CPU and memory per invocation from the Vercel usage dashboard;
  the CPU figures above are estimates from mocked runs.
- Payment provider and fees (cards, Telegram Stars) were not researched.
- STRATZ written answer on commercial use; Individual token application if STRATZ traffic
  approaches 4,000/day.

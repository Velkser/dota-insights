# Replay parsing: own parser vs OpenDota parsed data

Tracker task p1-10. Researched 2026-11-04 from public repositories, package indexes and
documentation only. No third-party binaries were downloaded or run, so every speed or size
figure below is quoted from its source (linked) or marked as an **assumption**.

## TL;DR

- **Recommendation: hybrid.** Use OpenDota parsed match data (`/matches/{id}`, plus
  `POST /request/{id}` to trigger a parse) as the default source for `analyze_match`, vision
  and laning. Add our own parser later, as a separate worker, only for the features OpenDota
  does not expose: per-hit combat log around a death (`analyze_death`), per-second hero
  positions, and our own teamfight definition.
- If we run our own parser, use **odota/parser** (Java, MIT) as a sidecar HTTP service. It
  is the parser OpenDota itself runs, it is actively maintained (last commit 2026-10-24), and
  its raw line-delimited JSON already carries the events we need. Fall back to **gem-dota**
  only if we must stay pure Python and can accept roughly 30-160 s per replay.
- Replays live on Valve's CDN for a limited time (commonly cited as 7-14 days, no official
  figure), and building the URL needs `cluster` + `replay_salt`, which only the Steam Game
  Coordinator returns. OpenDota already returns `replay_url` for matches it has GC data for,
  so we do not need our own Steam bot.
- Cost: on the stated assumptions, about 1,000 matches/day fits on one small 4 vCPU / 8 GB
  VPS at a fraction of its capacity. The real constraints are replay availability and
  download bandwidth, not CPU.

## What our tools need

| Tool / feature | Data needed | In OpenDota parsed data? |
|---|---|---|
| `analyze_match` | per-minute gold/xp/lh, purchases, kills, objectives, advantage graphs | Yes: `gold_t`, `xp_t`, `lh_t`, `dn_t`, `purchase_log`, `kills_log`, `objectives`, `radiant_gold_adv`, `radiant_xp_adv` |
| teamfights | fight windows, per-player damage/deaths/gold delta | Yes, `teamfights`, with OpenDota's fixed rule (below) |
| vision | ward placements and removals with coordinates and time | Yes: `obs_log`, `sen_log`, `obs_left_log`, `sen_left_log` |
| laning | early positions, lane assignment, lh/denies at 10 min | Partly: `lane_pos` (position heatmap), `lane_role`, `lh_t`/`dn_t`; no raw position timeline |
| `analyze_death` | every damage instance to the victim in the seconds before death, who was nearby, what was used | **No.** Only aggregates (`damage_taken`, `killed_by`, `damage_inflictor_received`) and `kills_log` timestamps. Needs the raw combat log + positions |

The parsed field names above come from odota/parser's `processors/parseSchema.mjs` and the
odota/core match response schema.

## Parser candidates

Repository data was read through the GitHub API on 2026-11-04.

| | odota/parser | skadistats/clarity | dotabuff/manta | gem-dota | Rupas1k/source2-demo |
|---|---|---|---|---|---|
| Language | Java 21 (+ Node.js post-processors) | Java 21 (library) | Go (library) | Pure Python ≥ 3.10 | Rust (library) |
| License | MIT | BSD-3-Clause | MIT | MIT | Apache-2.0 |
| Last commit | 2026-10-24 | 2026-10-29 | 2026-10-23 | 2026-10-30 (PyPI 0.14.0, 2026-10-26) | 2026-10-09 |
| Latest release | none tagged; pins clarity 5.0.1 | v5.0.1 (2026-10-05) | v1.5.2 (2026-10-23) | 0.14.0 (28 releases since 2026-03) | crate 0.5.x |
| Stars | 162 | 775 | 696 | 158 | 73 |
| Kind | ready HTTP service | low-level library | low-level library | high-level library | low-level library |
| Output | line-delimited JSON events, or a parsed-match JSON blob (`/blob`) | Java callbacks, raw protobufs | Go callbacks, raw protobufs | dataclasses, pandas DataFrames, JSON, Parquet | Rust callbacks |
| Who uses it | OpenDota production | odota/parser, clarity-examples | Dotabuff | independent project (created 2026-03) | small |

### odota/parser

- **What it is.** A small Java HTTP server (port 5600) built on clarity. `POST` a `.dem` to
  `/` and it streams line-delimited JSON events. `GET /blob?replay_url=...` downloads the
  replay itself, decompresses it (bzip2 or zstd, detected by magic bytes), parses it and runs
  the Node.js processors, returning the same parsed-match JSON that OpenDota stores. A
  `Dockerfile` (`maven:3.9.12-eclipse-temurin-21-noble`, JVM `-XX:MaxRAMPercentage=75.0`) is
  included. `/healthz` exists for health checks.
- **Events emitted** (`Parse.java`, `Entry.java`): every combat log entry
  (`DOTA_COMBATLOG_DAMAGE`, `HEAL`, `MODIFIER_ADD/REMOVE`, `DEATH`, `ABILITY`, `ITEM`,
  `PURCHASE`, `GOLD`, `XP`, `BUYBACK`, ...) with attacker, target, inflictor, value and
  illusion/hero flags; `interval` entries **every 1 game second per player** (`INTERVAL = 1`)
  with x/y position, gold, xp, lh, denies, net worth, level, KDA, stuns, hero damage, tower
  damage, wards placed, stacks, runes and teamfight participation; ward placed/expired/killed
  with coordinates (`obs`, `sen`, `obs_left`, `sen_left`); player actions, pings, chat and
  chat wheel; draft timings; pauses; neutral item history; starting items; epilogue.
- **Teamfights** (`processors/processTeamfights.mjs`): a fight starts at the first hero
  death (minus a 15 s cooldown), extends while hero deaths keep happening within 15 s, and is
  kept only if at least 3 heroes died. It is a fixed heuristic, so a 2-death skirmish is never
  a fight.
- **Speed.** odota/core's `svc/fetcher/ParsedFetcher.ts` records one timing for match
  7503212404: download 2,967 ms, bunzip2 6,716 ms, parse 9,407 ms, post-process 3,278 ms,
  so about 22 s end to end. Hardware and match length are not stated.
- **Resources.** `Main.java` notes that each `/blob` request holds the raw, decompressed and
  parsed replay in memory at once, and caps the thread pool at
  `min(cores × 6, 24)` for that reason. Peak memory per replay is not documented.

### skadistats/clarity

- The underlying Java library (Dota 2, CS2, Deadlock). Exposes combat log, entities (heroes,
  creeps, wards, with all networked properties, so positions at any tick), modifiers, temp
  entities, user messages, game events, sounds and the end-of-game overview.
- Very active: 5.0.0 (2026-10-04) moved to Java 21 and relocated its protobuf runtime.
- No published per-replay benchmark found; the repo describes itself as "comically fast".
  Using it directly means writing the extraction that odota/parser already has.

### dotabuff/manta

- Dotabuff's Go parser, low level: callbacks for every protobuf message and entity update,
  no opinion on output structure.
- Benchmark from PR #183 (merged 2026-10-23): one full match (`BenchmarkMatch8552595443`)
  parses in **405 ms and allocates 111.8 MiB** on an Apple M4 Pro, down 33 % from the
  previous master. That measures decoding without any extraction work, so a real pipeline
  will be slower.
- Fastest documented option, but we would have to write all event extraction (combat log
  normalization, ward tracking, intervals, teamfights) in Go ourselves.

### Python options

- **gem-dota** (`whanyu1212/gem-dota`, PyPI `gem-dota`): the only maintained pure-Python
  Source 2 parser found. High-level output (players, combat log, positions, wards, smokes,
  runes, fights, laning, per-minute curves) as dataclasses, DataFrames, JSON or Parquet, plus
  `gem.fetch_replay()` that downloads via OpenDota and `gem.parse_many()` for process-pool
  batches. Its README says per-player totals and per-minute curves match OpenDota exactly on
  its test replays, and flags fights, smokes and Roshan records as experimental.
  Its published performance study (Apple M2, median of 3): a 23-minute pro replay (94.4 MiB
  `.dem`) parses in 30.6 s on Python 3.12 (45.7 s on 3.10), and a long TI2026 replay
  (367.6 MiB) in 96.4 s on 3.12; peak RSS about 196 MiB and 624 MiB respectively.
  It is young (created 2026-03, pre-1.0, 158 stars) and roughly 75× slower than manta's
  benchmark.
- **smoke** and **tarrasque** (skadistats): archived since 2015 and 2014, Source 1 only.
  Not usable.
- **demoparser2** (LaihoE, Rust with Python bindings): Counter-Strike 2 only.
- Other GitHub hits (C#, Haskell, Rust hobby projects, Alice in C++) are either abandoned or
  have no users; not considered.

## How replays are obtained

1. **Replay URL.** odota/core `svc/util/utility.ts` `buildReplayUrl()`:

   ```
   http://replay{cluster}.valve.net/570/{match_id}_{replay_salt}.dem.bz2
   ```

   Clusters 413, 415 and 417 use `dota2.com.cn` instead of `valve.net`. A `.meta.bz2` file at
   the same path holds only the match metadata protobuf. odota/core notes that Valve switched
   meta files from bzip2 to Zstandard around 2026-07-27 while keeping the `.bz2` extension,
   and odota/parser detects bzip2 or zstd by magic bytes for the replay too, so we must not
   trust the extension.
2. **cluster + replay_salt.** These are not in the public Steam Web API. They come from the
   Dota 2 Game Coordinator (`CMsgGCMatchDetailsRequest`), which needs a logged-in Steam
   account running a GC client. OpenDota runs this as its "retriever" service
   (`svc/fetcher/GcdataFetcher.ts`) and stores `cluster` and `replay_salt` per match.
3. **Through OpenDota (recommended).** `GET /matches/{id}` returns `replay_url` whenever
   OpenDota has the salt (`svc/util/buildMatch.ts`). `POST /request/{id}` asks OpenDota to
   fetch GC data and parse the match; per its OpenAPI spec this counts as 10 calls for rate
   limiting (but not billing). Rate limits and pricing are covered in the OpenDota research
   doc from the sibling session. gem-dota's `fetch_replay` uses the same route.
4. **Retention.** Valve publishes no figure. Community guides cite about 7 or 8 days, others
   about two weeks. odota/core checks availability with a `HEAD` request before parsing and
   treats a failure as "Replay not found". Practical rule: parse within a few days of the
   match, and treat older matches as OpenDota-only. Since the 2026-06-18 update Valve also
   removed in-game "Watch Game" for players above 8,500 MMR (Hawk Live); no source says
   replay files themselves changed, but this should be checked against a real high-MMR match.
5. **Size.** No official figure. Uncompressed `.dem` data points from gem-dota: 94.4 MiB for a
   23-minute pro match, 367.6 MiB for a long TI2026 match. Compressed size is not documented
   anywhere we found; it needs measuring. For comparison, a full OpenDota parsed-match JSON
   is about 290-370 KB (odota/core test fixtures `1781962623_parsed.json`,
   `7490235544_parsed.json`).

## Cost estimate for N matches/day

**Assumptions (all to be measured before committing):**

- A1. Pipeline is odota/parser `/blob`. Per-replay CPU cost is about 20 s of one core
  (bunzip 6.7 s + parse 9.4 s + post-process 3.3 s from OpenDota's timing, rounded),
  treated as equal on a shared VPS vCPU. Download is I/O and overlaps.
- A2. Average compressed replay is about 60 MB (guess between nothing-documented and the
  94-368 MiB uncompressed points, assuming bzip2/zstd saves about 50-70 %).
- A3. Peak memory about 1 GB per concurrent parse (raw + decompressed + output held at once,
  per `Main.java`; scaled from gem's 200-620 MiB RSS for a slower but leaner parser).
- A4. We keep the parsed JSON (~350 KB, about 80 KB gzipped, **assumption**) and delete the
  `.dem` after parsing.
- A5. VPS: 4 vCPU, 8 GB RAM, 50 % target utilization. Hetzner's cloud page lists shared vCPU
  plans starting at €5.99-6.49/month (excl. VAT, observed 2026-11-04); a 4 vCPU / 8 GB plan
  costs more and the exact price should be checked when ordering.

**Formulae:** CPU-hours/day = N × 20 s / 3600; capacity ≈ cores × 86,400 × 0.5 / 20;
ingress/day = N × 60 MB; storage/month = N × 30 × 80 KB.

| N matches/day | CPU-hours/day | Concurrency needed | Ingress/day | Parsed storage/month |
|---|---|---|---|---|
| 100 | 0.6 | 1 | 6 GB | 0.24 GB |
| 1,000 | 5.6 | 1 (≈ 0.23 cores avg) | 60 GB | 2.4 GB |
| 5,000 | 27.8 | 2-3 | 300 GB | 12 GB |

Capacity under A1/A5 is about 8,600 matches/day on one 4 vCPU box, memory-limited to about
6 concurrent parses (A3). So on these assumptions one small VPS covers up to a few thousand
matches/day; above that, ingress traffic (9 TB/month at 5,000/day) and Valve CDN behavior
become the limits. Many VPS providers do not bill ingress, but check the plan. With gem-dota
instead of odota/parser, multiply the CPU column by about 2-8 (30-160 s per replay on M2,
likely slower on a shared vCPU).

## Recommendation: hybrid

1. **Phase 1 (now): OpenDota parsed data only.** It already covers `analyze_match`, vision,
   teamfights and most of laning with production-tested logic, costs us nothing to run, and
   OpenDota keeps the parsed result long after Valve deletes the replay. Request parses for
   matches that are not parsed yet with `POST /request/{id}`.
2. **Phase 2 (when `analyze_death` lands): own parser for the gaps.** Run odota/parser as a
   Docker sidecar, call it with the `replay_url` from OpenDota, and store the raw event lines
   we need (combat log entries for each hero death window, 1 s position intervals) next to
   OpenDota's blob. Reasons for odota/parser over the others: it is a ready service, MIT
   licensed, maintained by the same team whose data we already consume, and its output is a
   superset of OpenDota's, so the two sources reconcile field by field. Our Python code talks
   to it over HTTP, which keeps Java out of our package.
3. **Not recommended:** writing on clarity or manta directly (we would rebuild odota/parser's
   extraction logic), or making gem-dota the core path (pure Python fits our stack and its
   API is the nicest, but it is pre-1.0, a single maintainer, and an order of magnitude
   slower). Re-evaluate gem-dota at 1.0 for notebook-style research work.

Risks to track: replays expire within days, so own-parser features only work for recent
matches; Valve changed the meta file compression silently in 2026-07, so formats can change
without notice; and the 2026-06 high-MMR change may extend to replay files.

## Open questions to verify with a real replay

- Compressed `.dem.bz2` size and decompressed size for a typical 35-40 minute pub.
- odota/parser wall time and peak RSS on our actual VPS.
- How many days a replay stays downloadable (track `HEAD` on a sample of matches daily).
- Whether high-MMR matches still have downloadable replays after the 2026-06-18 change.

## Sources

- odota/parser: https://github.com/odota/parser (`src/main/java/opendota/Parse.java`,
  `Main.java`, `Entry.java`, `processors/processTeamfights.mjs`, `processors/parseSchema.mjs`,
  `Dockerfile`, `pom.xml`)
- odota/core: https://github.com/odota/core (`svc/util/utility.ts`,
  `svc/fetcher/ParsedFetcher.ts`, `svc/fetcher/GcdataFetcher.ts`, `svc/fetcher/MetaFetcher.ts`,
  `svc/util/buildMatch.ts`, `svc/api/spec.ts`, `json/` fixtures)
- skadistats/clarity: https://github.com/skadistats/clarity (README, CHANGELOG)
- skadistats/clarity-examples: https://github.com/skadistats/clarity-examples
- dotabuff/manta: https://github.com/dotabuff/manta and PR
  https://github.com/dotabuff/manta/pull/183
- gem-dota: https://pypi.org/project/gem-dota/, https://github.com/whanyu1212/gem-dota,
  performance study https://whanyu1212.github.io/gem-dota/deep-dives/parser-performance
- Rupas1k/source2-demo: https://github.com/Rupas1k/source2-demo
- skadistats/smoke, skadistats/tarrasque (archived), LaihoE/demoparser (CS2)
- Replay retention claims: https://www.oneesports.gg/dota2/what-is-dota-2-size-in-2024/,
  https://gamerant.com/how-to-watch-replays-in-dota-2/
- High-MMR replay change: https://hawk.live/posts/valve-completely-removed-ability-watch-replays-pro-players-public-matches
- Hetzner Cloud pricing page: https://www.hetzner.com/cloud

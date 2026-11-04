# STRATZ API research

Tracker task: p1-02. Researched 2026-11-04 from public pages only. We have no STRATZ token,
so **nothing below was tested against the live API**. Where a statement comes from a
third-party source rather than STRATZ itself, it is marked as such.

## Sources

| Source | What it gave us |
| --- | --- |
| <https://stratz.com/api> | Token tiers, rate-limit table, usage conditions (page rendered in Russian for us; quotes below are our translation) |
| <https://stratz.com/knowledge-base/API> | English FAQ: tokens, rate limits, "is it free", STRATZ vs OpenDota |
| [TheAmazingLooser/STRATZ_Models](https://github.com/TheAmazingLooser/STRATZ_Models) | C# types generated from the STRATZ GraphQL schema, commit `b83999e` (2024-08-25). Used for the schema overview. **May be out of date.** |
| [odota/core `dev/playerHistory.ts`](https://github.com/odota/core/blob/master/dev/playerHistory.ts) | A real request against `https://api.stratz.com/graphql`, showing the headers it sends |

`curl` and `WebFetch` get HTTP 403 (Cloudflare) on `stratz.com` and `api.stratz.com`. Pages
were read through a normal browser.

## Access

- **Endpoint:** `POST https://api.stratz.com/graphql` (GraphQL; the old REST API at
  `/api/v1/...` is legacy, and STRATZ says it "moved from REST to GraphQL").
- **Auth:** `Authorization: Bearer <token>`.
- **Headers seen in practice (third-party, unverified):** OpenDota's own script sends
  `user-agent: STRATZ_API` and `graphql-require-preflight: 1`. Community reports say
  requests without that User-Agent get blocked by Cloudflare. Treat it as required until
  proven otherwise.
- **How to get a token:** sign in on stratz.com **with a Steam account**; a *Default Token*
  is issued automatically and shown under "My Tokens" on `/api`. Higher tiers need an
  application form. Per the task brief we did **not** sign in or create anything. Someone
  on the team has to do it with their own Steam account. The token is tied to that person.

### Token tiers and rate limits

From the `/api` comparison table, confirmed by the English FAQ "Are there any rate limits?":

| Tier | How to get it | Meant for | Required referral clicks / month | calls/s | calls/min | calls/h | calls/day |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Default | Sign in with Steam | "small personal projects" | none | 20 | 250 | 2,000 | 10,000 |
| Individual | Application form | "web applications and community projects" | 1,000 | 20 | 250 | 4,000 | 20,000 |
| Multi-Token | Application form | Desktop apps; one sub-token per end user | 5,000 | 20 / user | 20 / user | 50 / user | 100 / user |

- Windows are fixed buckets: "if you use up all of your available Calls per Hour, you will
  not be able to request any new data until the top of the next hour."
- Remaining quota is visible only on the "My Tokens" panel (no header contract documented
  publicly; it may exist, but we couldn't check).
- One GraphQL request = one call, regardless of how many fields it asks for. That is the main
  reason to batch many sub-queries (aliases) into one request.
- Individual/Multi tokens come with a referral code. STRATZ "will periodically check" referral
  traffic, and a token that falls short "may be deactivated".

**What this means for us:** a Default Token gives 10k calls/day, or 20k with an Individual
token. That is enough for a collector that fetches *aggregates* (hero stats, matchups, builds)
a few times a day. It is not enough to proxy STRATZ live for every MCP user request. Cache
everything server-side.

## Schema overview

Root query type `DotaQuery` (from the 2024 model dump):
`match`, `matches`, `player`, `players`, `team`, `teams`, `league`, `leagues`, `guild`,
`heroStats`, `constants`, `leaderboard`, `live`, `stratz`, `plus`, `vendor`, `yogurt`.

Common filter enums:

- `RankBracketBasicEnum`: `UNCALIBRATED`, `HERALD_GUARDIAN`, `CRUSADER_ARCHON`,
  `LEGEND_ANCIENT`, `DIVINE_IMMORTAL`, `FILTERED`, `ALL` (pairs of medals, not single medals).
- `MatchPlayerPositionType`: `POSITION_1`..`POSITION_5`, `UNKNOWN`, `FILTERED`, `ALL`.
- `week` arguments take a unix timestamp of the week start. Hero aggregates are bucketed by
  week, day, hour, month and game version.

### Hero stats by position / bracket: `heroStats`

| Field | Key args | Returns (selected) |
| --- | --- | --- |
| `stats` | `heroIds`, `week`, `bracketBasicIds`, `positionIds`, `groupByTime/Position/Bracket`, `minTime`, `maxTime` | `matchCount`, `winCount`, kills/deaths/assists, `networth`, `cs`, `goldPerMinute`, `xp`, hero/tower damage, stuns, healing, `campsStacked`, `stompWon/Lost`, `comeBackWon/Lost`, … (per time bucket when grouped by time) |
| `winWeek` / `winDay` / `winHour` / `winMonth` / `winGameVersion` | `heroIds`, `bracketIds`, `positionIds`, `regionIds`, `gameModeIds`, `groupBy` | win/match counts over time, which is what meta trends need |
| `banDay` | | ban counts by day |
| `talent`, `abilityMinLevel`, `abilityMaxLevel` | hero, bracket, position | talent pick/win rates, ability level timings |

### Matchups: `heroStats.matchUp`, `heroStats.heroVsHeroMatchup`

- `matchUp(heroId | heroIds, week, bracketBasicIds, orderBy, matchLimit, take, skip)` returns,
  for each hero, `with[]` and `vs[]` lists with `synergy`, `winsAverage`, `matchCount`,
  `winCount` and per-hero averages.
- `heroVsHeroMatchup(heroId, bracketBasicIds, matchLimit, take)` → `advantage[]` / `disadvantage[]`.

### Item and ability builds

- `heroStats.itemFullPurchase(heroId, week, bracketBasicIds, positionIds, minTime, maxTime, matchLimit)`:
  `itemId`, `instance`, `time`, `matchCount`, `winCount`, `winsAverage`. This means win rate by
  item *and timing*.
- `heroStats.itemStartingPurchase`, `itemBootPurchase`, `itemNeutral`: starting items, boots and
  neutral items, each with win rates.
- `heroStats.guide(heroId, withHeroId, againstHeroId, isPro, positionId, itemId, neutralItemId)`:
  real high-rank/pro matches used as "guides" (item order, match, player).
- Per-match ability order: `match.players.playbackData.abilityLearnEvents`.

### Lane outcomes

- `heroStats.laneOutcome(isWith, heroId, bracketBasicIds, positionIds)`: `winCount`, `drawCount`,
  `lossCount`, `stompWinCount`, `stompLossCount`, `matchWinCount` (how often winning the lane
  turns into winning the match).
- Per match: `match.topLaneOutcome` / `midLaneOutcome` / `bottomLaneOutcome`, `match.laneReport`.

### Match events: `match(id)`

- Header: `didRadiantWin`, `durationSeconds`, `gameMode`, `lobbyType`, `rank`, `bracket`,
  `league`, `series`, teams, `pickBans`, `towerDeaths`, `chatEvents`.
- Timelines: `radiantNetworthLeads`, `radiantExperienceLeads`, `radiantKills`, `direKills`,
  `winRates`, `predictedWinRates` (STRATZ's own win-probability model).
- `match.playbackData` (map-level): courier, rune, ward, building, tower-death and **Roshan** events.
- `match.players[]`: KDA, last hits, GPM/XPM, `lane`, `position`, `role`, `imp` (STRATZ's
  performance metric), `award`, `intentionalFeeding`, and:
  - `stats`: per-minute arrays (`lastHitsPerMinute`, `goldPerMinute`, `networthPerMinute`,
    `impPerMinute`, …), `farmDistributionReport`, `wards`, `itemPurchases`, `runes`,
    `heroDamageReport`, `killEvents`, `deathEvents`, `assistEvents`.
  - `stats.deathEvents[]`: `time`, `attacker`, `byAbility`, `byItem`, `goldLost`, `goldFed`,
    `xpFed`, `timeDead`, position x/y, and context flags such as `isWardWalkThrough`,
    `isAttemptTpOut`, `isDieBack`, `isBurst`, `isEngagedOnDeath`, `hasHealAvailable`,
    `isTracked`. This is the closest ready-made input for `analyze_death`.
  - `stats.killEvents[]`: `isSolo`, `isGank`, `isInvisible`, `isSmoke`, `isTpRecently`.
  - `playbackData`: full event streams (positions, gold, health, purchases, ability/item use,
    buybacks). These are large; request them only when needed.
- Only parsed matches have these fields: `match.isStats` / `parsedDateTime` show whether the
  replay was parsed. `stratz.matchRetry` can ask for a reparse. That is a mutation-like
  field, and we haven't checked its quota cost.

### Player data: `player(steamAccountId)`

`steamAccount`, `matchCount`, `winCount`, `ranks`, `leaderboardRanks`, `names` (name history),
`behaviorScore`, `activity`, `simpleSummary`, `performance` (per-hero filters: IMP, streaks,
award counts, averages by position), `heroesPerformance(request)` (per-hero win/KDA/GPM/IMP),
`matches(request: PlayerMatchesRequestType)`, `matchesGroupBy`, `heroStreaks`, `feats`.
Private data (exact MMR) is not exposed. The FAQ says "nothing private like actual MMR numbers".

### Pro / league data

- `league(id)` / `leagues(request)`: tier, prize pool, dates, `matches(request)`, `series`,
  `nodeGroups` (brackets), `standings`, `stats`, `liveMatches`, `streams`.
- `team(id)` / `teams`: roster (`members`), `matches`, `series`, `heroPickBan`, `leagues`.
- `constants.proSteamAccounts`, `constants.popularTeamIds`, `constants.tiWinners`.
- `heroStats.guide(isPro: true)` for pro builds.

### Live: `live.match(id)`, `live.matches(request)`

`MatchLiveType`: scores, `gameTime`, `radiantLead`, `buildingState`, teams/league, `players[]`
(hero, KDA, net worth, `respawnTimer`, `ultimateState`, `position`, `impPerMinute`),
`liveWinRateValues`, `winRateValues`, `insight`, `delay`, `spectators`.

### Constants

`constants.heroes / items / abilities / gameVersions / patchNotes / facets / regions /
gameModes / lobbyTypes / npcs / modifiers`. These are a reasonable alternative to
`dotaconstants`, but they cost quota. Cache them for each game version.

## What STRATZ has that OpenDota lacks

Based on STRATZ's own FAQ plus the schema. Before relying on any OpenDota "lacks" claim,
check it against the OpenDota research doc.

- **GraphQL**: many sub-queries per request, and it counts as one call against quota.
- **Minute-by-minute stats** for many match statistics. The FAQ says STRATZ "is the only
  API to offer advanced minute-by-minute stats on many key match statistics".
- **Proprietary metrics**: IMP, position detection, lane outcomes with stomp/draw
  classification, predicted win rates, death-context flags (`isBurst`, `isAttemptTpOut`, …).
- **Hero aggregates by position (1–5) and paired rank bracket**, bucketed by week, day or
  hour: win rates, item timings, starting items, neutral items, talents, lane outcomes.
  OpenDota's public aggregates are mostly per rank tier and not per position (uncertain;
  confirm in the OpenDota doc).
- **Live matches with a win-probability series** (`liveWinRateValues`).
- Player behavior score, name history, guild data, Dota Plus hero levels.

What STRATZ lacks or is weaker at, compared with OpenDota:

- No public SQL explorer or bulk data dumps.
- Strict daily quota (10k–20k/day), versus OpenDota's paid unlimited tier.
- You need a Steam login to get a token, and higher tiers need an application plus
  referral-traffic requirements.

## Tool mapping

Legend: **available**: one query or a few give what the tool needs. **partial**: the raw data
exists but we must compute the result or combine sources. **missing**: STRATZ has nothing
usable.

| Planned tool | Status | STRATZ queries | Notes / gaps |
| --- | --- | --- | --- |
| `compare_heroes` | available | `heroStats.stats(heroIds:[a,b], bracketBasicIds, positionIds)`, `heroStats.matchUp(heroId:a)` | Win rate, KDA, GPM, damage and so on, side by side. Head-to-head comes from `vs[]`. |
| `get_farming_stats` | partial | Aggregate: `heroStats.stats` (`cs`, `neutrals`, `networth`, `goldPerMinute`, `campsStacked`, grouped by time). Per match: `players.stats.lastHitsPerMinute/networthPerMinute/farmDistributionReport` | Farm-efficiency benchmarks (for example "GPM percentile at 10 min") we compute ourselves. |
| `analyze_item` | available | `heroStats.itemFullPurchase` (win rate by timing), `itemStartingPurchase`, `itemBootPurchase`, `itemNeutral` | Item usage *across all heroes* needs one query per hero. Batch with aliases. |
| `analyze_death` | partial | `match.players.stats.deathEvents` (+ `killEvents`, `playbackData.playerUpdatePositionEvents`) | The raw context flags are excellent. "Why did I die" reasoning and aggregation over a player's matches is our logic. Parsed matches only. |
| `find_meta_trends` | available | `heroStats.winWeek/winDay/winGameVersion`, `heroStats.banDay`, `heroStats.stats(groupByTime)` | Trend detection (rising or falling heroes) we compute from the series. |
| `analyze_match` | available | `match(id)` with `players`, `stats`, `pickBans`, lane outcomes, `winRates`, `playbackData` | Rich. Needs a parsed match. Large payloads, so select fields carefully. |
| `get_player_stats` | available | `player(steamAccountId)` → `performance`, `heroesPerformance`, `matches`, `ranks`, `simpleSummary` | Profiles marked private, or with "Expose Public Match Data" off, show up anonymous or empty. |
| hero matchups | available | `heroStats.matchUp`, `heroStats.heroVsHeroMatchup` | Synergy and advantage values from STRATZ. |
| hero builds | available | `heroStats.itemFullPurchase`, `guide`, `talent`, `abilityMinLevel/MaxLevel` | Skill order *aggregates* are partial: only min/max level per ability is exposed, not full orders. Full orders come from guide matches. |
| hero lane stats | available | `heroStats.laneOutcome(isWith, heroId, positionIds)` | Lane-vs-specific-hero is partial: `isWith` with/against splits exist, but per-opponent granularity is unconfirmed. |
| draft | partial | `heroStats.matchUp` (with/vs synergy), `heroStats.stats` by position | No draft-recommendation endpoint. We build the scorer. Pro drafts: `team.heroPickBan`, `match.pickBans`. |
| pro scene | available | `leagues`, `league.matches/series/standings/stats`, `team`, `constants.proSteamAccounts` | Tier filters exist (`LeagueType.tier`). |
| live | available | `live.matches`, `live.match(id)` (`liveWinRateValues`, `players`) | Live polling eats quota fast. Poll on a schedule and cache, never once per user. |

Overall: STRATZ covers or partly covers every planned tool. It is the strongest source for
position-specific hero aggregates, lane outcomes and death context. The constraints are the
quota and the terms (see `data-rights.md`).

## Open questions (need a token or a STRATZ reply)

1. Is the `STRATZ_API` User-Agent actually required, and are there response headers with
   remaining quota?
2. Does a single heavy query (for example `playbackData` for 10 players) count as one call,
   or is there query-complexity limiting? We couldn't verify.
3. Current schema drift since the 2024 model dump (new facets, Dota 7.3x+ fields). Run
   introspection once a token exists, then regenerate this overview.
4. Whether an Individual Token can be approved for a project with a paid tier (see `data-rights.md`).

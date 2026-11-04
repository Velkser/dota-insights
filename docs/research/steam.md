# Steam Web API research (Dota 2)

Tracker task: p1-03. Researched 2026-11-04 from public docs. We have **no Steam Web API key**,
so the Dota endpoints were not called with a key. Every `IDOTA2*` call without a key returns
HTTP 403.

## Sources

- Steam Web API Terms of Use: <https://steamcommunity.com/dev/apiterms> ("Last updated July 2010")
- Key registration: <https://steamcommunity.com/dev> and <https://steamcommunity.com/dev/apikey>
- Steamworks auth docs: <https://partner.steamgames.com/doc/webapi_overview/auth>
- `ISteamUser` reference: <https://partner.steamgames.com/doc/webapi/ISteamUser>
- Current method list (scraped from Steam by SteamDB):
  [SteamDatabase/SteamTracking `API/*.json`](https://github.com/SteamDatabase/SteamTracking/tree/master/API)
- Endpoint status reports:
  [ValveSoftware/Dota2-Gameplay#17910](https://github.com/ValveSoftware/Dota2-Gameplay/issues/17910),
  [ValveSoftware/Dota2-Gameplay#24383](https://github.com/ValveSoftware/Dota2-Gameplay/issues/24383)
- What OpenDota still calls in production: [odota/core `svc/util/http.ts`](https://github.com/odota/core/blob/master/svc/util/http.ts)

## SteamID64 ↔ 32-bit account_id

Dota APIs (OpenDota, STRATZ `steamAccountId`, `GetMatchDetails` `account_id`) use the 32-bit
**account ID**. Steam profile APIs use the 64-bit **SteamID64**. For individual accounts in the
public universe:

```
STEAM64_BASE = 76561197960265728          # 0x0110000100000000
account_id   = steamid64 - STEAM64_BASE   # == steamid64 & 0xFFFFFFFF
steamid64    = account_id + STEAM64_BASE
```

Worked example: `76561197960287930` → `22202`, which is `76561197960287930 & 0xFFFFFFFF`.
We checked this locally.

SteamID64 bit layout: universe (8 bits, `1` = Public) | account type (4 bits, `1` =
Individual) | instance (20 bits, `1` = desktop) | account ID (32 bits). Validation for user
input:

- Reject a 64-bit value unless universe == 1 and type == 1. Group and clan IDs have a different type.
- Legacy text form `STEAM_X:Y:Z`: `account_id = Z*2 + Y`.
- `[U:1:N]` form: `account_id = N`.
- Dota sends `4294967295` (`2^32-1`) as the account ID for **anonymous** players (those with
  "Expose Public Match Data" off). Treat it as "no player", never as a real account.
- Vanity URLs (`steamcommunity.com/id/<name>`) need `ISteamUser/ResolveVanityURL` (key required).

## Profile endpoints and privacy

| Endpoint | Notes |
| --- | --- |
| `ISteamUser/GetPlayerSummaries/v2?steamids=` | Up to **100** SteamID64s per call. Valve: "should only be used if your website needs to display information about a user." |
| `ISteamUser/ResolveVanityURL/v1?vanityurl=` | `url_type` 1 = profile (default), 2 = group, 3 = official game group. |
| `ISteamUser/GetFriendList/v1` | Returns HTTP **401** if the friend list is private. |
| `ISteamUser/GetPlayerBans/v1` | VAC/game/community bans. |

Privacy:

- `communityvisibilitystate` in `GetPlayerSummaries`: `3` means public. `1` means not visible
  to the caller (private or friends-only). Valve's Steamworks page shows both values but
  doesn't define them. The meanings come from Valve's older developer wiki, which we couldn't
  fetch today (bot challenge), so treat them as **likely but unverified**.
- Basic fields (persona name, avatar, profile URL) are returned even for private profiles.
  Fields such as real name, country and time created are only returned for public profiles
  (also from the wiki; unverified today).
- **Dota match privacy is separate from Steam profile privacy.** The in-game option "Expose
  Public Match Data" controls whether the account ID appears in match data. When it is off,
  the player appears anonymous in future matches. OpenDota notes that "existing data is not
  deleted". Our tools must handle anonymous slots.

## Dota 2 Web API interfaces (app 570)

Method list from SteamTracking (current as of 2026-11-04) and status evidence:

| Interface / method | Status | Evidence |
| --- | --- | --- |
| `IDOTA2Match_570/GetMatchHistoryBySequenceNum` | **Works.** This is the reliable way to get *all* new public matches. | OpenDota and others moved to it in 2024 (#17910). OpenDota's code still uses it. |
| `IDOTA2Match_570/GetMatchHistory` | Works (reported in 2025, #24383). Limited to recent matches; filters by `account_id`, `hero_id`, etc. | #24383 says "Another method works fine (`GetMatchHistory`)". OpenDota uses it as its key test. |
| `IDOTA2Match_570/GetMatchDetails` | **Broken or unreliable since patch 7.36 (May 2024).** Returns `{}` or HTTP 500. | #17910 (still open, no Valve reply, marked stale 2026-08); #24383 (Feb 2025, closed as stale 2026-10). We don't know if it works today. Test once we have a key. |
| `IDOTA2Match_570/GetLiveLeagueGames` | Listed. Probably works; OpenDota still calls it. | Not verified by us. |
| `IDOTA2Match_570/GetTopLiveGame`, `GetTopLiveEventGame`, `GetTopWeekendTourneyGames` | Listed. OpenDota calls `GetTopLiveGame`. | Not verified. |
| `IDOTA2MatchStats_570/GetRealtimeStats` | Listed (live match detail by `server_steam_id`). OpenDota calls it. | Not verified. |
| `IDOTA2Match_570/GetTeamInfoByTeamID`, `IDOTA2Teams_570/GetTeamInfo` | Listed. OpenDota calls both. | Not verified. |
| `IDOTA2Match_570/GetTournamentPlayerStats` (v1, v2) | Listed. Probably legacy. | Uncertain. |
| `IEconDOTA2_570/GetHeroes` | Listed. OpenDota calls it. | Not verified. `GetGameItems` is **no longer listed**. Use `dotaconstants` or game files for items. |
| `IEconDOTA2_570/GetTournamentPrizePool`, `GetRarities`, `GetEventStatsForAccount`, `GetItemCreators` | Listed. | Uncertain. |
| `IDOTA2Fantasy_570/GetFantasyPlayerStats`, `GetPlayerOfficialInfo` | Listed. | Uncertain. Probably legacy. |
| `GetLeagueListing` and other old league/schema methods | **Not listed.** Treat as removed. | SteamTracking list. |

Notes:

- `GetMatchHistoryBySequenceNum` returns full match objects in sequence order. Following the
  live stream "is pretty annoying to do on a single IP", and it lacks some fields (draft
  info, ability upgrades), per a commenter on #17910. Detailed per-match data (draft,
  ability upgrades, anything replay-derived) needs the Game Coordinator or replay parsing.
  That is covered in the separate replay research.
- Our plan should not depend on `GetMatchDetails`. Use OpenDota or STRATZ for per-match
  details, and Steam only for sequence ingestion, profiles and vanity resolution.

## Key requirements and limits

- "All use of the Steam Web API requires the use of an API Key." Register at
  <https://steamcommunity.com/dev/apikey> with a Steam account and a **domain name** for the
  application. You must accept the API Terms of Use.
- Pass the key as the `key` query parameter or the `x-webapi-key` header (Steamworks docs).
  Prefer the header so the key stays out of logs and URLs.
- **Limit:** "You are limited to one hundred thousand (100,000) calls to the Steam Web API
  per day." There is no documented per-second limit. In practice Steam returns HTTP 429 when
  you burst. Throttle to about 1 request/second per key (community practice, not documented).
- The key is "personal to you and specific to your Application". Keep it confidential, never
  ship it to clients, and don't share it with third parties.
- **Uncertain:** Valve support says *limited user accounts* (accounts that have spent less
  than $5) have restricted features. Community reports say they can't register a Web API key.
  We couldn't load the support article text today. Use an account that has made a purchase.
- Some reports say Steam Guard Mobile Authenticator is needed to view or register keys. We
  found no official statement (uncertain).

## Implications for Dota Insights

1. Use Steam for: sequence-number match ingestion (if we run our own collector), profile
   lookups (persona name, avatar), vanity URL → account ID, and the hero list.
2. Don't use Steam for: per-match details (`GetMatchDetails` broken), item constants, or
   anything replay-derived.
3. Budget: 100k calls/day per key. `GetMatchHistoryBySequenceNum` with
   `matches_requested=100` probably can't keep up with global match volume on one key and one
   IP (from the #17910 discussion). Size this when we design the collector.
4. Terms constraints that affect the design (store only what users request, privacy policy,
   no implied Valve endorsement) are in `data-rights.md`.

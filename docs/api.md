# Developer API (prototype, v1)

A small read-only REST API over the same analytics the MCP tools use, for developers who do not
use MCP. Each endpoint runs one MCP tool and returns that tool's envelope unchanged: `status`,
`result`, `sample_size`, `filters`, `period`, `definitions`, `confidence`, `caveats`, `source`
and `data_freshness`. Report the sample size, patch and caveats alongside any number you show.

- Base URL: `https://dota-insights.vercel.app/api/v1`
- OpenAPI 3.1 document: [`/api/v1/openapi.json`](https://dota-insights.vercel.app/api/v1/openapi.json)
  (generated from the routes and the tools' Pydantic models; no key needed)
- Endpoint list: `GET /api/v1` (no key needed)

## Endpoints

All are `GET`. Path segments in braces and the query parameters are the tool's arguments.

| Endpoint | Tool | Query parameters |
| --- | --- | --- |
| `/heroes/{hero}` | `get_hero_overview` | filters |
| `/heroes/{hero}/matchups` | `get_hero_matchups` | `mode`, `other_position`, `limit`, filters |
| `/heroes/{hero}/builds` | `get_hero_builds` | filters |
| `/tier-list` | `get_tier_list` | `limit`, filters |
| `/items/{item}` | `get_item_overview` | `top_heroes`, filters |
| `/matches/{match_id}` | `analyze_match` | none |
| `/players/{player}` | `get_player_profile` | none |

"Filters" means the shared match filters as plain query parameters: `patch` (`7.41` or `7.41f`),
`rank_bracket` (`divine`, `divine+`, `archon-legend`), `position` (1-5), `game_mode`
(`ranked_all_pick`, `turbo`, ...) and `min_sample`. Heroes and items take any English or Russian
spelling (`am`, `bkb`, `Пудж`). `{player}` takes an account id or SteamID64 (not a profile URL).

```bash
curl -H "Authorization: Bearer $DOTA_INSIGHTS_KEY" \
  "https://dota-insights.vercel.app/api/v1/tier-list?position=1&rank_bracket=divine%2B&limit=10"
```

## Responses

| HTTP | Body | When |
| --- | --- | --- |
| 200 | the tool's envelope | the tool ran. Check `status`: `ok` has a `result`; `insufficient_data` says in `hint` how to widen the filters; `error` says why in `message` and `hint` |
| 401 | `error` envelope | no `Authorization: Bearer` header, or the key is unknown or revoked |
| 404 | `error` envelope | no such endpoint |
| 422 | `error` envelope | an unknown query parameter, or a value the tool rejects (`match_id=abc`) |
| 429 | `error` envelope, `Retry-After` header | the key's per-minute or per-day quota is spent |
| 503 | `error` envelope | the API is not enabled on the server, or keys cannot be checked right now |

Error envelopes look like the tools' own:

```json
{"status": "error", "result": null, "message": "Missing API key.",
 "hint": "Send Authorization: Bearer <your API key>. ...", "caveats": [], "source": []}
```

## Keys and quotas

Keys look like `di_` followed by 40 characters and go in `Authorization: Bearer <key>`. Each
key has a per-minute and a per-day quota (defaults `DOTA_INSIGHTS_API_KEY_PER_MINUTE=30` and
`DOTA_INSIGHTS_API_KEY_PER_DAY=1000`, or set per key). Heavy tools (the tier list, item
overview and other multi-hero aggregates) also count against the heavy limits per key
(`DOTA_INSIGHTS_RATE_LIMIT_HEAVY_PER_MINUTE` / `_PER_DAY`). Every API call also shares the
server-wide OpenDota and STRATZ caps with MCP clients, so a call can still answer with a source
rate-limit `error` envelope when those are spent.

Key quotas are always enforced; `DOTA_INSIGHTS_RATE_LIMIT_ENABLED=false` turns off only the
per-IP limits for MCP clients.

## Operating it

1. Set `DOTA_INSIGHTS_API_KEY_SECRET` (a long random string, e.g. `openssl rand -hex 32`) in
   Vercel and in the environment where you run the CLI. Keys are stored as HMAC-SHA256 with this
   secret; without it the API answers 503. Changing it invalidates every key.
2. Apply migration `0005` (`uv run alembic upgrade head`; the collector workflow does this before
   its jobs). It adds the `api_keys` table and `usage_events.channel`.
3. Manage keys (needs `DOTA_INSIGHTS_DATABASE_URL` and the secret):

```bash
uv run dota-insights api-keys create "Alice's bot" --per-minute 60 --per-day 5000
```

```bash
uv run dota-insights api-keys list
```

```bash
uv run dota-insights api-keys revoke <id>
```

`create` prints the plain key once; only its hash and first 10 characters are stored. A key
looked up by the server is cached for up to a minute (Redis and the instance), so a revoked key
stops working within a minute.

API calls are recorded by the usage analytics like MCP calls, with `channel: "api"`; the usage
report (`dota-insights usage report`, `GET /admin/usage`) shows calls per channel. The key
itself is not stored with the events.

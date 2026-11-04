# Privacy policy

> **Template, not legal advice.** This policy describes how the code in this repository handles
> data. The owner of a deployment must review it, fill in every `[bracketed]` field and check it
> against the laws that apply to them before relying on it. The same text is served in English and
> Russian at [`/privacy`](https://dota-insights.vercel.app/privacy)
> (`src/dota_insights/mcp/site.py`); keep the two in sync.

Operator: [OPERATOR NAME AND COUNTRY — owner to fill in].
Contact: [CONTACT EMAIL — owner to fill in].
Last updated: [DATE — owner to fill in].

## Summary

- There are no accounts, sign-ups or cookies, and the pages use no tracking scripts.
- The server answers questions with **public** Dota 2 match and profile data from OpenDota and
  STRATZ.
- To enforce rate limits the server sees your IP address; it stores only a short hash of it, for
  at most two days.

## What is processed

### Public match and profile data

Tools fetch public data from [OpenDota](https://www.opendota.com) and [STRATZ](https://stratz.com):
matches, heroes, items, and, for players whose Dota 2 profile exposes public match data, account
IDs, persona names, ranks and match histories. Results are cached for minutes to days. A collector
also stores public ranked matches in our database (match and per-player statistics with account
IDs) to build aggregate statistics. Anonymous players (who hide their profile) are not identified.

### Your questions and requests

Your AI client sends tool calls (for example a hero name or an account ID) to the server. The
server logs each call: tool name, duration, status, data sources, cache use and whether a rate
limit applied. It does not log your IP address in these lines. What you type to your AI assistant
stays between you and that assistant's provider; the server receives only the tool calls.

### IP addresses and rate limiting

Each client is limited to a number of tool calls per minute and per day. The server reads the
caller's IP address from the request, keeps only the first 16 hex characters of its SHA-256 hash
as the counter key, and lets each counter expire after twice its window (at most two days). The
hosting provider (Vercel) records standard request logs, including IP addresses, and keeps them
under its own retention policy. [Owner: state the retention of your Vercel log plan.]

## Third parties

- **Vercel** (hosting, request logs) — [privacy policy](https://vercel.com/legal/privacy-policy).
- **Neon** (PostgreSQL database with collected public match data) —
  [privacy policy](https://neon.com/privacy-policy).
- **Upstash** (Redis: cache, rate-limit counters with hashed IPs) —
  [privacy policy](https://upstash.com/trust/privacy.pdf).
- **OpenDota** and **STRATZ** (sources of public Dota 2 data; the server calls their APIs, not your
  browser) — [opendota.com](https://www.opendota.com), [stratz.com](https://stratz.com).

[Owner: list the regions where your Vercel, Neon and Upstash resources run.]

## Removing a player's data

To stop new matches from being public, turn off "Expose Public Match Data" in Dota 2 settings.
OpenDota and STRATZ keep their own copies; ask them directly to remove data there. To have your
account's data removed from this server's database and cache, email [CONTACT EMAIL — owner to fill
in] with your Steam account ID (32-bit Dota ID). We will delete the stored per-player rows for that
account and exclude it from future collection within [N] days. [Owner: set the time frame and
describe how you verify the request.]

## Children

The service is not directed at children and collects no data that identifies a person beyond
public game profiles.

## Changes

Changes to this policy are published on this page and in the project's repository.

## For the owner: what to check before publishing

- Fill in operator, contact, date, retention of Vercel logs, hosting regions and the removal time
  frame (search for `[` in this file and in `PRIVACY` in `src/dota_insights/mcp/site.py`).
- The removal promise needs a working process: there is no deletion command yet, so deleting an
  account's rows from `match_players` (and related tables) and excluding it from collection is
  manual until one exists.
- If request analytics or feedback collection are added, describe what they store and for how
  long, and update both copies of this policy.
- Valve's Steam Web API terms and EU/UK data protection law may require more (a legal basis,
  data-subject rights, a named storage country); see `docs/research/data-rights.md`.

---
name: dota-insights
description: Use when the user asks about Dota 2 statistics or wants advice backed by data. Examples include hero win rates, tier lists and the current meta; item builds, item timings and whether an item is worth it; counters, synergies and drafts ("what should I pick against..."); analysing a match by its ID (laning, teamfights, vision, turning points); a player's profile, hero pool, trends or weaknesses by account ID; pro teams, tournaments and live games; and patch changes. Answers come from the free public Dota Insights MCP server, with sample sizes, patch and caveats.
---

# Dota Insights

Answer Dota 2 questions with numbers from the public **Dota Insights** MCP server instead of
from memory. The server aggregates OpenDota, STRATZ and its own collector of ranked public
matches. Every tool returns an envelope with the sample size, patch, filters and caveats.

## 1. Make sure the server is connected

The server is free, needs no account or key, and speaks MCP over Streamable HTTP:

```
https://dota-insights.vercel.app/mcp
```

If no `dota-insights` tools are available, tell the user how to add the server for their
agent (see `references/connect.md`, e.g. `claude mcp add --transport http dota-insights
https://dota-insights.vercel.app/mcp` for Claude Code) and stop. Do not answer statistical
questions from memory as if they were current data.

## 2. Pick the tool

Map the question to one tool first; call more only when the question needs it.
`references/tools.md` lists every tool with its purpose and required arguments. The most
common ones:

| The user asks about | Tool |
| --- | --- |
| How strong a hero is now | `get_hero_overview` |
| The best heroes / tier list | `get_tier_list` (filter by position or rank) |
| What to build on a hero | `get_hero_builds`, `get_ability_stats` |
| When an item is worth buying | `analyze_item`, `compare_items`, `get_item_payback` |
| Counters and synergies | `get_hero_matchups`, `find_hero_combos` |
| What to pick in a draft | `suggest_pick`, then `analyze_draft` for the full draft |
| A specific match (by match ID) | `analyze_match`, then `find_turning_points`, `analyze_laning_phase`, `analyze_teamfights`, `analyze_vision` |
| A player (by 32-bit account ID) | `get_player_profile`, `get_player_hero_pool`, `find_player_weaknesses`, `get_player_trends` |
| Pro scene | `get_pro_matches`, `get_team_stats`, `get_tournament_stats`, `get_live_matches` |
| What a patch changed | `get_patch_summary`, `find_meta_trends`, `get_hero_patch_history` |
| What an ability or item does | `lookup_reference` |
| A custom question nothing else covers | `explore_query`, `test_hypothesis`, `analyze_correlation` |

Arguments:

- Heroes and items can be named in English or Russian, including common nicknames
  ("PA", "BKB", "Пудж", "БКБ").
- Use `filters` only when the user restricts the question: `patch`, `rank_bracket`
  (`"herald"`, `"archon-legend"`, `"divine+"`), `position` (1–5), `game_mode`, `min_sample`.
- Leave `patch` empty for the current patch.
- Player tools need the 32-bit account ID from the player's OpenDota or STRATZ profile URL,
  and a public profile ("Expose Public Match Data" enabled in the game).

## 3. Read the envelope before answering

Each result has a `status`:

- `ok`: use the numbers. Always report `sample_size` (matches or games), the `patch` and the
  rank or position filters that applied.
- `insufficient_data`: there were too few matches for a reliable answer. Say so, give the
  sample that was found, and offer the widening the `hint` suggests (older patch, wider rank
  range). Do not present these numbers as facts.
- `error`: a source failed or a rate limit was hit. Relay the `hint` (for example "retry in
  60 seconds") instead of guessing numbers. Do not call the same tool in a loop.

Always pass on the `caveats` that change the meaning of a number. `references/reading-results.md`
explains the common ones, such as selection bias in item timings, the STRATZ time window and
win-rate intervals.

## 4. Answer format

1. Start with a direct answer to the question in one or two sentences.
2. Then give the key numbers as a short list or table: win rate with its 95% interval (`confidence`)
   when the tool gives one, pick rate, and the sample size.
3. End with one line naming the data: source, patch, rank filter and number of matches.
4. Mention a caveat only when it matters for the decision the user is making.

Answer in the user's language. Hero and item names can stay in English if the user writes
them that way.

## 5. Limits and feedback

- The server is free and rate-limited per client: about 30 tool calls a minute and 1,000 a
  day, with fewer for heavy tools such as `explore_query` and all-hero item queries. Prefer
  one well-filtered call over many.
- MMR, hidden matchmaking data and private profiles are not available. Say so instead of
  estimating.
- If the user says an answer is wrong (e.g. it contradicts the game or another trusted site),
  call `report_wrong_answer`. If no tool can answer a Dota question, call `suggest_feature`
  with the question. Both send only the text you pass.

See `examples/questions.md` for sample questions and the calls that answer them.

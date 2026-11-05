# Reading Dota Insights results

Every tool returns the same envelope:

| Field | Meaning |
| --- | --- |
| `status` | `ok`, `insufficient_data` or `error` |
| `result` | The data (null on error) |
| `sample_size` | Matches or games behind the numbers |
| `filters` | Patch, rank bracket, position and game mode that applied |
| `period` / `data_freshness` | Time window of the data, and when it was fetched |
| `confidence` | Metric name → `[low, high]` 95% interval |
| `definitions` | How each metric is computed; use it when explaining a number |
| `source` | `opendota`, `stratz`, `steam` and/or `dota-insights-db` (the server's own ranked-match database) |
| `caveats` | Limits that change how to read the numbers |
| `hint` | What to do next (widen filters, retry after N seconds) |
| `names_ru` | Russian names of the heroes and items in the result |

## Rules for presenting numbers

- **Report the sample.** "52.1% over 48,210 games" says much more than "52.1%". With fewer
  than a few hundred games, treat a difference of 1–2 points as noise.
- **Use the interval.** When `confidence` holds a 95% interval for a metric and two heroes'
  intervals overlap, do not call one stronger.
- **Name the patch and rank.** Numbers from different patches or rank brackets are not
  comparable, so say which ones apply.
- **Draft-level vs detailed data.** Some tools use only matches that were parsed in detail
  (items, lanes, timings). The `sample_size` there is smaller than the hero's total games.

## Common caveats

- **Selection bias in item timings.** A late item often shows a low win rate because the
  game was already going badly, not because the item is bad. Do not answer "is X worth it"
  from purchase-time win rates alone. Compare within the same timing bucket (`compare_items`)
  or use `get_item_payback`.
- **Win rate by duration.** Long games are mostly comebacks or even games, so a hero that
  wins more late is not necessarily a "late-game hero" in the usual sense.
- **Matchup advantage, not raw win rate.** `get_hero_matchups` corrects for each hero's
  baseline, so use the advantage column for counters.
- **STRATZ time window.** STRATZ aggregates cover a recent window (days to weeks), not the
  whole patch.
- **Pro data is small.** Tournament and team stats often cover a few dozen matches.
- **Players.** Recent-match averages only cover public matches. Medal is not MMR.

## Errors

| `status: error` with... | Do |
| --- | --- |
| a rate-limit hint ("retry in N seconds") | Wait, or tell the user when to ask again |
| "source unavailable" / "overloaded" | Say the data source is temporarily down. Do not invent numbers. |
| an unknown hero or item | Use the suggestions in the message (the name resolver lists close matches) |
| a private player profile | Ask the user to enable "Expose Public Match Data" in Dota 2 |

# Insights: Weekly Meta Report and post drafts

Tracker tasks p5-04 (Weekly Meta Report with charts) and p5-01 (Insight Generator). Code in
`src/dota_insights/insights/`, tests in `tests/test_insights.py`, workflow
`.github/workflows/insights.yml`.

The generator turns the collector's database into drafts of Telegram posts in Russian. **Nothing
is published**: a moderator reads the drafts, edits them and posts by hand (automatic posting is
p4-11).

## Running it

```bash
uv run dota-insights insights weekly --out reports [--end YYYY-MM-DD] [--source matches|opendota]
    [--min-games 200] [--top 5] [--alpha 0.05] [--min-wr-delta 0.015] [--min-pr-delta 0.01]
    [--max-anomalies 10] [--no-charts]
```

`python -m dota_insights.insights weekly ...` is the same. The reported week is the seven days
before `--end` (default: today, UTC); it is compared with the seven days before that. The
`Insights` workflow runs it every Monday at 06:23 UTC (after the daily collector run), so the week
is Monday to Sunday, and uploads `reports/` as the `insights-<run id>` artifact (kept 30 days).
`workflow_dispatch` takes `end`, `source` and `min_games`. It only reads the database
(`DOTA_INSIGHTS_DATABASE_URL`) and skips with a warning when the secret is not set.

## Data

- `matches` (default): the collector's ranked matches (`matches` + `match_players`) that started in
  the week, bracketed by `avg_rank_tier`. Patch names come from `matches.patch_id`; if the two
  weeks span more than one patch, every draft says so.
- `opendota`: the `hero_stats_daily` snapshot of OpenDota `/heroStats` taken on the last day of each
  week. OpenDota counts its own trailing window, which is not exactly seven days, and matches per
  bracket are estimated as picks / 10. Useful as a cross-check while the collector's volume is low.

Immortal is folded into Divine ("Божество и Титан"), as everywhere else in the tools; matches of
unknown rank are left out. "Все ранги" sums brackets 1-7.

## Method

- Win rate = wins / games; pick rate = games / matches of the bracket. A hero x bracket is compared
  only with at least `--min-games` games in **each** week; everything below is left out.
- Risers and fallers: the `--top` largest increases and decreases per bracket, for both rates.
- Anomalies: for every eligible hero x bracket, a pooled two-proportion z-test of this week against
  the previous one, for win rate and for pick rate (`analytics.stats.two_proportion_ztest`). All
  tests of the report go through one Benjamini-Hochberg correction; an anomaly needs q ≤ `--alpha`
  **and** a change of at least `--min-wr-delta` (win rate) or `--min-pr-delta` (pick rate), so a
  huge sample does not turn a 0.3-point wobble into a post. One draft per hero and metric (its
  strongest bracket), strongest first, at most `--max-anomalies`.

## Output

`<out>/<last day of the week>/`:

| File | What |
|---|---|
| `README.md` | Index for the moderator: period, sample, source, thresholds, files, checklist |
| `weekly-meta.md` | Weekly Meta Report post, all ranks, top 3 per section (fits a photo caption) |
| `weekly-meta-by-rank.md` | Per-bracket tables behind the post, with z-scores (reference) |
| `weekly-winrate.png`, `weekly-pickrate.png` | Bar charts of the changes (`analytics.charts`) |
| `insights/NN-<hero>-<metric>-<rank>.md` | One short Insight Generator draft per anomaly |
| `report.json` | Every eligible change and anomaly, to check the numbers |

Texts (`insights/texts.py`) are short and factual and always state the sample (matches, games
before → after) and the patch. Numbers follow Russian conventions: decimal comma, non-breaking
thousands separator, "п.п." for rate changes, plural forms ("1 игра", "3 игры", "5 игр"). Hero
names are the English display names for now; switch `texts.hero_name` to the RU/EN resolver once
it lands. A chart that fails to render is listed in `README.md`; the texts are still written.

A sample folder generated from the synthetic test fixtures (not real data: Anti-Mage is made
stronger and Axe more popular on purpose) is in [`docs/samples/insights/`](../samples/insights/).

# Trends fixtures

Recorded live on 2026-11-04 with the queries in `dota_insights.analytics.trends`.

- `stratz_series_ranked.json`: `SERIES_QUERY` with `modes: [ALL_PICK_RANKED]`, no bracket or
  position. `stratz_series_immortal_pos1.json`: the same with `brackets: [IMMORTAL]`,
  `positions: [POSITION_1]`. Both trimmed to `winMonth` rows from 2025-01 onward (the live
  answer goes back to 2020-10); `winWeek` is complete (19 weeks, 2026-05-28 to 2026-10-01).
- `stratz_game_versions.json`: `GAME_VERSIONS_QUERY`, untrimmed.
- `stratz_patch_notes_181.json` (7.40) and `stratz_patch_notes_184.json` (7.41, empty on
  STRATZ): `PATCH_NOTES_QUERY`, untrimmed.
- `opendota_hero_stats.json`: `/heroStats`, trimmed to `id`, `localized_name`, `pub_pick`,
  `pub_win`, `pub_pick_trend`, `pub_win_trend`.

"""Insight Generator (p5-01) and the Weekly Meta Report rubric (p5-04).

Reads the collector's tables and writes drafts of Telegram posts in Russian for manual review;
nothing is published (Telegram posting is p4-11). ``dota-insights insights weekly`` writes one
dated folder of Markdown, PNG charts and the underlying numbers as JSON:

- ``data``: hero picks and wins per rank bracket for two consecutive weeks, from the collector's
  own matches or from OpenDota ``/heroStats`` snapshots (``hero_stats_daily``).
- ``meta``: week-over-week win-rate and pick-rate changes, risers and fallers per bracket, and
  anomalies (two-proportion z-tests with a Benjamini-Hochberg correction).
- ``texts``: the Russian templates. ``report``: writes the folder.
"""

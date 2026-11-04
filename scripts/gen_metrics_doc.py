"""Regenerate docs/architecture/metrics.md: every tool's metrics and the source behind each (p1-05).

``MATRIX`` below is the hand-kept part: for each registered tool, what it measures, where the
numbers come from (in the order the tool tries them) and how well that source covers the
question. The generated page adds each tool's typed result fields from its output schema, so a
renamed or new field shows up as a diff. Run after adding or changing a tool::

    uv run python scripts/gen_metrics_doc.py          # rewrite docs/architecture/metrics.md
    uv run python scripts/gen_metrics_doc.py --check  # exit 1 if the page is out of date

``tests/test_metrics_doc.py`` runs the check and fails when a registered tool has no entry here
or an entry names a tool that no longer exists.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "architecture" / "metrics.md"

Coverage = Literal["good", "partial", "weak", "none"]

# Source shorthands used in the table.
DB = "dota-insights DB (collector aggregates)"
EXPLORER_LEAGUE = "OpenDota `/explorer` (parsed league matches)"
EXPLORER_PUBS = "OpenDota `/explorer` (`public_matches`)"
STRATZ_HERO = "STRATZ `heroStats`"


@dataclass(frozen=True)
class Entry:
    metrics: str
    sources: tuple[str, ...]  # tried in this order
    coverage: Coverage
    note: str = ""


MATRIX: dict[str, Entry] = {
    # --- heroes
    "get_hero_overview": Entry(
        "win rate, pick rate (Wilson CIs), win-rate rank, positions, win rate per medal, "
        "3-patch trend, pro pick/ban",
        (DB, f"{STRATZ_HERO} `winDay`/`winWeek`", "OpenDota `/heroStats`"),
        "good",
        "DB has no Immortal split (OpenDota caps lobby rank at Divine); per-medal Immortal "
        "comes from STRATZ only. Pro rates from OpenDota `/heroStats` (or the DB daily copy).",
    ),
    "compare_heroes": Entry(
        "win/pick rate, KDA, XPM, net worth, last hits per hero; pairwise z-tests",
        (DB, f"{STRATZ_HERO} `stats`", "OpenDota `/heroStats`"),
        "good",
        "Averages need enriched (STRATZ-detail) matches in the DB; STRATZ `stats` only takes "
        "paired brackets and returns null `goldPerMinute`.",
    ),
    "get_tier_list": Entry(
        "sample-adjusted (shrunk) win rate, tier S-D",
        (DB, STRATZ_HERO, "OpenDota `/heroStats`"),
        "good",
        "Tier cut-offs are our own definition, not a source metric.",
    ),
    "find_hidden_gems": Entry(
        "pick rate, shrunk win rate, significance",
        (DB, STRATZ_HERO, "OpenDota `/heroStats`"),
        "good",
    ),
    # --- matchups
    "get_hero_matchups": Entry(
        "pair win rate vs/with every hero, log5 advantage, STRATZ synergy",
        (DB, "STRATZ `heroStats.matchUp`", "OpenDota `/heroes/{id}/matchups` + `/heroStats`"),
        "partial",
        "No source splits matchups by position (STRATZ `matchUp` has no `positionIds`). "
        "OpenDota fallback is vs-only with small samples.",
    ),
    "find_hero_combos": Entry(
        "duo/trio synergy advantage",
        (DB, "STRATZ `heroStats.matchUp`"),
        "partial",
        "Bracket only, no position; trios are built from pair scores, not observed trios.",
    ),
    "suggest_pick": Entry(
        "sum of matchup + synergy advantages for each candidate",
        (DB, "STRATZ `heroStats.matchUp`", "STRATZ `heroStats.stats` (positions)"),
        "partial",
        "Heuristic score; no observed win rate of the full draft.",
    ),
    "analyze_draft": Entry(
        "pairwise contributions, heuristic Radiant win estimate, draft weaknesses",
        (DB, "STRATZ `heroStats.matchUp`", "reference roles (local)"),
        "weak",
        "The win estimate is a logistic of summed pair advantages, not a fitted draft model.",
    ),
    # --- items and builds
    "get_item_overview": Entry(
        "purchase rate, median purchase minute, win rate by timing quartile, top buyers",
        (DB, f"{STRATZ_HERO} `itemFullPurchase`"),
        "partial",
        "STRATZ purchase counts are not unique matches and use a different window from "
        "`heroStats.stats`; DB timings only from STRATZ-enriched matches.",
    ),
    "analyze_item": Entry(
        "win rate by purchase-minute bucket (Wilson), buyer vs non-buyer lift by hero",
        (DB, f"{STRATZ_HERO} `itemFullPurchase`", "OpenDota `/scenarios/itemTimings`"),
        "partial",
        "Correlational: timing tracks game state.",
    ),
    "compare_items": Entry(
        "bucket-matched win rates, timing-standardised total, z-tests with BH correction",
        (DB, f"{STRATZ_HERO} `itemFullPurchase`"),
        "partial",
    ),
    "get_item_payback": Entry(
        "extra GPM after purchase, payback minutes",
        (EXPLORER_LEAGUE,),
        "weak",
        "League (pro) matches only, per-minute gold from `gold_t`; no pub source has bulk "
        "per-minute gold.",
    ),
    "get_hero_builds": Entry(
        "starting items, boots, core/situational items (popularity, median minute, win rate), "
        "talents, skill order, facet",
        (
            f"{STRATZ_HERO} item/talent aggregates + `guide`",
            "OpenDota `/heroes/{id}/itemPopularity`",
        ),
        "partial",
        "No full skill-order aggregate in STRATZ (built from guide matches); facet choice has "
        "no aggregate source. OpenDota fallback has popularity only, no win rates.",
    ),
    "get_ability_stats": Entry(
        "level each ability is maxed with win rate, skill orders, talent shares/win rates, "
        "Aghanim's purchase rate and timing",
        (f"{STRATZ_HERO} `abilityMaxLevel`/`talent`/`itemFullPurchase`",),
        "partial",
        "Ultimate rows can be missing from `abilityMaxLevel`.",
    ),
    "get_neutral_items": Entry(
        "neutral item usage by tier, win rate, best per hero",
        ("STRATZ `heroStats.itemNeutral`", EXPLORER_LEAGUE),
        "weak",
        "STRATZ `itemNeutral` returned no rows when built, so in practice this is league "
        "matches only.",
    ),
    "get_pro_builds": Entry(
        "pro item shares and timings vs public, first ten skill points, max order",
        (
            "STRATZ `match` (pro league games)",
            "STRATZ `heroStats` (public)",
            "OpenDota `/heroes/{id}/matches`",
        ),
        "partial",
        "Public items compared by popularity rank, not share (windows differ).",
    ),
    # --- farming
    "get_hero_benchmarks": Entry(
        "percentiles p10-p99 of GPM, XPM, LH/min, damage/min, ...; placement of given values",
        (f"{DB} (GPM/XPM histograms)", "OpenDota `/benchmarks`"),
        "partial",
        "DB covers GPM/XPM only. OpenDota `/benchmarks` has no sample size, bracket or role.",
    ),
    "get_hero_power_curve": Entry(
        "win rate by 5-minute duration bucket, peak, early/late classification",
        (f"{STRATZ_HERO} `winWeek` by duration minute", "OpenDota `/heroes/{id}/durations`"),
        "good",
    ),
    "get_farming_stats": Entry(
        "gold, GPM, XP, XPM, last hits at given minutes (median, quartiles)",
        (EXPLORER_LEAGUE,),
        "weak",
        "League matches only (per-minute arrays `gold_t`/`xp_t`/`lh_t`); no pub source.",
    ),
    "get_gold_xp_curves": Entry(
        "median gold and XP per minute, wins vs losses",
        (EXPLORER_LEAGUE,),
        "weak",
        "League matches only.",
    ),
    "get_hero_lane_stats": Entry(
        "lane win/draw/loss and stomp rates, net worth and LH at 10, best/worst lane pairings",
        (f"{STRATZ_HERO} `laneOutcome`, `stats` at 10 min", EXPLORER_LEAGUE),
        "good",
        "The league fallback derives lane outcomes from 10-minute net worth.",
    ),
    # --- economy
    "analyze_death": Entry(
        "gold lost and dead time per death by minute, lead swing after a death, buyback win rate",
        (EXPLORER_LEAGUE,),
        "weak",
        "League matches only (`gold_reasons`, `life_state`, `kills_log`, `buyback_log`).",
    ),
    "get_farm_distribution": Entry(
        "share of team gold by position, best carry/core share",
        (EXPLORER_LEAGUE,),
        "weak",
        "League only; positions inferred from lane and farm.",
    ),
    "get_comeback_stats": Entry(
        "win rate from a deficit at a minute, deficit grid, comeback factors, comeback heroes",
        (EXPLORER_LEAGUE, f"{STRATZ_HERO} `stats` (`comeBackWon`/`comeBackLost`)"),
        "partial",
        "Gold timelines only for league matches; the pub hero list is STRATZ's undocumented "
        "2-decimal shares.",
    ),
    "get_objective_impact": Entry(
        "taker win rate for first blood, tower, Roshan, Tormentor, barracks, rune leads; "
        "lead at event; even-game win rate",
        (EXPLORER_LEAGUE,),
        "weak",
        "League only. `when_even` is uninformative for first blood (lead is ~0 at the first "
        "kill). No Roshan-timing split.",
    ),
    # --- analysis
    "test_hypothesis": Entry(
        "two-group win rate or metric difference (chi-square/Fisher/Mann-Whitney), effect CI",
        (EXPLORER_LEAGUE,),
        "weak",
        "League only and the newest 500 rows per hero, so many heroes fall below min_sample "
        "on a new patch; no rank or position filter.",
    ),
    "analyze_correlation": Entry(
        "Mantel-Haenszel odds ratio of winning, high vs low metric, duration strata",
        (EXPLORER_LEAGUE,),
        "weak",
        "League only; roles pooled unless a hero is given.",
    ),
    "find_matches": Entry(
        "match search by heroes, outcome, rank, duration, comeback size",
        (EXPLORER_LEAGUE, EXPLORER_PUBS),
        "partial",
        "Comeback (net-worth swing) filters need gold timelines, i.e. parsed league matches.",
    ),
    "explore_query": Entry(
        "ad-hoc aggregates over whitelisted explorer tables",
        ("OpenDota `/explorer`",),
        "partial",
    ),
    # --- general
    "get_general_stats": Entry(
        "Radiant/Dire win rate (Wilson), duration distribution, region and mode shares",
        (EXPLORER_PUBS, "STRATZ constants (regions)"),
        "partial",
        "Latest ~11 hours of public matches only, so it leans to the regions in prime time.",
    ),
    "get_records": Entry(
        "top-100 single-match records",
        ("OpenDota `/records/{metric}`",),
        "good",
    ),
    "get_rank_distribution": Entry(
        "share of players per medal and star, percentile at each medal, leaderboard sizes",
        ("OpenDota `/distributions`", "STRATZ `leaderboard`"),
        "good",
    ),
    # --- trends
    "find_meta_trends": Entry(
        "win/pick rate change between patches or periods, z-tests with BH correction",
        (f"{STRATZ_HERO} `winWeek`/`winMonth`", "OpenDota `/heroStats`"),
        "good",
    ),
    "get_hero_patch_history": Entry(
        "win and pick rate per patch with intervals, biggest jumps",
        (f"{STRATZ_HERO} `winWeek`/`winMonth` by patch window",),
        "good",
    ),
    "get_patch_summary": Entry(
        "patch dates, biggest win/pick rate movers, patch notes",
        (f"{STRATZ_HERO} `winWeek`/`winMonth`", "STRATZ `constants.gameVersions`, patch notes"),
        "partial",
        "STRATZ has patch notes only for some patches.",
    ),
    # --- matches
    "analyze_match": Entry(
        "result, draft, KDA, GPM, XPM, net worth, damage, contribution score, largest leads",
        ("OpenDota `/matches/{id}`",),
        "good",
        "Gold swings need a parsed match (`/request/{id}` is sent otherwise).",
    ),
    "get_match_timeline": Entry(
        "per-minute gold/XP advantage, events, purchases",
        ("OpenDota `/matches/{id}`",),
        "good",
        "Parsed matches only.",
    ),
    "find_turning_points": Entry(
        "largest gold swings and the fights/objectives around them",
        ("OpenDota `/matches/{id}`",),
        "good",
        "Parsed matches only.",
    ),
    "analyze_teamfights": Entry(
        "teamfights: participants, deaths, gold/XP swing, winner, per-player damage",
        ("OpenDota `/matches/{id}`",),
        "good",
        "Parsed matches only.",
    ),
    "analyze_vision": Entry(
        "wards placed/destroyed, lifetime, dewards per player",
        ("OpenDota `/matches/{id}`",),
        "good",
        "Parsed matches only.",
    ),
    "analyze_laning_phase": Entry(
        "LH, denies, gold and XP at 10:00, lane winners, early kills",
        ("OpenDota `/matches/{id}`",),
        "good",
        "Parsed matches only.",
    ),
    # --- players
    "get_player_profile": Entry(
        "medal, leaderboard rank, total matches, win rate, last 20 matches",
        ("OpenDota `/players/{id}`, `/wl`, `/recentMatches`",),
        "good",
        "Private profiles return nothing.",
    ),
    "get_player_stats": Entry(
        "average KDA, GPM, XPM, LH, damage, win rate (95% CIs)",
        ("OpenDota `/players/{id}/matches`",),
        "good",
    ),
    "get_player_hero_pool": Entry(
        "per-hero matches and win rate vs the hero's win rate and benchmarks",
        ("OpenDota `/players/{id}/heroes`, `/benchmarks`", "OpenDota `/heroStats`"),
        "partial",
        "Benchmarks are all-bracket percentiles, not the player's medal.",
    ),
    "find_player_weaknesses": Entry(
        "per-minute stats vs medal percentiles, phase where losses fall behind, loss traits",
        ("OpenDota `/players/{id}/matches`", "OpenDota `/benchmarks`", "STRATZ player matches"),
        "partial",
    ),
    "get_player_trends": Entry(
        "rolling win rate, KDA, GPM, XPM, LH, damage; streaks",
        ("OpenDota `/players/{id}/matches`",),
        "good",
    ),
    "get_player_peers": Entry(
        "frequent teammates/opponents with win rates",
        ("OpenDota `/players/{id}/peers`",),
        "good",
    ),
    "compare_players": Entry(
        "two players' win rate, averages, shared heroes and positions",
        ("OpenDota `/players/{id}/matches`", "OpenDota `/players/{id}/heroes`"),
        "good",
    ),
    # --- pro
    "get_pro_matches": Entry(
        "recent league matches with drafts",
        ("OpenDota `/proMatches`", "OpenDota `/leagues`"),
        "good",
    ),
    "get_team_stats": Entry(
        "team win rate, duration, hero pool, first-pick and ban tendencies",
        ("OpenDota `/teams`", "STRATZ `team.matches`"),
        "good",
    ),
    "get_head_to_head": Entry(
        "league record between two teams, by league, drafts",
        (EXPLORER_LEAGUE,),
        "good",
    ),
    "get_tournament_stats": Entry(
        "hero picks, bans, contest and win rates in an event vs public; match records",
        (EXPLORER_LEAGUE, "OpenDota `/leagues`", "OpenDota `/heroStats` (pub, last 7 days)"),
        "good",
    ),
    "get_pro_player": Entry(
        "team, signature heroes, results by position",
        ("OpenDota `/proPlayers`", "STRATZ `player.matches`"),
        "good",
    ),
    "get_live_matches": Entry(
        "live top games: teams, players, time, score, net-worth lead",
        ("OpenDota `/live`",),
        "good",
    ),
    "predict_live_win": Entry(
        "win probability from net-worth lead, time and draft; calibration",
        ("OpenDota `/live`", "local logistic model (trained on league gold timelines)"),
        "partial",
        "Model trained on league matches; draft adds almost nothing beyond net worth.",
    ),
    # --- reference, charts, meta
    "lookup_reference": Entry(
        "hero/item/ability attributes, costs, components, abilities and talents",
        ("bundled reference data (from OpenDota `/constants`)",),
        "good",
        "Refreshed by `scripts/update_reference.py`, not live.",
    ),
    "render_chart": Entry(
        "chart of another tool's result (Vega-Lite + PNG)", ("none (input data)",), "good"
    ),  # fmt: skip
    "server_info": Entry("server version, configured sources", ("server configuration",), "good"),
    "report_wrong_answer": Entry(
        "client feedback on a tool's answer (stored, not a metric)",
        ("usage_feedback table",),
        "good",
    ),
    "suggest_feature": Entry(
        "questions no tool answers (stored for review, not a metric)",
        ("usage_feedback table",),
        "good",
    ),
}

# Metrics players ask for (docs/research/user-questions.md) that no source covers well.
GAPS: tuple[tuple[str, str], ...] = (
    (
        "Matchups and synergies by position",
        "STRATZ `matchUp` has no position argument; the DB stores pairs per bracket only.",
    ),
    (
        "Per-minute farm, deaths, objectives and item timings in ranked pubs",
        "Bulk per-minute arrays, kill logs and objectives exist only for parsed league matches "
        "in the OpenDota explorer. Every economy and analysis tool is league-only; the collector's "
        "STRATZ-enriched pubs could fill this.",
    ),
    (
        "Immortal-only stats from our database",
        "OpenDota public matches cap the lobby rank at Divine; Immortal needs STRATZ.",
    ),
    (
        "Roshan/objective timing effects (e.g. Roshan before 20 minutes)",
        "get_objective_impact reports the first event at any time; no tool splits by event minute.",
    ),
    (
        "Early-game rune control",
        "Rune counts are whole-match only (OpenDota `runes`), so a winning team's late runes "
        "dominate.",
    ),
    (
        "Facet choice and full skill orders as aggregates",
        "No STRATZ aggregate; builds are inferred from guide matches.",
    ),
    (
        "Neutral items in pubs",
        "STRATZ `itemNeutral` returned no rows; the tool falls back to league matches.",
    ),
    (
        "Party vs solo, role queue, behaviour score, hidden MMR",
        "No public source exposes them (MYT-03, MYT-05, MYT-06, MYT-14).",
    ),
)


async def _result_fields() -> dict[str, list[str]]:
    from dota_insights.mcp.server import mcp

    out = {}
    for tool in await mcp.list_tools():
        schema = tool.output_schema or {}
        defs = schema.get("$defs", {})
        node = schema.get("properties", {}).get("result", {})
        for option in node.get("anyOf", [node]):
            if "$ref" in option:
                node = defs[option["$ref"].rsplit("/", 1)[-1]]
                break
        out[tool.name] = list(node.get("properties", {}))
    return out


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render(fields: dict[str, list[str]]) -> str:
    counts = {c: sum(e.coverage == c for e in MATRIX.values()) for c in ("good", "partial", "weak")}
    lines = [
        "# Metrics and their sources",
        "",
        "<!-- generated by scripts/gen_metrics_doc.py; edit MATRIX there, not this file -->",
        "",
        f"Every registered tool ({len(MATRIX)}), what it measures and where each number comes "
        "from, in the order the tool tries the sources. *Result fields* are read from the "
        "tool's output schema (tools that return free-form dicts show —). Coverage: "
        f"**good** {counts['good']}, **partial** {counts['partial']}, **weak** {counts['weak']}.",
        "",
        "- **good**: the source answers the question for the population players mean "
        "(ranked pubs by medal and position, or the exact match/player).",
        "- **partial**: answered, but with a limit that changes some answers (no position split, "
        "different windows, a fallback without win rates).",
        "- **weak**: only a proxy population (parsed league matches) or a heuristic.",
        "",
        "Cross-source agreement for hero win and pick rates is measured in "
        "[source-comparison.md](../research/source-comparison.md).",
        "",
        "| Tool | Metrics | Sources (in order) | Coverage | Notes | Result fields |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(MATRIX):
        e = MATRIX[name]
        result = ", ".join(f"`{f}`" for f in fields.get(name, [])) or "—"
        lines.append(
            f"| `{name}` | {_cell(e.metrics)} | {_cell('; '.join(e.sources))} | {e.coverage} | "
            f"{_cell(e.note) or '—'} | {result} |"
        )
    lines += ["", "## Gaps: metrics with no source or weak coverage", ""]
    lines += [f"- **{what}.** {why}" for what, why in GAPS]
    weak = sorted(n for n, e in MATRIX.items() if e.coverage == "weak")
    lines += ["", "Tools rated weak: " + ", ".join(f"`{n}`" for n in weak) + "."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    expected = render(asyncio.run(_result_fields()))
    if "--check" in argv:
        if not DOC.exists() or DOC.read_text(encoding="utf-8") != expected:
            print("docs/architecture/metrics.md is out of date; run scripts/gen_metrics_doc.py")
            return 1
        return 0
    DOC.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

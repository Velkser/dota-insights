"""Per-tool cost table for docs/research/pro-and-costs.md (p5-05).

Source request counts are cold-cache counts at default arguments: measured with countplug.py on
the test fixtures, then scaled to default fan-out from the code. Run from the repo root:
``uv run python docs/research/samples/pro-costs/costs.py``. Prices as of 2026-11-04.
"""

import statistics

from dota_insights.mcp.ratelimit import HEAVY_TOOLS

# tool: (domain, od_cold, st_cold, db_first, ttl, lat_cold, lat_warm, note)
T = {
    "analyze_correlation": (
        "analysis",
        1,
        0,
        0,
        "6h",
        "C",
        "A",
        "1 explorer SQL (≤500 player rows / 3000 hero rows)",
    ),
    "explore_query": ("analysis", 1, 0, 0, "6h", "C", "A", "1 explorer SQL, limit ≤500"),
    "find_matches": ("analysis", 2, 0, 0, "6h", "C", "A", "2 explorer SQL (pub window + rows)"),
    "test_hypothesis": ("analysis", 1, 0, 0, "6h", "C", "A", "1 explorer SQL"),
    "get_ability_stats": (
        "builds",
        0,
        6,
        0,
        "6h",
        "B",
        "A",
        "Builds + 4 aliased batches + guides (≤60 guides / 20 per query)",
    ),
    "get_hero_builds": (
        "builds",
        0,
        6,
        0,
        "6h",
        "B",
        "A",
        "same queries as get_ability_stats, shared cache",
    ),
    "get_neutral_items": (
        "builds",
        2,
        1,
        0,
        "6h",
        "C",
        "A",
        "STRATZ Neutral; OpenDota item constants (24h) + 1 explorer fallback",
    ),
    "render_chart": ("charts", 0, 0, 0, "—", "A", "A", "no source; CPU for the PNG (~0.3–0.7 s)"),
    "analyze_death": ("economy", 2, 0, 0, "6h", "C", "A", "2 explorer SQL"),
    "get_comeback_stats": ("economy", 2, 1, 0, "6h", "C", "A", "2 explorer SQL + STRATZ Comeback"),
    "get_farm_distribution": ("economy", 2, 0, 0, "6h", "C", "A", "2 explorer SQL"),
    "get_objective_impact": ("economy", 2, 0, 0, "6h", "C", "A", "2 explorer SQL"),
    "get_farming_stats": (
        "farming",
        1,
        0,
        1,
        "6h",
        "C",
        "A",
        "DB histograms first; else 1 explorer SQL (≤300 league matches)",
    ),
    "get_gold_xp_curves": ("farming", 2, 0, 0, "6h", "C", "A", "2 explorer SQL"),
    "get_hero_benchmarks": ("farming", 1, 0, 1, "6h", "B", "A", "DB first; else /benchmarks"),
    "get_hero_lane_stats": ("farming", 1, 0, 0, "6h", "C", "A", "1 explorer SQL (league fallback)"),
    "get_hero_power_curve": (
        "farming",
        1,
        1,
        0,
        "6h",
        "B",
        "A",
        "STRATZ first, /heroes/{id}/durations fallback",
    ),
    "report_wrong_answer": (
        "feedback",
        0,
        0,
        0,
        "—",
        "A",
        "A",
        "no source; Redis push + DB row via collector",
    ),
    "suggest_feature": ("feedback", 0, 0, 0, "—", "A", "A", "no source; Redis push"),
    "get_general_stats": (
        "general",
        4,
        1,
        0,
        "6h",
        "C",
        "A",
        "STRATZ Regions; explorer + 3 constants (24h)",
    ),
    "get_rank_distribution": ("general", 1, 1, 0, "6h", "B", "A", "/distributions + STRATZ"),
    "get_records": (
        "general",
        11,
        0,
        0,
        "6h",
        "C",
        "A",
        "1 /records/{metric} per metric, 11 when all are asked",
    ),
    "compare_heroes": (
        "heroes",
        0,
        3,
        1,
        "6h",
        "B",
        "A",
        "DB first; GameVersions + HeroWinDay + HeroAverages",
    ),
    "find_hidden_gems": (
        "heroes",
        0,
        2,
        0,
        "6h",
        "B",
        "A",
        "GameVersions + HeroWinDay (all heroes in one query)",
    ),
    "get_hero_overview": (
        "heroes",
        1,
        4,
        1,
        "6h",
        "B",
        "A",
        "DB first; 4 STRATZ + /heroStats for pro bans",
    ),
    "get_tier_list": ("heroes", 1, 2, 0, "6h", "B", "A", "2 STRATZ; /heroStats when STRATZ fails"),
    "analyze_item": (
        "items",
        1,
        6,
        1,
        "6h",
        "C",
        "A",
        "DB first; all heroes = 6 batches of 25, one hero = 1; OpenDota scenarios fallback",
    ),
    "compare_items": ("items", 0, 1, 1, "6h", "B", "A", "DB first; 1 batch per 25 heroes"),
    "get_item_overview": ("items", 0, 6, 1, "6h", "C", "A", "DB first; 6 batches of 25 heroes"),
    "get_item_payback": ("items", 1, 0, 0, "6h", "C", "A", "1 explorer SQL"),
    "predict_live_win": ("live", 1, 0, 0, "60s", "B", "A", "/live (60 s cache)"),
    "analyze_laning_phase": (
        "matches",
        2,
        0,
        0,
        "7d",
        "B",
        "B",
        "/matches/{id}; unparsed adds POST /request (10 rate units, deduped 5 min)",
    ),
    "analyze_match": ("matches", 2, 0, 0, "7d", "B", "B", "as above"),
    "analyze_teamfights": ("matches", 2, 0, 0, "7d", "B", "B", "as above"),
    "analyze_vision": ("matches", 2, 0, 0, "7d", "B", "B", "as above"),
    "find_turning_points": ("matches", 2, 0, 0, "7d", "B", "B", "as above"),
    "get_match_timeline": ("matches", 2, 0, 0, "7d", "B", "B", "as above"),
    "analyze_draft": (
        "matchups",
        0,
        2,
        1,
        "6h",
        "B",
        "A",
        "DB first; Matchups + Positions; OpenDota fallback ≤10 heroes",
    ),
    "find_hero_combos": ("matchups", 0, 2, 1, "6h", "B", "A", "DB first; Matchups + Positions"),
    "get_hero_matchups": ("matchups", 0, 2, 1, "6h", "B", "A", "DB first; Matchups + Positions"),
    "suggest_pick": ("matchups", 0, 2, 1, "6h", "B", "A", "DB first; Matchups + Positions"),
    "server_info": ("meta", 0, 0, 0, "—", "A", "A", "reads quota counters from Redis only"),
    "compare_players": (
        "players",
        4,
        2,
        0,
        "15m",
        "C",
        "A",
        "2 × (profile + matches + 1 STRATZ per 100 matches)",
    ),
    "find_player_weaknesses": (
        "players",
        8,
        1,
        0,
        "15m",
        "C",
        "A",
        "profile, matches, /heroStats, ≤5 /benchmarks, STRATZ details per 100 matches",
    ),
    "get_player_hero_pool": (
        "players",
        8,
        0,
        0,
        "15m",
        "C",
        "A",
        "profile, matches, /heroStats, ≤5 /benchmarks",
    ),
    "get_player_peers": ("players", 3, 0, 0, "15m", "B", "A", "profile, matches, peers"),
    "get_player_profile": ("players", 3, 0, 0, "15m", "B", "A", "profile, wl, recentMatches"),
    "get_player_stats": (
        "players",
        2,
        1,
        0,
        "15m",
        "B",
        "A",
        "profile + matches; STRATZ details per 100 matches (≤5 at 500)",
    ),
    "get_player_trends": ("players", 2, 0, 0, "15m", "B", "A", "profile + matches"),
    "get_head_to_head": ("pro", 2, 0, 0, "6h", "C", "A", "/teams (24h) + 1 explorer SQL"),
    "get_live_matches": ("pro", 2, 0, 0, "60s", "B", "A", "/live (60 s) + /leagues (24h)"),
    "get_pro_builds": (
        "pro",
        1,
        4,
        0,
        "6h",
        "C",
        "A",
        "hero pro matches + 3 aliased batches of 10 (30 matches) + public builds; "
        "a failed batch retries match by match",
    ),
    "get_pro_matches": (
        "pro",
        3,
        2,
        0,
        "6h",
        "B",
        "A",
        "/proMatches, /leagues, /teams + drafts in batches of 10",
    ),
    "get_pro_player": ("pro", 1, 1, 0, "6h", "B", "A", "/proPlayers (24h) + STRATZ matches"),
    "get_team_stats": ("pro", 1, 3, 0, "6h", "B", "A", "/teams + up to 3 STRATZ pages of 100"),
    "get_tournament_stats": (
        "pro",
        3,
        0,
        0,
        "6h",
        "C",
        "A",
        "/leagues, 1 explorer SQL, /heroStats",
    ),
    "lookup_reference": ("reference", 0, 0, 0, "—", "A", "A", "bundled reference data, no request"),
    "find_meta_trends": ("trends", 0, 2, 0, "6h", "B", "A", "patch list + HeroTrendSeries"),
    "get_hero_patch_history": ("trends", 0, 2, 0, "6h", "B", "A", "patch list + HeroTrendSeries"),
    "get_patch_summary": (
        "trends",
        0,
        3,
        0,
        "6h",
        "B",
        "A",
        "patch list + HeroTrendSeries + PatchNotes",
    ),
}

assert len(T) == 60
WALL = {"A": 0.3, "B": 3, "C": 10}  # seconds per latency class
CPU = {"A": 0.05, "B": 0.15, "C": 0.3}  # active CPU seconds per latency class
CPU_H = 0.128  # Vercel Pro Active CPU, $/hour (iad1)
MEM_GBH = 0.0106  # Vercel Pro provisioned memory, $/GB-hour
MEM_GB = 2  # default function memory
INV = 0.60 / 1e6  # Vercel invocation
OD = 0.0001  # OpenDota Premium, $/call
RCMD = 0.20 / 1e5  # Upstash pay-as-you-go, $/command
HEADER = (
    "| Domain | Tool | Heavy | OD cold | STRATZ cold | DB first | Cache | Latency cold / warm "
    "| $ per 1k calls, cold | $ per 1k, warm | Notes |"
)


def cost(tool: str, requests: int, od: int, cls: str, cold: bool) -> tuple[float, float]:
    """(Vercel $, total $) for one call."""
    cpu = 0.5 if tool == "render_chart" else CPU[cls]
    vercel = cpu / 3600 * CPU_H + MEM_GB * WALL[cls] / 3600 * MEM_GBH + INV
    redis = 4 + (2 * requests if cold else 1)
    return vercel, vercel + od * OD + redis * RCMD


rows = []
for tool, (dom, od, st, db, ttl, cold_cls, warm_cls, note) in T.items():
    vercel, cold = cost(tool, od + st, od, cold_cls, True)
    # A warm call on an unparsed match (class B) refetches the match from OpenDota.
    warm_od = 1 if warm_cls == "B" else 0
    _, warm = cost(tool, warm_od, warm_od, warm_cls, False)
    rows.append((dom, tool, od, st, db, ttl, cold_cls, warm_cls, cold, warm, vercel, note))

print(HEADER)
print("|---|---|---|---|---|---|---|---|---|---|---|")
for dom, tool, od, st, db, ttl, cold_cls, warm_cls, cold, warm, _, note in rows:
    heavy = "yes" if tool in HEAVY_TOOLS else ""
    print(
        f"| {dom} | `{tool}` | {heavy} | {od} | {st} | {'yes' if db else ''} | {ttl} "
        f"| {cold_cls} / {warm_cls} | {cold * 1000:.3f} | {warm * 1000:.3f} | {note} |"
    )
print()
print("mean cold $/1k", statistics.mean(r[8] for r in rows) * 1000)
print("max cold $/1k", max(r[8] for r in rows) * 1000)
print("mean warm $/1k", statistics.mean(r[9] for r in rows) * 1000)
print("Vercel share of cold $/1k", statistics.mean(r[10] for r in rows) * 1000)
print("OpenDota requests", sum(r[2] for r in rows), "STRATZ requests", sum(r[3] for r in rows))

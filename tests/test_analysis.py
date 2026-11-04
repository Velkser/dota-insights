import json
import math
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP
from pydantic import ValidationError

from dota_insights import sources
from dota_insights.analytics import analysis as an
from dota_insights.cache import clear_all
from dota_insights.mcp.tools import analysis as analysis_tools
from dota_insights.sources.opendota import OpenDotaClient

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"

# Recorded explorer responses (patch 7.41, November 2026), keyed by the SQL that produced them.
EXPLORER = {
    fixture["sql"]: fixture
    for fixture in (json.loads(p.read_text()) for p in sorted(FIXTURES.glob("od_explorer_*.json")))
}
BKB_SQL = json.loads((FIXTURES / "od_explorer_hyp_pa_bkb.json").read_text())["sql"]


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def requests():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path != "/api/explorer":
            return httpx.Response(404, json={"error": "not recorded"})
        fixture = EXPLORER.get(request.url.params["sql"])
        if fixture is None:
            return httpx.Response(200, json={"rows": [], "rowCount": 0, "err": None})
        return httpx.Response(200, json=fixture)

    clear_all()
    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None, transport=httpx.MockTransport(handler), sleep=_no_sleep
        )
    )
    yield seen
    sources.use_clients()
    clear_all()


@pytest.fixture
def explorer_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": None, "err": "Query read timeout"})

    clear_all()
    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None, transport=httpx.MockTransport(handler), sleep=_no_sleep
        )
    )
    yield
    sources.use_clients()
    clear_all()


async def _call(tool: str, args: dict) -> dict:
    mcp = FastMCP("test")
    analysis_tools.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return json.loads(result.content[0].text)


def _bkb(op: str, minute: float | None = None) -> dict:
    cond = {"field": "item_minute", "item": "bkb", "op": op}
    if minute is not None:
        cond["value"] = minute
    return cond


# SQL value safety


@pytest.mark.parametrize(
    "value",
    [
        "7.41'; DROP TABLE matches; --",
        "x' OR '1'='1",
        "a\\' OR 1=1",
        "team -- comment",
        "null\x00byte",
        "line\nbreak",
        "x" * 65,
        42,
        None,
    ],
)
def test_sql_text_rejects_unsafe_values(value):
    with pytest.raises(an.UnsafeValueError):
        an.sql_text(value)


def test_sql_text_doubles_quotes():
    assert an.sql_text("O'Brien Gaming") == "'O''Brien Gaming'"
    assert an.sql_text("7.41") == "'7.41'"


@pytest.mark.parametrize("value", ["44 OR 1=1", "1; DROP TABLE x", True, 1.5, "1e3", [1], 2**63])
def test_sql_int_rejects_non_integers(value):
    with pytest.raises(an.UnsafeValueError):
        an.sql_int(value)


def test_sql_numbers():
    assert an.sql_int("42") == "42"
    assert an.sql_int(-3) == "-3"
    assert an.sql_int(20.0) == "20"
    assert an.sql_float("1.5") == "1.5"
    for bad in (math.nan, math.inf, "1.5; --", False):
        with pytest.raises(an.UnsafeValueError):
            an.sql_float(bad)
    assert an.sql_bool(True) == "TRUE"
    with pytest.raises(an.UnsafeValueError):
        an.sql_bool("1 OR 1")


def test_sql_key_only_accepts_internal_names():
    assert an.sql_key("black_king_bar") == "'black_king_bar'"
    with pytest.raises(an.UnsafeValueError):
        an.sql_key("bkb' OR 'x'='x")


# Explore query builder


def test_build_explore_sql_aggregate():
    built = an.build_explore_sql(
        an.ExploreSpec(
            table="player_matches",
            filters=[an.ExploreFilter(field="hero_id", op="=", value=44)],
            group_by=["lane_role"],
            aggregates=[an.ExploreAggregate(func="avg", field="win", alias="win_rate")],
            order_by="n",
            patch="7.41",
        )
    )
    assert built.sql == (
        "SELECT pm.lane_role AS lane_role, count(*) AS n, "
        "avg((((pm.player_slot < 128) = m.radiant_win))::int)::float8 AS win_rate "
        "FROM player_matches pm JOIN matches m USING (match_id) "
        "JOIN match_patch mp USING (match_id) "
        "WHERE mp.patch = '7.41' AND pm.hero_id = 44 GROUP BY pm.lane_role "
        "ORDER BY n DESC NULLS LAST LIMIT 100"
    )
    assert built.columns == ["lane_role", "n", "win_rate"]
    assert built.aggregated


def test_build_explore_sql_rows_and_operators():
    built = an.build_explore_sql(
        an.ExploreSpec(
            table="public_matches",
            select=["match_id", "radiant_team"],
            filters=[
                an.ExploreFilter(field="radiant_team", op="contains", value=[44, 1]),
                an.ExploreFilter(field="duration_min", op="between", value=[30, 40]),
                an.ExploreFilter(field="game_mode", op="in", value=[22, 23]),
                an.ExploreFilter(field="radiant_win", op="=", value=True),
                an.ExploreFilter(field="avg_rank_tier", op="not_null"),
            ],
            limit=10,
        )
    )
    assert built.sql == (
        "SELECT match_id AS match_id, radiant_team AS radiant_team FROM public_matches "
        "WHERE match_id > (SELECT max(match_id) FROM public_matches) - 100000 "
        "AND radiant_team @> ARRAY[44,1]::int[] AND (duration / 60) BETWEEN 30 AND 40 "
        "AND game_mode IN (22, 23) AND radiant_win = TRUE AND avg_rank_tier IS NOT NULL "
        "ORDER BY match_id DESC LIMIT 10"
    )
    assert built.applied  # the recent-window restriction is reported


def test_explicit_match_id_bound_replaces_the_window():
    built = an.build_explore_sql(
        an.ExploreSpec(
            table="public_matches",
            filters=[an.ExploreFilter(field="match_id", op=">", value=9076000000)],
        )
    )
    assert "max(match_id)" not in built.sql
    assert not built.applied


def test_text_values_are_quoted_in_explore():
    built = an.build_explore_sql(
        an.ExploreSpec(
            table="matches",
            filters=[an.ExploreFilter(field="radiant_team_name", op="=", value="O'Brien")],
            patch="7.41",
        )
    )
    assert "m.radiant_team_name = 'O''Brien'" in built.sql


@pytest.mark.parametrize(
    "spec",
    [
        # Identifiers: only whitelisted names are accepted.
        {"table": "player_matches", "select": ["hero_id FROM pg_user --"], "patch": "7.41"},
        {"table": "player_matches", "select": ["account_id"], "patch": "7.41"},
        {"table": "public_matches", "group_by": ["cluster; DROP TABLE matches"]},
        {
            "table": "public_matches",
            "aggregates": [{"func": "count", "alias": "n2 FROM pg_shadow --"}],
        },
        {"table": "public_matches", "aggregates": [{"func": "avg", "field": "(SELECT 1)"}]},
        {"table": "public_matches", "select": ["match_id"], "order_by": "1; SELECT pg_sleep(60)"},
        # Values: typed per field, never pasted.
        {
            "table": "public_matches",
            "filters": [{"field": "duration", "op": ">", "value": "0 OR 1=1"}],
        },
        {
            "table": "public_matches",
            "filters": [{"field": "radiant_win", "op": "=", "value": "true OR 1=1"}],
        },
        {
            "table": "matches",
            "patch": "7.41",
            "filters": [{"field": "radiant_team_name", "op": "=", "value": "x' OR '1'='1"}],
        },
        {
            "table": "matches",
            "patch": "7.41'; DROP TABLE matches; --",
        },
        {
            "table": "public_matches",
            "filters": [{"field": "radiant_team", "op": "contains", "value": ["1) OR (1=1"]}],
        },
        {
            "table": "public_matches",
            "filters": [{"field": "game_mode", "op": "in", "value": list(range(51))}],
        },
        # Shape rules.
        {"table": "public_matches", "select": ["duration"], "group_by": ["cluster"]},
        {
            "table": "public_matches",
            "filters": [{"field": "duration", "op": "contains", "value": [1]}],
        },
        {"table": "public_matches", "limit": 501},
        {"table": "public_matches", "recent_match_window": 5_000_000},
        {"table": "matches"},
    ],
)
def test_build_explore_sql_rejects_injection_and_bad_shapes(spec):
    with pytest.raises(ValueError):
        an.build_explore_sql(an.ExploreSpec(**spec))


def test_explore_spec_rejects_unknown_table():
    with pytest.raises(ValidationError):
        an.ExploreSpec(table="pg_user")


# Statistics


def test_fisher_exact_matches_reference():
    # R: fisher.test(matrix(c(8, 1, 2, 5), 2))$p.value = 0.03496503
    assert an.fisher_exact(8, 2, 1, 5) == pytest.approx(0.034965, abs=1e-6)
    assert an.fisher_exact(5, 5, 5, 5) == pytest.approx(1.0)


def test_mann_whitney_fully_separated():
    mw = an.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert mw.u == 0
    assert mw.rank_biserial == -1
    # z = (0 - 12.5 + 0.5) / sqrt(25 * 11 / 12)
    assert mw.z == pytest.approx(-12 / math.sqrt(25 * 11 / 12))
    assert mw.p_value == pytest.approx(0.01219, abs=1e-4)


def test_mann_whitney_ties_and_identical_samples():
    mw = an.mann_whitney([1, 1, 1], [1, 1, 1])
    assert mw.p_value == 1.0
    assert an.mann_whitney([1, 2, 2, 3], [2, 2, 3, 4]).rank_biserial < 0


def test_newcombe_interval_reference_example():
    # Newcombe (1998), example (a): 56/70 vs 48/80 -> 0.0524 to 0.3339.
    ci = an.newcombe_diff_ci(56, 70, 48, 80)
    assert ci.low == pytest.approx(0.0524, abs=1e-4)
    assert ci.high == pytest.approx(0.3339, abs=1e-4)


def test_mantel_haenszel():
    t = an.Table2x2(30, 10, 15, 25)
    single = an.mantel_haenszel([t])
    assert single.odds_ratio == pytest.approx(30 * 25 / (10 * 15))
    low, high = single.ci
    assert low < single.odds_ratio < high
    # E[a] = 40·45/80 = 22.5, Var(a) = 40·40·45·35 / (80²·79); χ² = (7.5 - 0.5)² / Var(a)
    assert single.chi2 == pytest.approx(49 / (40 * 40 * 45 * 35 / (80**2 * 79)))
    assert single.p_value == pytest.approx(0.001716, abs=1e-6)
    # Two identical strata keep the odds ratio and narrow the interval.
    double = an.mantel_haenszel([t, t])
    assert double.odds_ratio == pytest.approx(single.odds_ratio)
    assert double.ci[1] - double.ci[0] < high - low
    # No association.
    null = an.mantel_haenszel([an.Table2x2(10, 10, 10, 10)])
    assert null.odds_ratio == 1
    assert null.p_value == pytest.approx(1.0)


def test_point_biserial_constant_is_none():
    assert an.point_biserial([1, 1, 1], [True, False, True]) is None
    assert an.point_biserial([1, 2, 3], [False, False, True]) == pytest.approx(0.866, abs=1e-3)


# Conditions


def test_conditions_and_groups():
    rows = [
        {"player_slot": 0, "radiant_win": True, "t_black_king_bar": 900, "gold_per_min": 600},
        {"player_slot": 128, "radiant_win": True, "t_black_king_bar": 1500, "gold_per_min": 500},
        {"player_slot": 1, "radiant_win": False, "t_black_king_bar": None, "gold_per_min": 400},
    ]
    before = an.resolve_condition(an.Condition(**_bkb("<", 20)), "black_king_bar")
    bought = an.resolve_condition(an.Condition(**_bkb("bought")), "black_king_bar")
    dire = an.resolve_condition(an.Condition(field="side", op="==", value="Dire"), None)
    gpm = an.resolve_condition(an.Condition(field="GPM", op=">=", value=500), None)
    assert [an.holds(r, before) for r in rows] == [True, False, False]
    assert [an.holds(r, bought) for r in rows] == [True, True, False]
    assert [an.holds(r, dire) for r in rows] == [False, True, False]
    assert gpm.field == "gold_per_min"
    a, b, overlap = an.split_groups(rows, [bought], [gpm])
    assert (len(a), len(b), overlap) == (0, 0, 2)
    a, b, overlap = an.split_groups(rows, [before], None)
    assert (len(a), len(b), overlap) == (1, 2, 0)
    assert before.label() == "black_king_bar minute < 20"


@pytest.mark.parametrize(
    "cond",
    [
        {"field": "account_id", "op": "==", "value": 1},
        {"field": "gold_per_min", "op": "bought"},
        {"field": "gold_per_min", "op": ">", "value": "lots"},
        {"field": "side", "op": "<", "value": "radiant"},
    ],
)
def test_bad_conditions(cond):
    with pytest.raises(ValueError):
        an.resolve_condition(an.Condition(**cond), None)


def test_item_minute_needs_item():
    with pytest.raises(ValueError):
        an.resolve_condition(an.Condition(field="item_minute", op="<", value=20), None)


def test_player_rows_sql_embeds_only_validated_parts():
    sql = an.player_rows_sql(44, "7.41", ["black_king_bar"])
    assert sql == BKB_SQL
    with pytest.raises(an.UnsafeValueError):
        an.player_rows_sql(44, "7.41", ["x' OR '1'='1"])


# test_hypothesis


async def test_hypothesis_bkb_timing(requests):
    out = await _call(
        "test_hypothesis",
        {
            "hero": "PA",
            "group_a": [_bkb("<", 25)],
            "group_b": [_bkb(">=", 25)],
            "filters": {"patch": "7.41", "min_sample": 50},
        },
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert r["hero"] == "Phantom Assassin"
    assert r["test"] == "chi_square_2x2"
    assert (r["group_a"]["matches"], r["group_a"]["wins"]) == (61, 23)
    assert (r["group_b"]["matches"], r["group_b"]["wins"]) == (33, 19)
    assert r["effect_size"] == pytest.approx(23 / 61 - 19 / 33)
    assert r["p_value"] == pytest.approx(0.0644, abs=1e-3)
    assert not r["significant"]
    low, high = r["effect_ci"]
    assert low < r["effect_size"] < high
    assert out["sample_size"] == 94
    assert out["filters"]["patch"] == "7.41"
    assert out["confidence"]["effect_size"] == [low, high]
    assert any("not an experiment" in c for c in out["caveats"])
    assert any("Item timing is confounded" in c for c in out["caveats"])
    assert out["period"] and out["data_freshness"]
    assert "chi_square_2x2" in out["definitions"]
    assert len(requests) == 1


async def test_hypothesis_metric_outcome_against_the_rest(requests):
    out = await _call(
        "test_hypothesis",
        {
            "hero": "Phantom Assassin",
            "group_a": [_bkb("bought")],
            "outcome": "gpm",
            "filters": {"patch": "7.41"},
        },
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert r["test"] == "mann_whitney_u"
    assert r["outcome"] == "gold_per_min"
    assert r["group_b"]["label"] == "not (bought black_king_bar)"
    assert r["group_a"]["matches"] + r["group_b"]["matches"] == 182
    assert r["effect_size"] == r["group_a"]["outcome_median"] - r["group_b"]["outcome_median"]
    assert r["significant"]
    assert -1 <= r["standardized_effect"] <= 1


async def test_hypothesis_small_group_is_insufficient(requests):
    out = await _call(
        "test_hypothesis",
        {
            "hero": "PA",
            "group_a": [_bkb("<", 20)],
            "group_b": [_bkb(">=", 20)],
            "filters": {"patch": "7.41", "min_sample": 50},
        },
    )
    assert out["status"] == "insufficient_data"
    assert "Group A has 11" in out["message"]
    assert out["hint"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"hero": "PA", "group_a": [_bkb("<", 20)], "outcome": "mmr"}, "Unknown outcome"),
        ({"hero": "PA", "group_a": [{"field": "elo", "op": ">", "value": 1}]}, "Unknown condition"),
        (
            {"hero": "PA", "group_a": [{"field": "item_minute", "item": "zzqq", "op": "bought"}]},
            "No item",
        ),
        ({"hero": "zzqq", "group_a": [_bkb("bought")]}, "No hero"),
        ({"hero": "PA", "group_a": [_bkb("bought")], "filters": {"patch": "latest"}}, "patch"),
    ],
)
async def test_hypothesis_errors(requests, args, message):
    out = await _call("test_hypothesis", args)
    assert out["status"] == "error"
    assert message in out["message"]
    assert requests == []


async def test_explorer_failure_is_an_error(explorer_timeout):
    out = await _call("test_hypothesis", {"hero": "PA", "group_a": [_bkb("bought")]})
    assert out["status"] == "error"
    assert "Query read timeout" in out["message"]
    assert "Narrow it" in out["hint"]


# analyze_correlation


async def test_correlation_gpm_controls_for_duration(requests):
    out = await _call(
        "analyze_correlation", {"metric": "GPM", "hero": "PA", "filters": {"patch": "7.41"}}
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert r["metric"] == "gold_per_min"
    assert r["games"] == 182 == out["sample_size"]
    assert [s["label"] for s in r["strata"]] == ["<25", "25-35", "35-45", "45+"]
    assert sum(s["games"] for s in r["strata"]) == 182
    assert r["direction"] == "positive"
    assert r["adjusted_odds_ratio"] > 1
    low, high = out["confidence"]["adjusted_odds_ratio"]
    assert low < r["adjusted_odds_ratio"] < high
    assert r["p_value"] < 0.001
    assert any("Selection bias" in c for c in out["caveats"])


async def test_correlation_wards_on_a_support(requests):
    out = await _call(
        "analyze_correlation",
        {"metric": "wards", "hero": "Crystal Maiden", "filters": {"patch": "7.41"}},
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert r["metric"] == "obs_placed"
    assert r["games"] == 500
    stratum = next(s for s in r["strata"] if s["label"] == "25-35")
    assert stratum["high_games"] + stratum["low_games"] == stratum["games"]


async def test_correlation_insufficient_and_errors(requests):
    out = await _call(
        "analyze_correlation",
        {"metric": "gpm", "hero": "PA", "filters": {"patch": "7.41", "min_sample": 1000}},
    )
    assert out["status"] == "insufficient_data"
    out = await _call("analyze_correlation", {"metric": "duration"})
    assert out["status"] == "error"


# find_matches


async def test_find_pub_matches_with_pagination(requests):
    out = await _call(
        "find_matches",
        {"heroes": ["PA"], "enemy_heroes": ["Anti-Mage"], "min_duration_min": 30, "limit": 5},
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert len(r["matches"]) == 5 == out["sample_size"]
    for m in r["matches"]:
        assert m["duration_min"] >= 30
        pa_radiant = "Phantom Assassin" in m["radiant_heroes"]
        enemies = m["dire_heroes"] if pa_radiant else m["radiant_heroes"]
        assert "Anti-Mage" in enemies
        assert m["url"].endswith(str(m["match_id"]))
    ids = [m["match_id"] for m in r["matches"]]
    assert ids == sorted(ids, reverse=True)
    assert r["next_before_match_id"] == ids[-1]
    assert r["applied"]["game_mode"] == "ranked_all_pick"
    sql = requests[0].url.params["sql"]
    assert "lobby_type = 7" in sql and "LIMIT 6" in sql


async def test_find_matches_next_page_uses_cursor(requests):
    await _call("find_matches", {"heroes": ["PA"], "before_match_id": 9076000000, "limit": 5})
    assert "match_id < 9076000000" in requests[0].url.params["sql"]


async def test_find_league_comebacks(requests):
    out = await _call(
        "find_matches", {"min_comeback_gold": 20000, "filters": {"patch": "7.41"}, "limit": 5}
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert r["applied"]["source"] == "league"
    assert all(m["winner_max_deficit"] >= 20000 for m in r["matches"])
    assert r["next_before_match_id"] is not None


async def test_find_pub_matches_on_a_patch_by_rank(requests):
    out = await _call(
        "find_matches",
        {
            "heroes": ["PA"],
            "heroes_outcome": "win",
            "filters": {"patch": "7.41", "rank_bracket": "divine+"},
            "limit": 3,
        },
    )
    assert out["status"] == "ok"
    for m in out["result"]["matches"]:
        assert m["avg_rank_tier"] >= 70
        assert ("Phantom Assassin" in m["radiant_heroes"]) == m["radiant_win"]
    assert any("avg_rank_tier stops at 75" in c for c in out["caveats"])
    assert len(requests) == 2  # patch bounds, then the search


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"min_comeback_gold": 20000, "source": "pub"}, "league"),
        ({"heroes": ["PA"], "enemy_heroes": ["PA"]}, "both teams"),
        ({"min_duration_min": 40, "max_duration_min": 30}, "above"),
    ],
)
async def test_find_matches_errors(requests, args, message):
    out = await _call("find_matches", args)
    assert out["status"] == "error"
    assert message in out["message"]
    assert requests == []


async def test_find_matches_nothing_found(requests):
    out = await _call("find_matches", {"heroes": ["PA", "Anti-Mage", "Lion"]})
    assert out["status"] == "insufficient_data"


# explore_query


async def test_explore_query_league_aggregate(requests):
    out = await _call(
        "explore_query",
        {
            "table": "player_matches",
            "where": [{"field": "hero_id", "op": "=", "value": 44}],
            "group_by": ["lane_role"],
            "aggregates": [
                {"func": "avg", "field": "win", "alias": "win_rate"},
                {"func": "median", "field": "gold_per_min"},
            ],
            "order_by": "n",
            "patch": "7.41",
            "min_sample": 50,
        },
    )
    assert out["status"] == "ok"
    r = out["result"]
    assert r["columns"] == ["lane_role", "n", "win_rate", "median_gold_per_min"]
    assert r["rows"][0]["lane_role"] == 1
    assert out["sample_size"] == 182
    assert r["sql"] == requests[0].url.params["sql"]
    assert "lane_role" in out["definitions"]


async def test_explore_query_public_matches(requests):
    out = await _call(
        "explore_query",
        {
            "table": "public_matches",
            "where": [{"field": "lobby_type", "op": "=", "value": 7}],
            "group_by": ["rank_bracket"],
            "aggregates": [{"func": "avg", "field": "duration_min", "alias": "avg_minutes"}],
            "order_by": "rank_bracket",
            "descending": False,
        },
    )
    assert out["status"] == "ok"
    rows = out["result"]["rows"]
    assert all(isinstance(r["avg_minutes"], float) for r in rows)
    assert out["sample_size"] == sum(r["n"] for r in rows)
    assert any("newest 100,000 match ids" in c for c in out["caveats"])


@pytest.mark.parametrize(
    "args",
    [
        {"table": "player_matches", "select": ["hero_id; DROP TABLE matches"]},
        {
            "table": "player_matches",
            "where": [{"field": "hero_id", "op": "=", "value": "44 OR 1=1"}],
        },
        {"table": "matches", "patch": "7.41' OR '1'='1"},
        {"table": "public_matches", "order_by": "match_id; SELECT pg_sleep(100)"},
    ],
)
async def test_explore_query_injection_never_reaches_the_explorer(requests, args):
    out = await _call("explore_query", args)
    assert out["status"] == "error"
    assert requests == []


async def test_explore_query_rejects_raw_sql_and_unknown_tables(requests):
    mcp = FastMCP("test")
    analysis_tools.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "explore_query", {"table": "pg_shadow"}, raise_on_error=False
        )
        assert result.is_error
        result = await client.call_tool(
            "explore_query",
            {"table": "matches", "sql": "SELECT * FROM pg_shadow"},
            raise_on_error=False,
        )
        assert result.is_error
    assert requests == []


async def test_explore_query_below_min_sample(requests):
    out = await _call(
        "explore_query",
        {
            "table": "player_matches",
            "where": [{"field": "hero_id", "op": "=", "value": 44}],
            "group_by": ["lane_role"],
            "order_by": "n",
            "patch": "7.41",
            "aggregates": [
                {"func": "avg", "field": "win", "alias": "win_rate"},
                {"func": "median", "field": "gold_per_min"},
            ],
            "min_sample": 500,
        },
    )
    assert out["status"] == "insufficient_data"


async def test_tools_are_registered():
    mcp = FastMCP("test")
    analysis_tools.register(mcp)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"test_hypothesis", "analyze_correlation", "find_matches", "explore_query"} <= names

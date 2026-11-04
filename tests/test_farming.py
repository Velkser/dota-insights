import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP

from dota_insights import sources
from dota_insights.analytics import farming
from dota_insights.cache import clear_all
from dota_insights.mcp.tools import farming as farming_tools
from dota_insights.models import Filters, Outcome
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "farming"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


OPENDOTA_ROUTES = {
    ("/api/benchmarks", (("hero_id", "1"),)): "od_benchmarks_hero_id_1.json",
    (
        "/api/benchmarks",
        (("bracket", "7"), ("hero_id", "1"), ("role", "core")),
    ): "od_benchmarks_hero_id_1_bracket_7_role_core.json",
    ("/api/heroes/1/durations", ()): "od_heroes_1_durations.json",
}

# Recorded explorer responses, keyed by the SQL that produced them (patch 7.41).
EXPLORER = {
    fixture["sql"]: fixture
    for fixture in (_load(f"od_explorer_{name}.json") for name in ("farm_1", "farm_44", "lane_1"))
}

# Recorded STRATZ responses, keyed by the query variables that produced them.
STRATZ = {
    json.dumps(fixture["variables"], sort_keys=True): fixture["data"]
    for fixture in (
        _load(f"stratz_{name}.json") for name in ("power_1", "power_1_divine_pos1", "lane_1")
    )
}
STRATZ_EMPTY = {"heroStats": {"winWeek": [], "vs": [], "with": [], "stats": []}}


@pytest.fixture
def requests():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/explorer":
            fixture = EXPLORER.get(request.url.params["sql"])
            if fixture is None:
                return httpx.Response(200, json={"rows": [], "rowCount": 0, "err": None})
            return httpx.Response(200, json=fixture)
        key = (request.url.path, tuple(sorted(request.url.params.items())))
        name = OPENDOTA_ROUTES.get(key)
        if name is None:
            return httpx.Response(404, json={"error": "not recorded"})
        return httpx.Response(200, json=_load(name))

    def stratz_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        variables = json.loads(request.content)["variables"]
        data = STRATZ.get(json.dumps(variables, sort_keys=True), STRATZ_EMPTY)
        return httpx.Response(200, json={"data": data})

    clear_all()
    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None, transport=httpx.MockTransport(handler), sleep=_no_sleep
        ),
        stratz=StratzClient(
            token="test", transport=httpx.MockTransport(stratz_handler), sleep=_no_sleep
        ),
    )
    yield seen
    sources.use_clients()
    clear_all()


@pytest.fixture
def stratz_down(requests):
    """STRATZ has no token, so tools fall back to OpenDota."""
    sources.use_clients(opendota=sources.get_opendota(), stratz=StratzClient(token=None))
    return requests


async def _call(tool: str, args: dict) -> dict:
    mcp = FastMCP("test")
    farming_tools.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return json.loads(result.content[0].text)


# Analytics


def test_place_value_interpolates_and_clamps():
    points = farming.parse_benchmarks(_load("od_benchmarks_hero_id_1.json"))["gold_per_min"]
    # Recorded AM all-ranks GPM: p20 = 505, p30 = 566.
    pct, label = farming.place_value(points, 520)
    assert pct == pytest.approx(0.2 + 15 / 61 * 0.1)
    assert label == "p22"
    assert farming.place_value(points, 669) == (0.5, "p50")
    assert farming.place_value(points, 100) == (0.1, "below p10")
    assert farming.place_value(points, 5000) == (0.99, "above p99")


def test_metric_aliases():
    assert farming.metric_name("GPM") == "gold_per_min"
    assert farming.metric_name("last hits") == "last_hits_per_min"
    assert farming.metric_name("tower_damage") == "tower_damage"
    assert farming.metric_name("mmr") is None


def test_benchmark_params_maps_filters():
    assert farming.benchmark_params(Filters()) == (None, None)
    assert farming.benchmark_params(Filters(rank_bracket="divine", position=2)) == (7, "core")
    assert farming.benchmark_params(Filters(position=5)) == (None, "support")
    with pytest.raises(ValueError, match="single medal"):
        farming.benchmark_params(Filters(rank_bracket="divine+"))
    with pytest.raises(ValueError, match="Immortal"):
        farming.benchmark_params(Filters(rank_bracket="immortal"))


def test_bucket_durations_sums_bins():
    rows = _load("od_heroes_1_durations.json")
    buckets = farming.bucket_durations(rows)
    assert sum(b.games for b in buckets) == sum(r["games_played"] for r in rows)
    by_label = {b.label: b for b in buckets}
    # Bin 1800 s holds matches of 30:00-34:59.
    thirty = next(r for r in rows if r["duration_bin"] == 1800)
    assert by_label["30-35"].games == thirty["games_played"]
    assert by_label["30-35"].wins == thirty["wins"]
    assert buckets[0].label == "<20" and buckets[-1].label == "50+"


def test_power_curve_classification_rule():
    def bucket(start, end, wins, games):
        return farming.DurationBucket(
            label=str(start), from_min=start, to_min=end, games=games, wins=wins,
            win_rate=wins / games, ci_low=0, ci_high=1,
        )  # fmt: skip

    late = farming.power_curve("X", 1, [bucket(20, 25, 400, 1000), bucket(40, 45, 600, 1000)])
    assert late.classification == "late" and late.peak == "40"
    early = farming.power_curve("X", 1, [bucket(20, 25, 600, 1000), bucket(40, 45, 400, 1000)])
    assert early.classification == "early"
    flat = farming.power_curve("X", 1, [bucket(20, 25, 50, 100), bucket(40, 45, 52, 100)])
    assert flat.classification == "flat" and flat.p_value > 0.05
    none = farming.power_curve("X", 1, [bucket(30, 35, 10, 20)])
    assert none.classification == "flat" and none.peak is None and none.p_value is None


# Tools


async def test_get_hero_benchmarks_places_value(requests):
    body = await _call("get_hero_benchmarks", {"hero": "am", "value": {"gpm": 520}})
    assert body["status"] == "ok"
    result = body["result"]
    assert result["hero"] == "Anti-Mage"
    assert result["metrics"]["gold_per_min"][4] == {"percentile": 0.5, "value": 669}
    assert result["placements"] == [
        {"metric": "gold_per_min", "value": 520.0, "percentile": pytest.approx(0.2246, abs=1e-4),
         "label": "p22"}
    ]  # fmt: skip
    assert body["source"] == ["opendota"]
    assert "gold_per_min" in body["definitions"]
    assert any("sample size" in c for c in body["caveats"])


async def test_get_hero_benchmarks_passes_bracket_and_role(requests):
    filters = {"rank_bracket": "divine", "position": 1}
    body = await _call("get_hero_benchmarks", {"hero": "Anti-Mage", "filters": filters})
    assert body["status"] == "ok"
    assert body["result"]["bracket"] == "divine"
    assert body["result"]["role"] == "core"
    assert body["result"]["metrics"]["gold_per_min"][4]["value"] == 713
    assert dict(requests[0].url.params) == {"hero_id": "1", "bracket": "7", "role": "core"}


async def test_get_hero_benchmarks_rejects_bad_input(requests):
    body = await _call("get_hero_benchmarks", {"hero": "am", "value": {"mmr": 5000}})
    assert body["status"] == "error" and "mmr" in body["message"]
    body = await _call(
        "get_hero_benchmarks", {"hero": "am", "filters": {"rank_bracket": "immortal"}}
    )
    assert body["status"] == "error"
    body = await _call("get_hero_benchmarks", {"hero": "zzzzqqq"})
    assert body["status"] == "error"
    assert requests == []


async def test_get_hero_power_curve_falls_back_to_opendota(stratz_down):
    body = await _call("get_hero_power_curve", {"hero": "Anti-Mage"})
    assert body["status"] == "ok"
    rows = _load("od_heroes_1_durations.json")
    assert body["sample_size"] == sum(r["games_played"] for r in rows)
    result = body["result"]
    assert result["classification"] == "late"
    assert result["late_win_rate"] > result["early_win_rate"]
    assert "win_rate[30-35]" in body["confidence"]
    assert "classification" in body["definitions"]


async def test_get_hero_power_curve_fallback_reports_ignored_filters(stratz_down):
    body = await _call(
        "get_hero_power_curve",
        {"hero": "am", "filters": {"rank_bracket": "divine", "min_sample": 1_000_000}},
    )
    assert body["status"] == "insufficient_data"
    assert body["filters"]["rank_bracket"] is None
    assert any("rank_bracket" in c for c in body["caveats"])


# Explorer-based analytics


def _rows(name: str) -> list[dict]:
    return _load(f"od_explorer_{name}.json")["rows"]


def test_recorded_sql_matches_query_builders():
    assert _load("od_explorer_farm_1.json")["sql"] == farming.farm_sql(1, "7.41")
    assert _load("od_explorer_lane_1.json")["sql"] == farming.lane_sql(1, "7.41")


def test_explorer_patch():
    assert farming.explorer_patch(Filters(patch="7.41f")) == "7.41"
    assert farming.explorer_patch(Filters(patch="7.40")) == "7.40"
    with pytest.raises(ValueError):
        farming.explorer_patch(Filters(patch="7.41'; DROP TABLE matches; --"))


def test_farm_at_minutes_on_recorded_matches():
    rows = _rows("farm_1")
    ten, thirty = farming.farm_at_minutes(rows, [10, 30], min_games=100)
    assert ten.games == len(rows)
    golds = sorted(r["gold_t"][10] for r in rows)
    assert ten.gold.q1 <= ten.gold.median <= ten.gold.q3
    assert golds[0] <= ten.gold.q1 and ten.gold.q3 <= golds[-1]
    assert ten.gpm.median == pytest.approx(ten.gold.median / 10)
    assert thirty.games == sum(1 for r in rows if len(r["gold_t"]) > 30)
    (late,) = farming.farm_at_minutes(rows, [80], min_games=100)
    assert late.gold is None and late.games < 100


def test_spread_quartiles():
    s = farming.spread([1, 2, 3, 4, 5])
    assert (s.q1, s.median, s.q3) == (2, 3, 4)
    assert farming.spread([7]) == farming.Spread(median=7, q1=7, q3=7)


def test_filter_outcome_uses_player_side():
    rows = [
        {"player_slot": 0, "radiant_win": True},
        {"player_slot": 128, "radiant_win": True},
        {"player_slot": 132, "radiant_win": False},
    ]
    assert farming.filter_outcome(rows, Outcome.WIN) == [rows[0], rows[2]]
    assert farming.filter_outcome(rows, Outcome.LOSS) == [rows[1]]


def test_lane_result_rule():
    assert farming.lane_result([1100], [1000]) == "draw"
    assert farming.lane_result([1200], [1000]) == "win"
    assert farming.lane_result([1000, 600], [1000]) == "loss"
    assert farming.lane_result([0], [0]) == "draw"


def _player(slot, hero, lane, gold, roaming=False, radiant_win=True):
    return {
        "match_id": 1, "player_slot": slot, "hero_id": hero, "lane": lane,
        "is_roaming": roaming, "gold_10": gold, "lh_10": gold // 50,
        "radiant_win": radiant_win, "start_time": 0,
    }  # fmt: skip


def test_lane_stats_counts_opponents_and_partners(monkeypatch):
    monkeypatch.setattr(farming, "MIN_PAIR_GAMES", 1)
    rows = [
        _player(0, 1, 1, 4000),  # Anti-Mage, safe lane bottom
        _player(1, 5, 1, 1500),  # Crystal Maiden with him
        _player(2, 14, 2, 3000),  # Pudge mid, other lane
        _player(128, 2, 1, 2000),  # Axe against them
        _player(129, 26, 3, 1000, roaming=True),
    ]
    stats = farming.lane_stats("Anti-Mage", 1, "7.41", rows)
    assert stats.games == 1 and stats.skipped == 0
    assert stats.lane_win_rate == 1.0  # (4000 + 1500) / 2 = 2750 vs 2000
    assert [m.hero for m in stats.best_opponents] == ["Axe"]
    assert [m.hero for m in stats.best_partners] == ["Crystal Maiden"]
    assert stats.match_win_rate_after_lane_win == 1.0
    assert stats.match_win_rate_after_lane_loss is None


def test_lane_stats_on_recorded_matches():
    stats = farming.lane_stats("Anti-Mage", 1, "7.41", _rows("lane_1"))
    assert stats.games + stats.skipped == len({r["match_id"] for r in _rows("lane_1")})
    total = stats.lane_win_rate + stats.lane_draw_rate + stats.lane_loss_rate
    assert total == pytest.approx(1.0)
    assert all(m.games >= farming.MIN_PAIR_GAMES for m in stats.best_opponents)
    assert len(stats.worst_opponents) == 5


# Explorer-based tools


async def test_get_farming_stats(requests):
    body = await _call("get_farming_stats", {"hero": "anti-mage"})
    assert body["status"] == "ok", body
    assert body["filters"]["patch"] == "7.41"
    assert body["sample_size"] == len(_rows("farm_1"))
    minutes = {m["minute"]: m for m in body["result"]["minutes"]}
    assert sorted(minutes) == [10, 15, 20, 25, 30]
    assert minutes[10]["last_hits"]["median"] == 57
    assert body["period"]["from_date"] >= "2026-03-24"
    assert any("league" in c for c in body["caveats"])


async def test_get_farming_stats_outcome_and_ignored_filters(requests):
    args = {"hero": "am", "outcome": "win", "filters": {"position": 1, "min_sample": 50}}
    body = await _call("get_farming_stats", args)
    assert body["status"] == "ok"
    rows = _rows("farm_1")
    assert body["sample_size"] == len(farming.filter_outcome(rows, Outcome.WIN))
    assert body["filters"]["position"] is None
    assert any("position" in c for c in body["caveats"])


async def test_get_farming_stats_insufficient_and_errors(requests):
    body = await _call("get_farming_stats", {"hero": "am", "filters": {"patch": "7.30"}})
    assert body["status"] == "insufficient_data"
    assert body["sample_size"] == 0
    body = await _call("get_farming_stats", {"hero": "am", "minutes": [0]})
    assert body["status"] == "error"


async def test_get_gold_xp_curves_side_by_side(requests):
    body = await _call("get_gold_xp_curves", {"heroes": ["am", "phantom assassin"]})
    assert body["status"] == "ok", body
    curves = {c["hero"]: c for c in body["result"]}
    assert set(curves) == {"Anti-Mage", "Phantom Assassin"}
    am = curves["Anti-Mage"]
    assert am["wins"][0]["minute"] == 0
    assert am["wins"][0]["games"] + am["losses"][0]["games"] == am["games"]
    assert all(p["games"] >= farming.MIN_CURVE_GAMES for p in am["losses"])
    assert body["sample_size"] == min(c["games"] for c in curves.values())


async def test_get_gold_xp_curves_skips_small_heroes(requests):
    body = await _call("get_gold_xp_curves", {"heroes": ["am", "pudge"], "outcome": "loss"})
    assert body["status"] == "ok"
    assert [c["hero"] for c in body["result"]] == ["Anti-Mage"]
    assert body["result"][0]["wins"] is None
    assert any("Pudge (0)" in c for c in body["caveats"])
    body = await _call("get_gold_xp_curves", {"heroes": ["pudge"]})
    assert body["status"] == "insufficient_data"


async def test_get_hero_lane_stats_falls_back_to_league(stratz_down):
    body = await _call("get_hero_lane_stats", {"hero": "am"})
    assert body["status"] == "ok", body
    result = body["result"]
    assert body["sample_size"] == result["games"]
    low, high = body["confidence"]["lane_win_rate"]
    assert low <= result["lane_win_rate"] <= high
    assert "lane outcome" in body["definitions"]
    assert result["best_opponents"] and result["worst_partners"]
    assert body["source"] == ["opendota"]
    assert any("STRATZ was unavailable" in c for c in body["caveats"])


# STRATZ analytics


def test_stratz_filter_mapping():
    f = Filters(rank_bracket="legend-divine", position=3, game_mode="turbo")
    assert farming.stratz_medals(f) == ["LEGEND", "ANCIENT", "DIVINE"]
    assert farming.stratz_basic_brackets(f) == ["LEGEND_ANCIENT", "DIVINE_IMMORTAL"]
    assert farming.stratz_basic_brackets(Filters(rank_bracket="herald")) == ["HERALD_GUARDIAN"]
    assert farming.stratz_basic_brackets(Filters()) is None
    assert farming.stratz_position(f) == ["POSITION_3"]
    assert farming.stratz_mode(f) == "TURBO"
    assert farming.stratz_mode(Filters()) == "ALL_PICK_RANKED"


def test_recorded_stratz_variables_match_builders():
    power = _load("stratz_power_1_divine_pos1.json")["variables"]
    flt = Filters(rank_bracket="divine+", position=1)
    assert power == farming.power_curve_variables(1, flt)
    assert _load("stratz_lane_1.json")["variables"] == farming.lane_variables(1, Filters())


def test_stratz_duration_rows_keeps_patch_weeks_and_drops_abandons():
    rows = [
        {"week": 1_780_000_000, "durationMinute": 6, "winCount": 5, "matchCount": 10},
        {"week": 1_780_000_000, "durationMinute": 1, "winCount": 9, "matchCount": 10},
        {"week": 1_700_000_000, "durationMinute": 6, "winCount": 1, "matchCount": 10},
    ]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bins, weeks = farming.stratz_duration_rows(rows, start, None)
    assert bins == [{"duration_bin": 1800, "games_played": 10, "wins": 5}]
    assert weeks == [datetime.fromtimestamp(1_780_000_000, UTC).date()]


def test_pub_lane_stats_on_recorded_response():
    data = _load("stratz_lane_1.json")["data"]
    stats = farming.pub_lane_stats("Anti-Mage", 1, Filters(), data)
    vs = data["heroStats"]["vs"]
    assert stats.pairings == sum(r["winCount"] + r["drawCount"] + r["lossCount"] for r in vs)
    total = stats.lane_win_rate + stats.lane_draw_rate + stats.lane_loss_rate
    assert total == pytest.approx(1.0)
    ten = next(r for r in data["heroStats"]["stats"] if r["time"] == 10)
    assert stats.networth_10_avg == ten["networth"]
    assert all(m.games >= farming.STRATZ_MIN_PAIR_GAMES for m in stats.worst_opponents)
    edges = [m.lane_win_rate - m.lane_loss_rate for m in stats.best_opponents]
    assert edges == sorted(edges, reverse=True)


def test_power_curve_peak_ignores_short_games():
    def bucket(start, end, wins, games):
        return farming.DurationBucket(
            label=str(start), from_min=start, to_min=end, games=games, wins=wins,
            win_rate=wins / games, ci_low=0, ci_high=1,
        )  # fmt: skip

    curve = farming.power_curve("X", 1, [bucket(0, 20, 90, 100), bucket(40, 45, 55, 100)])
    assert curve.peak == "40"


# STRATZ tools


async def test_get_hero_power_curve_from_stratz(requests):
    body = await _call("get_hero_power_curve", {"hero": "am"})
    assert body["status"] == "ok", body
    assert body["source"] == ["stratz"]
    assert body["filters"]["patch"] == "7.41"
    assert body["filters"]["game_mode"] == "ranked_all_pick"
    result = body["result"]
    assert result["classification"] == "late"
    assert result["peak"] not in (None, "<20")
    assert body["sample_size"] == sum(b["games"] for b in result["buckets"])
    assert body["period"]["from_date"] >= "2026-03-24"
    assert any("stratz.com" in c for c in body["caveats"])


async def test_get_hero_power_curve_from_stratz_with_filters(requests):
    filters = {"rank_bracket": "divine+", "position": 1}
    body = await _call("get_hero_power_curve", {"hero": "Anti-Mage", "filters": filters})
    assert body["status"] == "ok"
    assert body["filters"]["rank_bracket"] == "divine+"
    assert body["filters"]["position"] == 1
    sent = json.loads(requests[-1].content)["variables"]
    assert sent["brackets"] == ["DIVINE", "IMMORTAL"] and sent["positions"] == ["POSITION_1"]


async def test_get_hero_power_curve_from_stratz_old_patch_is_insufficient(requests):
    body = await _call("get_hero_power_curve", {"hero": "am", "filters": {"patch": "7.39"}})
    assert body["status"] == "insufficient_data"
    assert body["source"] == ["stratz"]


async def test_get_hero_lane_stats_from_stratz(requests):
    body = await _call("get_hero_lane_stats", {"hero": "am"})
    assert body["status"] == "ok", body
    assert body["source"] == ["stratz"]
    result = body["result"]
    assert body["sample_size"] == result["pairings"]
    low, high = body["confidence"]["lane_win_rate"]
    assert low <= result["lane_win_rate"] <= high
    assert result["best_opponents"] and result["worst_partners"]
    assert result["networth_10_avg"] > 0


async def test_get_hero_lane_stats_widens_medal_to_stratz_pair(requests):
    body = await _call("get_hero_lane_stats", {"hero": "am", "filters": {"rank_bracket": "divine"}})
    assert body["status"] == "insufficient_data"  # not recorded: the mock returns no rows
    assert body["filters"]["rank_bracket"] == "divine-immortal"
    assert any("widened" in c for c in body["caveats"])

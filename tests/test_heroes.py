import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import heroes as analytics
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "heroes"
STRATZ_FIXTURES = [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("stratz_*.json"))]
OPENDOTA_HERO_STATS = json.loads((FIXTURES / "opendota_hero_stats.json").read_text())
DIVINE_PLUS = {"rank_bracket": "divine+"}


async def _no_sleep(_: float) -> None:
    return None


def _stratz_ok(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    operation = re.search(r"query (\w+)", payload["query"]).group(1)
    for fixture in STRATZ_FIXTURES:
        if fixture["operation"] == operation and fixture["variables"] == payload["variables"]:
            return httpx.Response(200, json=fixture["response"])
    raise AssertionError(f"no fixture for {operation} {payload['variables']}")


def _stratz_down(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def _opendota(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/heroStats")
    return httpx.Response(200, json=OPENDOTA_HERO_STATS)


def _use(stratz_handler) -> None:
    sources.use_clients(
        stratz=StratzClient(
            token="t", transport=httpx.MockTransport(stratz_handler), sleep=_no_sleep
        ),
        opendota=OpenDotaClient(transport=httpx.MockTransport(_opendota), sleep=_no_sleep),
    )


@pytest.fixture(autouse=True)
async def clients():
    clear_all()
    _use(_stratz_ok)
    yield
    await sources.close_clients()
    clear_all()


async def _call(name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    return result.structured_content


def _fixture(operation: str, **variables) -> dict:
    for fixture in STRATZ_FIXTURES:
        if fixture["operation"] == operation and all(
            fixture["variables"].get(k) == v for k, v in variables.items()
        ):
            return fixture
    raise KeyError(operation)


def _expected_counts(rows: list[dict]) -> dict[int, tuple[int, int]]:
    """Per-hero (games, wins) over the rows inside the live patch (7.41f)."""
    versions = _fixture("GameVersions")["response"]["data"]["constants"]["gameVersions"]
    start = max(v["asOfDateTime"] for v in versions)
    start_day = datetime.fromtimestamp(start, UTC).replace(hour=0).timestamp()
    counts: dict[int, tuple[int, int]] = {}
    for row in rows:
        if row["day"] >= start_day:
            games, wins = counts.get(row["heroId"], (0, 0))
            counts[row["heroId"]] = (games + row["matchCount"], wins + row["winCount"])
    return counts


async def test_tool_list_contains_hero_tools():
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"get_hero_overview", "compare_heroes", "get_tier_list", "find_hidden_gems"} <= names


async def test_tier_list_ranks_by_smoothed_win_rate():
    out = await _call("get_tier_list", {"filters": DIVINE_PLUS, "limit": 10})
    assert out["status"] == "ok"
    assert out["source"] == ["stratz"]
    assert out["filters"]["patch"] == "7.41f"
    assert out["filters"]["game_mode"] == "ranked_all_pick"
    assert "tier" in out["definitions"] and "smoothed_win_rate" in out["definitions"]
    assert any("stratz.com" in c for c in out["caveats"])

    rows = _fixture("HeroWinDay", positionIds=None)["response"]["data"]["heroStats"]["winDay"]
    expected = _expected_counts(rows)
    total = round(sum(g for g, _ in expected.values()) / 10)
    assert out["sample_size"] == total

    heroes = out["result"]["heroes"]
    assert len(heroes) == 10
    scores = [h["smoothed_win_rate"] for h in heroes]
    assert scores == sorted(scores, reverse=True)
    prior = out["result"]["pool_win_rate"]
    for entry in heroes:
        games, wins = expected[entry["hero_id"]]
        assert entry["matches"] == games
        assert entry["win_rate"] == pytest.approx(wins / games)
        smoothed = (wins + prior * analytics.PRIOR_STRENGTH) / (games + analytics.PRIOR_STRENGTH)
        assert entry["smoothed_win_rate"] == pytest.approx(smoothed)
        assert entry["tier"] == analytics.assign_tier(smoothed - prior)
        assert entry["pick_rate"] == pytest.approx(games / total)
        low, high = entry["win_rate_ci"]
        assert low < entry["win_rate"] < high
        assert out["confidence"][f"{entry['hero']}.win_rate"] == [low, high]


async def test_tier_list_for_a_position_uses_two_slots_per_match():
    filters = {"rank_bracket": "immortal", "position": 1}
    out = await _call("get_tier_list", {"filters": filters, "limit": 5})
    assert out["status"] == "ok"
    assert out["filters"]["position"] == 1
    rows = _fixture("HeroWinDay", positionIds=["POSITION_1"])["response"]["data"]["heroStats"][
        "winDay"
    ]
    expected = _expected_counts(rows)
    assert out["sample_size"] == round(sum(g for g, _ in expected.values()) / 2)


async def test_tier_list_below_min_sample_is_insufficient():
    out = await _call("get_tier_list", {"filters": DIVINE_PLUS | {"min_sample": 10_000_000}})
    assert out["status"] == "insufficient_data"
    assert out["result"] is None
    assert out["hint"]


def test_assign_tier_cutoffs():
    assert analytics.assign_tier(0.025) == "S"
    assert analytics.assign_tier(0.0249) == "A"
    assert analytics.assign_tier(0.01) == "A"
    assert analytics.assign_tier(0.0) == "B"
    assert analytics.assign_tier(-0.01) == "B"
    assert analytics.assign_tier(-0.02) == "C"
    assert analytics.assign_tier(-0.03) == "D"


async def test_hero_overview():
    out = await _call("get_hero_overview", {"hero": "PA", "filters": DIVINE_PLUS})
    assert out["status"] == "ok"
    assert out["source"] == ["stratz", "opendota"]
    result = out["result"]
    assert result["hero"] == "Phantom Assassin"
    assert result["hero_id"] == 44
    assert "Carry" in result["roles"]
    assert result["patch"] == "7.41f"

    rows = _fixture("HeroWinDay", positionIds=None)["response"]["data"]["heroStats"]["winDay"]
    games, wins = _expected_counts(rows)[44]
    assert result["current"]["matches"] == games == out["sample_size"]
    assert result["current"]["win_rate"] == pytest.approx(wins / games)
    assert out["confidence"]["win_rate"] == result["current"]["win_rate_ci"]

    assert result["typical_positions"] == ["carry"]
    shares = [p["share_of_hero_games"] for p in result["by_position"]]
    assert sum(shares) == pytest.approx(1.0)
    assert [b["group"] for b in result["by_bracket"]][-1] == "immortal"
    assert len(result["by_bracket"]) == 8
    assert 1 <= result["win_rate_rank"] <= result["ranked_heroes"]

    trend = result["trend"]
    assert 1 <= len(trend) <= analytics.TREND_PATCHES
    assert trend[-1]["patch"] == "7.41f" and trend[-1]["partial"]

    pro = result["pro"]
    row = next(r for r in OPENDOTA_HERO_STATS if r["id"] == 44)
    assert pro["picks"] == row["pro_pick"] and pro["bans"] == row["pro_ban"]
    total = round(sum(r["pro_pick"] or 0 for r in OPENDOTA_HERO_STATS) / 10)
    assert pro["pro_matches"] == total
    assert pro["ban_rate"] == pytest.approx(row["pro_ban"] / total)


async def test_hero_overview_rejects_unknown_hero():
    out = await _call("get_hero_overview", {"hero": "zzzzqqq"})
    assert out["status"] == "error"
    assert "No hero" in out["message"]


async def test_compare_heroes():
    out = await _call("compare_heroes", {"heroes": ["am", "pa", "jugg"], "filters": DIVINE_PLUS})
    assert out["status"] == "ok"
    assert out["source"] == ["stratz"]
    heroes = out["result"]["heroes"]
    assert [h["hero"] for h in heroes] == ["Anti-Mage", "Phantom Assassin", "Juggernaut"]
    assert set(heroes[0]["values"]) == set(analytics.METRICS)
    assert all(v == 0 for v in heroes[0]["delta_vs_first"].values())

    averages = {
        r["heroId"]: r for r in _fixture("HeroAverages")["response"]["data"]["heroStats"]["stats"]
    }
    am = averages[1]
    assert heroes[0]["values"]["kda"] == pytest.approx((am["kills"] + am["assists"]) / am["deaths"])
    assert heroes[0]["values"]["xpm"] == pytest.approx(am["xp"] / am["time"])
    pa_delta = heroes[1]["delta_vs_first"]["networth"]
    assert pa_delta == pytest.approx(averages[44]["networth"] - am["networth"])

    tests = out["result"]["win_rate_tests"]
    assert len(tests) == 3
    for test in tests:
        assert test["q_value"] >= test["p_value"]
        assert test["significant"] == (test["q_value"] <= 0.05)
    assert out["sample_size"] == min(h["matches"] for h in heroes)


async def test_compare_heroes_selected_metrics_skip_averages():
    out = await _call(
        "compare_heroes",
        {"heroes": ["am", "pa"], "metrics": ["win_rate"], "filters": DIVINE_PLUS},
    )
    assert out["status"] == "ok"
    assert set(out["result"]["heroes"][0]["values"]) == {"win_rate"}
    assert out["result"]["averages_sample"] == {}


async def test_compare_heroes_rejects_duplicates():
    out = await _call("compare_heroes", {"heroes": ["am", "anti-mage"]})
    assert out["status"] == "error"
    assert "twice" in out["message"]


async def test_hidden_gems():
    out = await _call("find_hidden_gems", {"filters": DIVINE_PLUS, "max_pick_rate": 0.05})
    assert out["status"] == "ok"
    gems = out["result"]["gems"]
    assert gems
    assert [g["smoothed_win_rate"] for g in gems] == sorted(
        (g["smoothed_win_rate"] for g in gems), reverse=True
    )
    for gem in gems:
        assert gem["pick_rate"] <= 0.05
        assert gem["edge"] >= analytics.GEM_MIN_EDGE
        assert gem["matches"] >= 100
        assert gem["significant"] == (gem["q_value"] <= 0.05)


async def test_falls_back_to_opendota_when_stratz_is_down():
    await sources.close_clients()
    _use(_stratz_down)
    out = await _call("get_tier_list", {"filters": DIVINE_PLUS, "limit": 5})
    assert out["status"] == "ok"
    assert out["source"] == ["opendota"]
    assert any("Immortal into Divine" in c for c in out["caveats"])
    top = out["result"]["heroes"][0]
    row = next(r for r in OPENDOTA_HERO_STATS if r["id"] == top["hero_id"])
    assert top["matches"] == row["7_pick"]

    out = await _call("get_tier_list", {"filters": {"position": 2}})
    assert out["status"] == "error"
    assert out["hint"]


async def test_unknown_patch_is_an_error():
    out = await _call("get_tier_list", {"filters": {"patch": "6.01"}})
    assert out["status"] == "error"
    assert "7.41f" in out["hint"]


def _versions() -> tuple[analytics.GameVersion, ...]:
    raw = _fixture("GameVersions")["response"]["data"]["constants"]["gameVersions"]
    return tuple(
        sorted(
            (
                analytics.GameVersion(v["name"], datetime.fromtimestamp(v["asOfDateTime"], UTC))
                for v in raw
            ),
            key=lambda v: v.start,
        )
    )


def test_patch_window():
    versions = _versions()
    by_name = {v.name: v for v in versions}
    live = analytics.patch_window(versions, None)
    assert live.name == "7.41f" and live.current and live.end is None
    major = analytics.patch_window(versions, "7.41")
    assert major.start == by_name["7.41"].start and major.current
    lettered = analytics.patch_window(versions, "7.41d")
    assert lettered.end == by_name["7.41e"].start and not lettered.current
    assert analytics.patch_window(versions, "9.99") is None


def test_trend_by_patch_sums_weeks():
    rows = _fixture("HeroWinWeek")["response"]["data"]["heroStats"]["winWeek"]
    trend = analytics.trend_by_patch(rows, _versions())
    assert sum(t.matches for t in trend) <= sum(r["matchCount"] for r in rows)
    assert [t.patch for t in trend] == sorted(t.patch for t in trend)

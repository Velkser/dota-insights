import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP

from dota_insights import sources
from dota_insights.analytics import matchups as mu
from dota_insights.cache import clear_all
from dota_insights.mcp.tools import matchups as tools
from dota_insights.models import Filters
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "matchups"
# Heroes in stratz_matchup_divine.json (Divine/Immortal): AM, Lion, Axe, Invoker, CM,
# Pudge, Juggernaut, Shadow Shaman, Mars, Witch Doctor.
AM, LION, AXE, INVOKER, CM, PUDGE, JUGG, SHAMAN, MARS, WD = 1, 26, 2, 74, 5, 14, 8, 27, 129, 30


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _reset():
    clear_all()
    yield
    clear_all()
    sources.use_clients()


@pytest.fixture
def stratz_requests():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "matchUp" in body["query"]:
            return httpx.Response(200, json=_load("stratz_matchup_divine.json"))
        if "groupByPosition" in body["query"]:
            return httpx.Response(200, json=_load("stratz_positions_divine.json"))
        return httpx.Response(400)

    stratz = StratzClient(token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    sources.use_clients(stratz=stratz)
    return seen


@pytest.fixture
def opendota_only():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/heroes/1/matchups":
            return httpx.Response(200, json=_load("opendota_hero_1_matchups.json"))
        if request.url.path == "/api/heroStats":
            return httpx.Response(200, json=_load("opendota_hero_stats.json"))
        return httpx.Response(404)

    opendota = OpenDotaClient(transport=httpx.MockTransport(handler), sleep=_no_sleep)
    sources.use_clients(opendota=opendota, stratz=StratzClient(token=None))


@pytest.fixture
def server() -> FastMCP:
    mcp = FastMCP("test")
    tools.register(mcp)
    return mcp


async def _call(server: FastMCP, name: str, args: dict) -> dict:
    async with Client(server) as client:
        result = await client.call_tool(name, args, raise_on_error=False)
    return result.structured_content


DIVINE = {"rank_bracket": "divine+"}


# Analytics


def test_stratz_brackets_cover_rank_range():
    assert mu.stratz_brackets(Filters()) is None
    assert mu.stratz_brackets(Filters(rank_bracket="divine+")) == ("DIVINE_IMMORTAL",)
    assert mu.stratz_brackets(Filters(rank_bracket="archon-legend")) == (
        "CRUSADER_ARCHON",
        "LEGEND_ANCIENT",
    )
    divine = Filters(rank_bracket="divine+")
    assert mu.bracket_caveat(divine, ("DIVINE_IMMORTAL",)) is None
    legend = Filters(rank_bracket="legend")
    assert "legend-ancient" in mu.bracket_caveat(legend, mu.stratz_brackets(legend))


def test_advantages_match_their_formulas():
    pair = mu.Pair(hero=1, other=2, games=100, wins=60, base_hero=0.5, base_other=0.5)
    assert mu.matchup_advantage(pair) == pytest.approx(0.10)
    assert mu.synergy_advantage(pair) == pytest.approx(0.10)
    strong = mu.Pair(hero=1, other=2, games=100, wins=60, base_hero=0.6, base_other=0.6)
    # log5 against an equal hero expects 50%; as allies, 0.36 / (0.36 + 0.16) = 0.6923.
    assert mu.matchup_advantage(strong) == pytest.approx(0.10)
    assert mu.synergy_advantage(strong) == pytest.approx(0.6 - 0.36 / 0.52)


def test_against_flips_the_other_heroes_row():
    data = mu.MatchupData(source="stratz", brackets=None)
    data.vs[(2, 1)] = mu.Pair(hero=2, other=1, games=200, wins=80, base_hero=0.48, base_other=0.52)
    flipped = data.against(1, 2)
    assert (flipped.hero, flipped.games, flipped.wins) == (1, 200, 120)
    assert mu.matchup_advantage(flipped) == pytest.approx(-mu.matchup_advantage(data.vs[(2, 1)]))
    assert data.against(1, 3) is None


def test_parse_fixture_counts_games_and_pairs():
    data = mu.parse_stratz_matchups(_load("stratz_matchup_divine.json")["data"], None)
    assert set(data.games) == {AM, LION, AXE, INVOKER, CM, PUDGE, JUGG, SHAMAN, MARS, WD}
    raw = next(
        r
        for r in _load("stratz_matchup_divine.json")["data"]["heroStats"]["matchUp"]
        if r["heroId"] == AM
    )
    assert data.games[AM] == raw["matchCountVs"] // 5
    row = next(p for p in raw["vs"] if p["heroId2"] == AXE)
    pair = data.vs[(AM, AXE)]
    a, b = row["winRateHeroId1"], row["winRateHeroId2"]
    expected = row["winCount"] / row["matchCount"] - a * (1 - b) / (a * (1 - b) + b * (1 - a))
    assert mu.matchup_advantage(pair) == pytest.approx(expected)


# get_hero_matchups


async def test_hero_matchups_from_stratz(server, stratz_requests):
    out = await _call(server, "get_hero_matchups", {"hero": "am", "filters": DIVINE, "limit": 5})
    assert out["status"] == "ok"
    assert out["source"] == ["stratz"]
    assert any("stratz.com" in c for c in out["caveats"])
    result = out["result"]
    assert result["hero"] == "Anti-Mage"
    assert out["sample_size"] == result["games"]
    counters = [c["advantage"] for c in result["counters"]]
    assert counters == sorted(counters) and all(a < 0 for a in counters)
    allies = [c["advantage"] for c in result["best_allies"]]
    assert allies == sorted(allies, reverse=True) and all(a > 0 for a in allies)
    for lst in ("counters", "easiest_matchups", "best_allies", "worst_allies"):
        assert 0 < len(result[lst]) <= 5
        for entry in result[lst]:
            assert entry["games"] >= 100
            low, high = entry["win_rate_ci"]
            assert low <= entry["win_rate"] <= high
    assert "matchup_advantage" in out["definitions"]
    assert out["confidence"]
    matchup_call = next(r for r in stratz_requests if "matchUp" in r["query"])
    assert matchup_call["variables"] == {"heroIds": [AM], "brackets": ["DIVINE_IMMORTAL"]}


async def test_hero_matchups_other_position_and_own_position(server, stratz_requests):
    out = await _call(
        server,
        "get_hero_matchups",
        {
            "hero": "Anti-Mage",
            "mode": "counters",
            "other_position": 5,
            "filters": {**DIVINE, "position": 1},
        },
    )
    assert out["status"] == "ok"
    positions = mu.parse_positions(_load("stratz_positions_divine.json")["data"])
    supports = positions.heroes_at(5)
    listed = out["result"]["counters"] + out["result"]["easiest_matchups"]
    assert listed and all(e["hero_id"] in supports for e in listed)
    assert "best_allies" not in out["result"]
    assert out["result"]["at_position"]["position"] == 1
    assert out["result"]["at_position"]["share"] > 0.5


async def test_hero_matchups_insufficient_and_bad_input(server, stratz_requests):
    out = await _call(
        server, "get_hero_matchups", {"hero": "am", "filters": {**DIVINE, "min_sample": 10_000_000}}
    )
    assert out["status"] == "insufficient_data"
    assert out["hint"]

    out = await _call(server, "get_hero_matchups", {"hero": "zzzzzz"})
    assert out["status"] == "error"

    out = await _call(server, "get_hero_matchups", {"hero": "am", "filters": {"patch": "6.88"}})
    assert out["status"] == "error" and "patch" in out["message"]

    out = await _call(
        server, "get_hero_matchups", {"hero": "am", "filters": {"game_mode": "turbo"}}
    )
    assert out["status"] == "error"


async def test_hero_matchups_fall_back_to_opendota(server, opendota_only):
    out = await _call(server, "get_hero_matchups", {"hero": "am", "filters": {"min_sample": 50}})
    assert out["status"] == "ok"
    assert out["source"] == ["opendota"]
    assert "best_allies" not in out["result"]
    assert all(e["games"] >= 50 for e in out["result"]["counters"])
    assert any("OpenDota" in c for c in out["caveats"])

    # OpenDota has no rank split, so a rank filter cannot fall back.
    out = await _call(server, "get_hero_matchups", {"hero": "am", "filters": DIVINE})
    assert out["status"] == "error"


# find_hero_combos


async def test_duos_by_position(server, stratz_requests):
    out = await _call(
        server, "find_hero_combos", {"filters": DIVINE, "size": 2, "positions": [1, 5], "limit": 5}
    )
    assert out["status"] == "ok"
    positions = mu.parse_positions(_load("stratz_positions_divine.json")["data"])
    combos = out["result"]["combos"]
    scores = [c["synergy_advantage"] for c in combos]
    assert scores == sorted(scores, reverse=True)
    for c in combos:
        a, b = c["hero_ids"]
        assert positions.share(a, 1) >= mu.MIN_POSITION_SHARE
        assert positions.share(b, 5) >= mu.MIN_POSITION_SHARE
        assert c["games"] >= 100
    assert out["sample_size"] == min(c["min_pair_games"] for c in combos)


async def test_trios_are_sums_of_pairs(server, stratz_requests):
    out = await _call(server, "find_hero_combos", {"filters": DIVINE, "size": 3, "limit": 3})
    assert out["status"] == "ok"
    for c in out["result"]["combos"]:
        assert len(set(c["hero_ids"])) == 3
        assert c["synergy_advantage"] == pytest.approx(
            sum(p["synergy_advantage"] for p in c["pairs"]), abs=1e-3
        )
    assert any("three-hero" in c for c in out["caveats"])

    out = await _call(server, "find_hero_combos", {"size": 3, "positions": [1, 5]})
    assert out["status"] == "error"


# suggest_pick


async def test_suggest_pick(server, stratz_requests):
    out = await _call(
        server,
        "suggest_pick",
        {
            "allies": ["Lion", "Axe"],
            "enemies": ["Pudge", "Juggernaut"],
            "bans": ["Invoker"],
            "position": 2,
            "filters": DIVINE,
            "limit": 5,
        },
    )
    assert out["status"] == "ok"
    rows = out["result"]["suggestions"]
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    positions = mu.parse_positions(_load("stratz_positions_divine.json")["data"])
    for r in rows:
        assert r["hero_id"] not in {LION, AXE, PUDGE, JUGG, INVOKER}
        assert positions.share(r["hero_id"], 2) >= mu.MIN_POSITION_SHARE
        assert r["score"] == pytest.approx(
            r["matchup_advantage_sum"] + r["synergy_advantage_sum"], abs=1e-3
        )
        assert r["reasons"] and r["pairs_used"] + len(r["pairs_missing"]) == 4
    assert out["sample_size"] >= 100


async def test_suggest_pick_player_pool_and_errors(server, stratz_requests):
    out = await _call(
        server,
        "suggest_pick",
        {
            "allies": [],
            "enemies": ["Pudge"],
            "player_pool": ["Mars", "Invoker"],
            "filters": DIVINE,
        },
    )
    assert out["status"] == "ok"
    assert {r["hero_id"] for r in out["result"]["suggestions"]} <= {MARS, INVOKER}

    out = await _call(server, "suggest_pick", {"allies": ["Lion"], "enemies": ["Lion"]})
    assert out["status"] == "error"
    out = await _call(server, "suggest_pick", {"allies": [], "enemies": []})
    assert out["status"] == "error"


# analyze_draft


async def test_analyze_draft(server, stratz_requests):
    out = await _call(
        server,
        "analyze_draft",
        {
            "radiant": ["am", "lion", "axe", "invoker", "cm"],
            "dire": ["pudge", "jugg", "shadow shaman", "mars", "wd"],
            "filters": DIVINE,
        },
    )
    assert out["status"] == "ok"
    result = out["result"]
    parts = result["estimate_components"]
    assert result["radiant_win_estimate"] == pytest.approx(0.5 + sum(parts.values()), abs=1e-3)
    assert 0.05 <= result["radiant_win_estimate"] <= 0.95
    assert len(result["radiant"]) == len(result["dire"]) == 5
    assert len(result["pairwise"]["matchups"]) == 25
    assert result["pairs_used"] <= result["pairs_possible"] == 45
    # Each cross pair counts for the Radiant hero and against the Dire hero.
    rad = sum(h["matchup_advantage_sum"] for h in result["radiant"])
    dire = sum(h["matchup_advantage_sum"] for h in result["dire"])
    assert rad == pytest.approx(-dire, abs=1e-3)
    assert isinstance(result["weaknesses"]["radiant"], list)
    assert any("heuristic" in c and "not a calibrated model" in c for c in out["caveats"])
    assert out["sample_size"] >= 100


def test_draft_weaknesses_report_counters_and_missing_positions():
    positions = mu.PositionStats(games={1: {1: 100}, 2: {1: 100}}, wins={1: {1: 50}, 2: {1: 50}})
    cross = [(1, 9, -0.05, 500)]
    notes = mu.draft_weaknesses([1, 2], cross, [(1, 2, -0.03, 300)], positions, "radiant")
    assert any("countered by" in n for n in notes)
    assert any("win less together" in n for n in notes)
    assert sum("commonly played at position" in n for n in notes) == 4

import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import matches as am
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.sources.opendota import OpenDotaClient

FIXTURES = Path(__file__).parent / "fixtures" / "matches"
PARSED_ID = 9073999945
UNPARSED_ID = 9075999981


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


PARSED = _load("match_9073999945_parsed.json")
UNPARSED = _load("match_9075999981_unparsed.json")


async def _no_sleep(_: float) -> None:
    return None


class FakeOpenDota:
    """Serves the recorded matches and records every request."""

    def __init__(self, *, parse_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.parse_status = parse_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.startswith("/api/request/"):
            if self.parse_status != 200:
                return httpx.Response(self.parse_status)
            return httpx.Response(200, json={"job": {"jobId": 1}})
        if path == f"/api/matches/{PARSED_ID}":
            return httpx.Response(200, json=PARSED)
        if path == f"/api/matches/{UNPARSED_ID}":
            return httpx.Response(200, json=UNPARSED)
        return httpx.Response(404, json={"error": "Not Found"})

    def count(self, method: str) -> int:
        return sum(1 for r in self.requests if r.method == method)


@pytest.fixture
def fake():
    clear_all()
    api = FakeOpenDota()
    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None,
            transport=httpx.MockTransport(api.handler),
            sleep=_no_sleep,
            max_retries=0,
        )
    )
    yield api
    sources.use_clients()
    clear_all()


async def call(tool: str, **args) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
    return result.structured_content


# analyze_match


async def test_analyze_parsed_match(fake):
    out = await call("analyze_match", match_id=PARSED_ID)
    assert out["status"] == "ok"
    assert out["sample_size"] == 1
    assert out["source"] == ["opendota"]
    r = out["result"]
    assert r["winner"] == "radiant"
    assert r["duration"] == "65:00"
    assert r["parsed"] is True
    assert len(r["players"]) == 10
    assert len(r["draft"]) == len(PARSED["picks_bans"])
    sven = r["players"][0]
    assert sven["hero"] == "Sven"
    assert sven["gpm"] == PARSED["players"][0]["gold_per_min"]
    assert sven["net_worth"] == 47572
    swing = r["gold_swing"]
    assert swing["max_radiant_lead"] == {"gold": 10028, "minute": 41}
    assert swing["max_dire_lead"] == {"gold": 19321, "minute": 56}
    assert swing["winner_max_deficit"] == 19321
    assert swing["comeback"] is True
    overall = r["biggest_contributors"]["overall"]
    assert len(overall) == 3
    assert overall[0]["score"] >= overall[1]["score"] >= overall[2]["score"]
    assert r["biggest_contributors"]["radiant"]["side"] == "radiant"
    assert "contribution score" in out["definitions"]
    assert fake.count("POST") == 0


async def test_analyze_unparsed_match_returns_partial_and_requests_parse(fake):
    out = await call("analyze_match", match_id=UNPARSED_ID)
    assert out["status"] == "ok"
    r = out["result"]
    assert r["parsed"] is False
    assert r["winner"] == "dire"
    assert r["gold_swing"] is None
    assert all(p["stuns_s"] is None for p in r["players"])
    assert r["players"][0]["kills"] == UNPARSED["players"][0]["kills"]
    assert "parse was requested" in out["caveats"][0]
    assert fake.count("POST") == 1


# parsed-only tools on an unparsed match


@pytest.mark.parametrize(
    "tool",
    [
        "get_match_timeline",
        "find_turning_points",
        "analyze_teamfights",
        "analyze_vision",
        "analyze_laning_phase",
    ],
)
async def test_parsed_only_tools_request_parse(fake, tool):
    out = await call(tool, match_id=UNPARSED_ID)
    assert out["status"] == "insufficient_data"
    assert out["result"] is None
    assert "parse was requested" in out["message"]
    assert "about a minute" in out["hint"]
    assert "dire won in 30:01" in out["caveats"][0]
    assert fake.count("POST") == 1


async def test_parse_is_requested_once_per_window_but_match_is_refetched(fake):
    await call("get_match_timeline", match_id=UNPARSED_ID)
    await call("analyze_vision", match_id=UNPARSED_ID)
    assert fake.count("POST") == 1
    assert fake.count("GET") == 2


async def test_parse_request_failure_is_reported(fake):
    fake.parse_status = 400
    out = await call("analyze_teamfights", match_id=UNPARSED_ID)
    assert out["status"] == "insufficient_data"
    assert "parse request failed" in out["message"]


async def test_parsed_match_is_cached(fake):
    await call("analyze_match", match_id=PARSED_ID)
    await call("analyze_vision", match_id=PARSED_ID)
    assert fake.count("GET") == 1


async def test_unknown_match_is_an_error(fake):
    out = await call("analyze_match", match_id=1)
    assert out["status"] == "error"
    assert "no match 1" in out["message"]


# parsed tools


async def test_timeline(fake):
    out = await call("get_match_timeline", match_id=PARSED_ID)
    assert out["status"] == "ok"
    r = out["result"]
    adv = r["advantage_by_minute"]
    assert len(adv) == len(PARSED["radiant_gold_adv"])
    assert adv[1] == {"minute": 1, "gold_adv": -404, "xp_adv": PARSED["radiant_xp_adv"][1]}
    first = r["events"][0]
    assert first == {
        "type": "first_blood",
        "side": "dire",
        "hero": "Pudge",
        "victim": "Warlock",
        "time_s": 50,
        "time": "0:50",
    }
    types = {e["type"] for e in r["events"]}
    assert {"tower", "barracks", "ancient", "roshan", "aegis", "tormentor", "buyback"} <= types
    tormentor = next(e for e in r["events"] if e["type"] == "tormentor")
    assert tormentor["side"] == "dire" and tormentor["time"] == "29:14"
    times = [e["time_s"] for e in r["events"]]
    assert times == sorted(times)
    sven = r["item_purchases"][0]
    assert sven["hero"] == "Sven"
    assert sven["purchases"][0] == {"time": "-1:29", "time_s": -89, "item": "Quelling Blade"}
    assert all(p["item"] != "Tango" for p in sven["purchases"])


async def test_timeline_with_consumables(fake):
    out = await call("get_match_timeline", match_id=PARSED_ID, include_consumables=True)
    items = [p["item"] for p in out["result"]["item_purchases"][0]["purchases"]]
    assert "Tango" in items


async def test_turning_points(fake):
    out = await call("find_turning_points", match_id=PARSED_ID)
    assert out["status"] == "ok"
    points = out["result"]["turning_points"]
    assert len(points) == 5
    assert [p["from_minute"] for p in points] == sorted(p["from_minute"] for p in points)
    biggest = max(points, key=lambda p: abs(p["gold_adv_change_radiant"]))
    assert biggest["from_minute"] == 51
    assert biggest["gold_adv_change_radiant"] == -13490
    assert biggest["benefited"] == "dire"
    assert [f["fight"] for f in biggest["teamfights"]] == [14]
    radiant_push = points[0]
    assert (radiant_push["from_minute"], radiant_push["gold_adv_change_radiant"]) == (38, 10953)
    assert any(o["type"] == "barracks" for o in radiant_push["objectives"])
    starts = [p["from_minute"] for p in points]
    assert all(b - a >= 3 for a, b in zip(starts, starts[1:], strict=False))


async def test_teamfights(fake):
    out = await call("analyze_teamfights", match_id=PARSED_ID)
    r = out["result"]
    assert r["fight_count"] == len(PARSED["teamfights"]) == 19
    assert r["fights_won"] == {"radiant": 9, "dire": 10}
    first = r["teamfights"][0]
    assert first["start"] == "2:47"
    assert first["deaths_by_side"] == {"radiant": 2, "dire": 1}
    assert first["gold_swing_radiant"] == -255
    assert first["winner"] == "dire"
    assert first["participants"][0]["hero"] == "Sven"
    assert first["participants"][0]["damage"] == 132
    assert any("stuns per teamfight" in c for c in out["caveats"])


async def test_vision(fake):
    out = await call("analyze_vision", match_id=PARSED_ID)
    r = out["result"]
    lion = r["players"][2]
    assert lion["hero"] == "Lion"
    assert lion["observers"]["placed"] == 22
    assert lion["observers"]["destroyed_by_enemy"] == 8
    assert lion["dewards"] == {"observers": 4, "sentries": 6}
    assert r["teams"]["radiant"]["observers_placed"] == 29
    assert r["match_average_per_player"]["observers_placed"] == 5.7
    assert lion["vs_match_average"]["observers_placed"] == 16.3


async def test_laning(fake):
    out = await call("analyze_laning_phase", match_id=PARSED_ID)
    r = out["result"]
    sven = r["players"][0]
    assert sven["lane"] == "bottom" and sven["lane_role"] == "safe"
    assert sven["last_hits_10"] == 69 and sven["denies_10"] == 9
    top = next(lane for lane in r["lanes"] if lane["lane"] == "top")
    assert top["radiant"]["heroes"] == ["Warlock", "Dark Seer"]
    assert top["outcome"] == "dire_won"
    assert all(k["time_s"] <= 600 for k in r["early_kills"])
    first = r["early_kills"][0]
    assert first["killer"] == "Pudge" and first["victim"] == "Warlock"
    assert first["rotation"] is True


# pure helpers


def test_clock():
    assert am.clock(-89) == "-1:29"
    assert am.clock(3900) == "65:00"
    assert am.clock(None) is None


def test_contribution_shares_add_up_per_team():
    ranked = am.contributors(am.player_lines(PARSED))
    for side in ("radiant", "dire"):
        mine = [c for c in ranked if c["side"] == side]
        assert sum(c["shares"]["hero_damage"] for c in mine) == pytest.approx(1, abs=0.01)
        assert sum(c["shares"]["net_worth"] for c in mine) == pytest.approx(1, abs=0.01)


def test_no_comeback_when_winner_never_trailed():
    raw = {"radiant_win": True, "radiant_gold_adv": [0, 500, 3000, 8000]}
    swing = am.advantage_swing(raw)
    assert swing["winner_max_deficit"] == 0
    assert swing["comeback"] is False


def test_is_parsed():
    assert am.is_parsed(PARSED)
    assert not am.is_parsed(UNPARSED)

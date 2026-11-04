import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import players as analytics
from dota_insights.analytics.players import PlayerInputError, parse_player
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "players"
GORGC = 56939869  # public pro player (Gorgc)
YATORO = 321580662  # public pro player (Yatoro)
PRIVATE = 2  # OpenDota profile with fh_unavailable and no public matches
UNKNOWN = 1999999999


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


def _player_files(account_id: int) -> str | None:
    return {GORGC: "gorgc", YATORO: "yatoro"}.get(account_id)


def opendota_handler(calls: list[httpx.Request]):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        parts = request.url.path.removeprefix("/api").strip("/").split("/")
        params = request.url.params
        if parts == ["heroStats"]:
            return httpx.Response(200, json=load("herostats.json"))
        if parts == ["benchmarks"]:
            name = f"benchmarks_{params['hero_id']}_{params.get('bracket')}.json"
            if not (FIXTURES / name).exists():
                return httpx.Response(404, json={"error": "Not Found"})
            return httpx.Response(200, json=load(name))
        if parts[0] != "players":
            return httpx.Response(404, json={"error": "Not Found"})
        account_id = int(parts[1])
        prefix = _player_files(account_id)
        if account_id == PRIVATE:
            if len(parts) == 2:
                return httpx.Response(200, json=load("private_player.json"))
            if parts[2] == "wl":
                return httpx.Response(200, json={"win": 0, "lose": 0})
            return httpx.Response(200, json=[])
        if prefix is None:
            return httpx.Response(404, json={"error": "Not Found"})
        if len(parts) == 2:
            return httpx.Response(200, json=load(f"{prefix}_player.json"))
        if parts[2] == "matches":
            rows = load(f"{prefix}_matches.json")
            if "hero_id" in params:
                rows = [r for r in rows if r["hero_id"] == int(params["hero_id"])]
            return httpx.Response(200, json=rows[: int(params.get("limit", len(rows)))])
        name = {"recentMatches": "recent"}.get(parts[2], parts[2])
        return httpx.Response(200, json=load(f"{prefix}_{name}.json"))

    return handle


def stratz_handler(calls: list[dict], fail: bool = False):
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["variables"])
        if fail:
            return httpx.Response(503)
        account_id = body["variables"]["id"]
        wanted = set(body["variables"]["matchIds"])
        pages = load(f"{_player_files(account_id)}_stratz.json")
        matches = [m for page in pages for m in page["player"]["matches"] if m["id"] in wanted]
        data = {"player": {"steamAccountId": account_id, "matches": matches}}
        return httpx.Response(200, json={"data": data})

    return handle


@pytest.fixture
def api():
    calls = {"opendota": [], "stratz": [], "stratz_fail": False}

    def install(stratz_fail: bool = False):
        opendota = OpenDotaClient(
            api_key=None,
            transport=httpx.MockTransport(opendota_handler(calls["opendota"])),
            requests_per_minute=1e6,
            burst=1000,
            sleep=_no_sleep,
        )
        stratz = StratzClient(
            token="t",
            transport=httpx.MockTransport(stratz_handler(calls["stratz"], stratz_fail)),
            requests_per_minute=1e6,
            burst=1000,
            sleep=_no_sleep,
            max_retries=0,
        )
        sources.use_clients(opendota=opendota, stratz=stratz)

    clear_all()
    install()
    calls["reinstall"] = install
    yield calls
    clear_all()
    sources.use_clients()


async def call(tool: str, **args) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return result.structured_content


def od_paths(calls) -> list[str]:
    return [r.url.path.removeprefix("/api") for r in calls["opendota"]]


# Player references


@pytest.mark.parametrize(
    "text",
    [
        "56939869",
        56939869,
        "76561198017205597",
        "https://steamcommunity.com/profiles/76561198017205597/",
        "https://www.opendota.com/players/56939869/overview",
        "https://stratz.com/players/56939869",
        "https://www.dotabuff.com/players/56939869/matches",
        "STEAM_0:1:28469934",
        "[U:1:56939869]",
    ],
)
def test_parse_player_forms(text):
    assert parse_player(text) == GORGC


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("https://steamcommunity.com/id/akashiedein/", "Custom Steam URL"),
        ("103582791429521408", "not the SteamID64 of an individual"),  # a Steam group
        ("4294967295", "anonymous"),
        ("0", "anonymous"),
        ("Gorgc", "not an account ID"),
        ("", "No player"),
    ],
)
def test_parse_player_rejects(text, message):
    with pytest.raises(PlayerInputError, match=message):
        parse_player(text)


# Pure helpers


def test_percentile_of_interpolates_and_clamps():
    table = [
        {"percentile": 0.1, "value": 100},
        {"percentile": 0.5, "value": 300},
        {"percentile": 0.9, "value": 500},
    ]
    assert analytics.percentile_of(table, 200) == pytest.approx(0.3)
    assert analytics.percentile_of(table, 50) == 0.1
    assert analytics.percentile_of(table, 900) == 0.9
    assert analytics.percentile_of([{"percentile": 0.5, "value": None}], 1) is None


def _m(win: bool) -> analytics.PlayerMatch:
    row = {"match_id": 1, "player_slot": 0, "radiant_win": win, "hero_id": 1, "start_time": 0}
    return analytics.match_from_opendota(row)


def test_streaks():
    seq = [_m(w) for w in (True, True, False, True, True, True, False, False)]
    assert analytics.streaks(seq) == {
        "longest_win_streak": 3,
        "longest_loss_streak": 2,
        "current_streak": {"kind": "loss", "length": 2},
    }


def test_stratz_position_and_xp():
    match = _m(True)
    detail = {
        "players": [
            {
                "position": None,
                "lane": "OFF_LANE",
                "role": "LIGHT_SUPPORT",
                "stats": {"networthPerMinute": [600, 800], "experiencePerMinute": [100, 200]},
            }
        ]
    }
    out = analytics.apply_stratz(match, detail)
    assert out.position == 4 and out.position_inferred
    assert out.xp_by_minute == (0, 100, 300)
    assert out.networth_by_minute == (600, 800)


def test_medal_name():
    assert analytics.medal_name(80) == "Immortal"
    assert analytics.medal_name(75) == "Divine 5"
    assert analytics.medal_name(None) is None


# Tools


async def test_profile(api):
    out = await call("get_player_profile", player="https://www.dotabuff.com/players/56939869")
    assert out["status"] == "ok"
    res = out["result"]
    wl = load("gorgc_wl.json")
    assert res["player"]["pro_name"] == "Gorgc"
    assert res["player"]["medal"] == "Immortal"
    assert res["player"]["leaderboard_rank"] == load("gorgc_player.json")["leaderboard_rank"]
    assert res["overall"]["matches"] == out["sample_size"] == wl["win"] + wl["lose"]
    low, high = res["overall"]["win_rate_ci"]
    assert low < res["overall"]["win_rate"] < high
    recent = load("gorgc_recent.json")
    assert len(res["last_20"]["games"]) == res["last_20"]["matches"] == len(recent)
    assert res["last_20"]["wins"] == sum(
        (r["player_slot"] < 128) == r["radiant_win"] for r in recent
    )
    assert out["source"] == ["opendota"]


async def test_private_and_unknown_players(api):
    out = await call("get_player_profile", player=str(PRIVATE))
    assert out["status"] == "error"
    assert "private" in out["message"]
    out = await call("get_player_stats", player=str(PRIVATE))
    assert out["status"] == "error" and "private" in out["message"]
    out = await call("get_player_profile", player=str(UNKNOWN))
    assert out["status"] == "error" and "no player" in out["message"]
    out = await call("get_player_profile", player="steamcommunity.com/id/someone")
    assert out["status"] == "error" and out["hint"]


async def test_stats(api):
    out = await call("get_player_stats", player=str(GORGC), matches=100)
    assert out["status"] == "ok"
    rows = load("gorgc_matches.json")[:100]
    res = out["result"]
    assert out["sample_size"] == res["matches"] == 100
    assert res["wins"] == sum((r["player_slot"] < 128) == r["radiant_win"] for r in rows)
    assert res["averages"]["gpm"] == round(sum(r["gold_per_min"] for r in rows) / 100, 2)
    low, high = out["confidence"]["avg_gpm"]
    assert low <= res["averages"]["gpm"] <= high
    assert out["period"]["to_date"] >= out["period"]["from_date"]
    assert "avg_kda" in out["definitions"]
    assert api["stratz"] == []  # no STRATZ call without a position filter


async def test_stats_hero_and_position_filters(api):
    out = await call("get_player_stats", player=str(GORGC), hero="necro", min_sample=5)
    assert out["status"] == "ok"
    # OpenDota applies hero_id before limit: the last 100 Necrophos matches.
    assert out["sample_size"] == sum(r["hero_id"] == 36 for r in load("gorgc_matches.json"))
    matches = [r for r in api["opendota"] if r.url.path.endswith("/matches")]
    assert matches[-1].url.params["hero_id"] == "36"

    out = await call("get_player_stats", player=str(GORGC), position="pos1", min_sample=5)
    assert out["status"] == "ok"
    assert out["filters"]["position"] == 1
    assert "stratz" in out["source"]
    assert any("position 1" in c for c in out["caveats"])
    assert out["sample_size"] < 100


async def test_stats_insufficient_and_unknown_hero(api):
    out = await call("get_player_stats", player=str(GORGC), matches=20, min_sample=50)
    assert out["status"] == "insufficient_data"
    assert out["sample_size"] == 20 and out["hint"]
    out = await call("get_player_stats", player=str(GORGC), hero="zzzzzz")
    assert out["status"] == "error"


async def test_stratz_outage(api):
    api["reinstall"](stratz_fail=True)
    out = await call("find_player_weaknesses", player=str(GORGC))
    assert out["status"] == "ok"
    assert out["result"]["phases"] is None
    assert any("STRATZ was unavailable" in c for c in out["caveats"])
    out = await call("get_player_stats", player=str(GORGC), position=2)
    assert out["status"] == "error" and "STRATZ" in out["message"]


async def test_hero_pool(api):
    out = await call("get_player_hero_pool", player=str(GORGC), matches=200)
    assert out["status"] == "ok"
    res = out["result"]
    assert res["benchmark_bracket"] == "Divine"
    assert any("Immortal" in c for c in out["caveats"])
    bench_calls = [r for r in api["opendota"] if r.url.path.endswith("/benchmarks")]
    assert len(bench_calls) == analytics.MAX_BENCHMARK_HEROES
    assert {r.url.params["bracket"] for r in bench_calls} == {"7"}
    heroes = {h["hero"]: h for h in res["heroes"]}
    assert sum(h["matches"] for h in res["heroes"]) == 200
    necro = heroes["Necrophos"]
    assert necro["matches"] == 20
    row = next(r for r in load("herostats.json") if r["id"] == 36)
    assert necro["rank_win_rate"] == round(row["7_win"] / row["7_pick"], 4)
    assert 0.1 <= necro["performance_index"] <= 0.99
    assert "win_rate_q_value" in necro
    for entry in res["overperforms"] + res["underperforms"]:
        assert heroes[entry["hero"]]["matches"] >= 5 and entry["reasons"]
    assert "win_rate[Necrophos]" in out["confidence"]


async def test_weaknesses(api):
    out = await call("find_player_weaknesses", player=str(GORGC), matches=200)
    assert out["status"] == "ok"
    res = out["result"]
    assert "stratz" in out["source"]
    assert [r["median_percentile"] for r in res["metrics_vs_rank"]] == sorted(
        r["median_percentile"] for r in res["metrics_vs_rank"]
    )
    phases = res["phases"]
    assert phases["parsed_matches"] > 100
    ten = phases["checkpoints"][0]
    assert ten["minute"] == 10
    assert ten["networth"]["wins_n"] + ten["networth"]["losses_n"] == phases["parsed_matches"]
    assert ten["networth"]["gap_ci"][0] <= ten["networth"]["gap"] <= ten["networth"]["gap_ci"][1]
    assert phases["networth_diverges_most_in"] in analytics.PHASES.values()
    traits = {t["trait"] for t in res["loss_traits"]}
    assert {"dire side", "playing in a party"} <= traits
    assert any(t.startswith("position ") for t in traits)
    assert all(0 <= t["q_value"] <= 1 for t in res["loss_traits"])
    assert res["loss_profile"]["losses"]["deaths"] > res["loss_profile"]["wins"]["deaths"]


async def test_trends(api):
    out = await call("get_player_trends", player=str(GORGC), matches=100, bucket_size=20)
    assert out["status"] == "ok"
    res = out["result"]
    assert len(res["buckets"]) == 5
    assert sum(b["matches"] for b in res["buckets"]) == 100
    assert res["buckets"][0]["from"] <= res["buckets"][-1]["to"]
    rows = sorted(load("gorgc_matches.json")[:100], key=lambda r: r["start_time"])
    results = [(r["player_slot"] < 128) == r["radiant_win"] for r in rows]
    longest, run = 0, 0
    for won in results:
        run = run + 1 if won else 0
        longest = max(longest, run)
    assert res["streaks"]["longest_win_streak"] == longest
    assert res["streaks"]["current_streak"]["kind"] == ("win" if results[-1] else "loss")
    assert set(res["change_per_10_matches"]) == set(analytics.TREND_METRICS)


async def test_peers(api):
    out = await call("get_player_peers", player=str(GORGC), min_games=5, top=5)
    assert out["status"] == "ok"
    res = out["result"]
    peers = load("gorgc_peers.json")
    expected = sorted(
        (p for p in peers if p["with_games"] >= 5),
        key=lambda p: (-p["with_games"], p["account_id"]),
    )
    assert [t["account_id"] for t in res["teammates"]] == [p["account_id"] for p in expected[:5]]
    first = res["teammates"][0]
    assert first["win_rate"] == round(expected[0]["with_win"] / expected[0]["with_games"], 4)
    assert all(o["matches"] >= 5 for o in res["opponents"])
    peer_call = next(r for r in api["opendota"] if r.url.path.endswith("/peers"))
    assert peer_call.url.params["limit"] == "200"


async def test_compare_players(api):
    out = await call(
        "compare_players",
        player_a=str(GORGC),
        player_b="https://www.opendota.com/players/321580662",
        matches=200,
    )
    assert out["status"] == "ok"
    res = out["result"]
    assert res["player_a"]["player"]["pro_name"] == "Gorgc"
    assert res["player_b"]["player"]["pro_name"] == "Yatoro"
    assert out["sample_size"] == 400
    for hero in res["shared_heroes"]:
        assert hero["player_a"]["matches"] >= 3 and hero["player_b"]["matches"] >= 3
    assert {p["position"] for p in res["positions"]} >= {1}
    shares = sum(p["player_b"]["share"] for p in res["positions"])
    assert shares <= 1.001
    out = await call("compare_players", player_a=str(GORGC), player_b="56939869")
    assert out["status"] == "error"

import json
import re
from collections import Counter
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import pro
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "pro"


def fixture_name(request: httpx.Request) -> str:
    """The recorded fixture file for a source request (time-dependent variables left out)."""
    if request.url.host == "api.stratz.com":
        body = json.loads(request.content)
        document, variables = body["query"], body.get("variables") or {}
        operation = re.search(r"query (\w+)", document).group(1)
        ids = re.findall(r"match\(id: (\d+)\)", document)
        if ids:
            return f"stratz_{operation}_{ids[0]}_{len(ids)}.json"
        parts = [str(v) for k, v in sorted(variables.items()) if k != "since"]
        return f"stratz_{operation}_{'_'.join(parts)}.json"
    path = request.url.path.removeprefix("/api/").strip("/").replace("/", "_")
    if path == "explorer":
        numbers = re.findall(r"= (\d+)", request.url.params["sql"])
        return f"opendota_explorer_{'_'.join(numbers)}.json"
    return f"opendota_{path}.json"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def _no_sleep(_: float) -> None:
    return None


class Replay:
    """httpx handler that answers from fixtures/pro and records which files were used."""

    def __init__(self, overrides: dict[str, httpx.Response] | None = None) -> None:
        self.overrides = overrides or {}
        self.used: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        name = fixture_name(request)
        self.used.append(name)
        if name in self.overrides:
            return self.overrides[name]
        path = FIXTURES / name
        if not path.exists():
            return httpx.Response(404, json={"error": f"no fixture {name}"})
        return httpx.Response(200, content=path.read_bytes())


def install(replay: Replay, *, stratz_token: str | None = "t") -> None:
    transport = httpx.MockTransport(replay)
    sources.use_clients(
        opendota=OpenDotaClient(
            transport=transport, sleep=_no_sleep, max_retries=0, requests_per_minute=1e6
        ),
        stratz=StratzClient(
            token=stratz_token, transport=transport, sleep=_no_sleep, requests_per_minute=1e6
        ),
    )


@pytest.fixture
def replay():
    clear_all()
    handler = Replay()
    install(handler)
    yield handler
    sources.use_clients()
    clear_all()


async def call(name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args, raise_on_error=False)
    return result.structured_content


# Name lookup


def test_fuzzy_lookup_exact_namesakes_ranked_and_reported():
    records = [
        {"name": "Team Spirit", "seen": 10},
        {"name": "Team. Spirit", "seen": 5},
        {"name": "Spirit Academy", "seen": 20},
    ]
    found = pro.fuzzy_lookup(
        "team spirit", records, keys=lambda r: ((r["name"], 1.0),), rank=lambda r: r["seen"]
    )
    assert found.best is records[0]
    assert found.namesakes == (records[1],)


def test_fuzzy_lookup_close_partial_matches_are_ambiguous():
    records = [{"name": "Aurora Gaming"}, {"name": "Aurora Esports"}]
    found = pro.fuzzy_lookup("aurora", records, keys=lambda r: ((r["name"], 1.0),), rank=len)
    assert found.best is None
    assert len(found.candidates) == 2
    nothing = pro.fuzzy_lookup("zzzz", records, keys=lambda r: ((r["name"], 1.0),), rank=len)
    assert nothing.best is None
    assert nothing.candidates == ()


def test_fuzzy_lookup_name_beats_tag():
    records = [
        {"name": "OG", "tag": "OG"},
        {"name": "Oshi hi teo Gaming", "tag": "OG"},
    ]
    found = pro.fuzzy_lookup(
        "og",
        records,
        keys=lambda r: ((r["name"], 1.0), (r["tag"], pro.TAG_FACTOR)),
        rank=lambda r: 0,
    )
    assert found.best is records[0]
    assert found.namesakes == ()


def test_stratz_draft_parsing_and_first_pick():
    raw = {
        "id": 1,
        "startDateTime": 100,
        "durationSeconds": 1800,
        "didRadiantWin": True,
        "radiantTeamId": 10,
        "direTeamId": 20,
        "pickBans": [
            {"isPick": False, "heroId": 5, "bannedHeroId": 5, "order": 0, "isRadiant": True},
            {"isPick": True, "heroId": 1, "bannedHeroId": None, "order": 1, "isRadiant": False},
            {"isPick": True, "heroId": 2, "bannedHeroId": None, "order": 2, "isRadiant": True},
        ],
        "players": [
            {"isRadiant": True, "heroId": 2, "kills": 3},
            {"isRadiant": False, "heroId": 1, "kills": 4},
        ],
    }
    match = pro.match_from_stratz(raw)
    assert match.heroes(True) == [2]
    assert match.bans(True) == [5]
    assert match.first_pick_radiant() is False
    assert (match.radiant_score, match.dire_score) == (3, 4)
    assert match.side_of(20) is False
    assert match.side_of(99) is None


# get_pro_matches


async def test_pro_matches_recent_fills_drafts_after_stratz_timeout(replay):
    out = await call("get_pro_matches", {"limit": 5})
    assert out["status"] == "ok"
    rows = out["result"]
    expected = sorted(load("opendota_proMatches.json"), key=lambda r: -r["start_time"])[:5]
    assert [r["match_id"] for r in rows] == [r["match_id"] for r in expected]
    # The batched STRATZ query timed out on three matches, so each was fetched alone.
    assert "stratz_ProDrafts_9076183783_5.json" in replay.used
    assert "stratz_ProDrafts_9076056982_1.json" in replay.used
    og = next(r for r in rows if r["match_id"] == 9076056982)
    assert og["winner"] == "GamerLegion"
    assert "Outworld Devourer" in og["draft"]["radiant_picks"]
    assert len(og["draft"]["radiant_bans"]) == 7
    assert "stratz" in out["source"]


async def test_pro_matches_by_league_uses_explorer_drafts(replay):
    out = await call("get_pro_matches", {"league": "EPL Masters 2026", "limit": 3})
    assert out["status"] == "ok"
    assert {r["league_id"] for r in out["result"]} == {19944}
    assert out["result"][0]["draft"]["first_pick"] in ("radiant", "dire")
    assert not any(name.startswith("stratz_") for name in replay.used)


async def test_pro_matches_by_team_has_scores_from_stratz(replay):
    out = await call("get_pro_matches", {"team": "OG", "limit": 2, "include_drafts": False})
    assert out["status"] == "ok"
    first = out["result"][0]
    assert first["radiant"]["team"] == "OG"
    assert first["radiant"]["score"] == 26
    assert first["dire"]["score"] == 60
    assert "draft" not in first
    assert any("stratz.com" in c for c in out["caveats"])


async def test_pro_matches_unknown_league_errors(replay):
    out = await call("get_pro_matches", {"league": "qqqzzzxxx"})
    assert out["status"] == "error"
    assert "No league" in out["message"]


# get_team_stats


async def test_team_stats_matches_fixture_counts(replay):
    out = await call("get_team_stats", {"team": "Team Spirit"})
    assert out["status"] == "ok"
    result = out["result"]
    raw = load("stratz_ProTeamMatches_0_100_7119388.json")["data"]["team"]["matches"]
    wins = sum(1 for m in raw if m["didRadiantWin"] == (m["radiantTeamId"] == 7119388))
    assert result["team_id"] == 7119388
    assert result["matches"] == len(raw) == out["sample_size"]
    assert result["wins"] == wins
    assert result["win_rate"] == round(wins / len(raw), 4)
    low, high = out["confidence"]["win_rate"]
    assert low < result["win_rate"] < high
    banned = Counter(
        p["bannedHeroId"]
        for m in raw
        for p in m["pickBans"] or []
        if not p["isPick"] and p["isRadiant"] == (m["radiantTeamId"] == 7119388)
    )
    top_hero, top_bans = banned.most_common(1)[0]
    assert result["most_banned_by_team"][0] == {"hero": pro.hero_name(top_hero), "bans": top_bans}
    assert 0 <= result["first_pick_share"] <= 1
    assert out["filters"]["min_sample"] == 10
    assert any("Team. Spirit" in c for c in out["caveats"])
    assert "stratz" in out["source"]


async def test_team_stats_ambiguous_name_lists_candidates(replay):
    out = await call("get_team_stats", {"team": "aurora"})
    assert out["status"] == "error"
    assert "Aurora Gaming" in out["message"]
    assert "Aurora.1xBet" in out["message"]


async def test_team_stats_below_min_matches_is_insufficient(replay):
    out = await call("get_team_stats", {"team": "Team Spirit", "min_matches": 200})
    assert out["status"] == "insufficient_data"
    assert out["result"] is None
    assert out["hint"]


async def test_team_stats_without_stratz_token_errors():
    clear_all()
    install(Replay(), stratz_token=None)
    try:
        out = await call("get_team_stats", {"team": "Team Spirit"})
    finally:
        sources.use_clients()
        clear_all()
    assert out["status"] == "error"
    assert "STRATZ" in out["message"]


# get_head_to_head


async def test_head_to_head_record(replay):
    out = await call("get_head_to_head", {"team_a": "OG", "team_b": "GamerLegion", "limit": 2})
    assert out["status"] == "ok"
    result = out["result"]
    rows = load("opendota_explorer_2586976_9964962_9964962_2586976.json")["rows"]
    og_wins = sum(1 for r in rows if r["radiant_win"] == (r["radiant_team_id"] == 2586976))
    assert result["meetings"] == len(rows)
    assert result["team_a_wins"] == og_wins
    assert result["team_a_wins"] + result["team_b_wins"] == result["meetings"]
    assert sum(lg["meetings"] for lg in result["by_league"]) == result["meetings"]
    assert len(result["recent_meetings"]) == 2
    assert result["recent_meetings"][0]["match_id"] == max(r["match_id"] for r in rows)


async def test_head_to_head_errors(replay):
    unknown = await call("get_head_to_head", {"team_a": "zzqqxx", "team_b": "OG"})
    assert unknown["status"] == "error"
    same = await call("get_head_to_head", {"team_a": "OG", "team_b": "2586976"})
    assert same["status"] == "error"
    assert "same team" in same["message"]


async def test_head_to_head_explorer_failure_is_an_error():
    clear_all()
    name = "opendota_explorer_2586976_9964962_9964962_2586976.json"
    install(Replay({name: httpx.Response(200, json={"err": "Query read timeout"})}))
    try:
        out = await call("get_head_to_head", {"team_a": "OG", "team_b": "GamerLegion"})
    finally:
        sources.use_clients()
        clear_all()
    assert out["status"] == "error"
    assert "timeout" in out["message"]


# get_tournament_stats


async def test_tournament_stats(replay):
    out = await call("get_tournament_stats", {"league": "EPL Masters"})
    assert out["status"] == "ok"
    result = out["result"]
    rows = load("opendota_explorer_19944.json")["rows"]
    assert result["league_id"] == 19944
    assert result["matches"] == len(rows) == out["sample_size"]

    top = result["most_contested"][0]
    hero_id = next(h.id for h in pro.hero_by_id().values() if h.display_name == top["hero"])
    picks = bans = wins = 0
    for r in rows:
        for p in r["picks_bans"]:
            if p["hero_id"] != hero_id:
                continue
            if p["is_pick"]:
                picks += 1
                wins += (p["team"] == 0) == r["radiant_win"]
            else:
                bans += 1
    assert (top["picks"], top["bans"], top["wins"]) == (picks, bans, wins)
    assert top["contest_rate"] == round((picks + bans) / len(rows), 4)
    public = next(h for h in load("opendota_heroStats.json") if h["id"] == hero_id)
    assert top["public_win_rate"] == round(public["pub_win"] / public["pub_pick"], 4)

    longest = max(rows, key=lambda r: r["duration"])
    assert result["records"]["longest_match"]["match_id"] == longest["match_id"]
    bloodiest = max(rows, key=lambda r: r["radiant_score"] + r["dire_score"])
    assert result["records"]["most_kills"]["value"] == (
        bloodiest["radiant_score"] + bloodiest["dire_score"]
    )
    assert all(h["picks"] >= 5 for h in result["highest_win_rate"])
    assert "contest_rate" in out["definitions"]


async def test_league_namesakes_prefer_newest(replay):
    found = await pro.resolve_league("Dota 2 Space League")
    ids = [found.best["leagueid"], *(lg["leagueid"] for lg in found.namesakes)]
    assert len(ids) > 1
    assert found.best["leagueid"] == max(ids)
    by_id = await pro.resolve_league("19944")
    assert by_id.best["name"].strip() == "EPL Masters 2026"


# get_pro_player


async def test_pro_player(replay):
    out = await call("get_pro_player", {"player": "Yatoro"})
    assert out["status"] == "ok"
    result = out["result"]
    assert result["account_id"] == 321580662
    assert result["team"] == "Team Spirit"
    raw = load("stratz_ProPlayerMatches_321580662_100.json")["data"]["player"]["matches"]
    assert result["matches"] == len(raw)
    assert result["wins"] == sum(1 for m in raw if m["players"][0]["isVictory"])
    assert sum(p["games"] for p in result["by_position"]) == result["matches"]
    heroes = Counter(m["players"][0]["heroId"] for m in raw)
    assert result["signature_heroes"][0]["games"] == heroes.most_common(1)[0][1]
    assert any("Several players" in c for c in out["caveats"])


async def test_pro_player_unknown(replay):
    out = await call("get_pro_player", {"player": "qqqzzzxxx"})
    assert out["status"] == "error"


# get_pro_builds


async def test_pro_builds(replay):
    out = await call("get_pro_builds", {"hero": "OD"})
    assert out["status"] == "ok"
    result = out["result"]
    assert result["hero"] == "Outworld Devourer"
    assert result["pro_games"] == out["sample_size"] >= 10
    names = [i["item"] for i in result["items"]]
    assert "Blink Dagger" in names
    blink = result["items"][names.index("Blink Dagger")]
    assert 0 < blink["pro_share"] <= 1
    assert blink["public_rank"] >= 1
    assert blink["public_typical_min"] is not None
    treads = result["items"][names.index("Power Treads")]
    assert treads["public_purchases"] > 0  # boots come from itemBootPurchase
    skills = result["skills"]
    assert skills["pro_common_first_10"][0]["games"] >= 1
    assert set(skills["public_max_priority"]) <= {a["ability"] for a in skills["abilities"]}
    assert any("correlational" in c for c in out["caveats"])
    assert sum(p["games"] for p in result["pro_positions"]) == result["pro_games"]


async def test_pro_builds_unknown_hero_and_small_sample(replay):
    unknown = await call("get_pro_builds", {"hero": "qqqzzzxxx"})
    assert unknown["status"] == "error"
    small = await call("get_pro_builds", {"hero": "OD", "min_matches": 500})
    assert small["status"] == "insufficient_data"


# get_live_matches


async def test_live_matches(replay):
    out = await call("get_live_matches", {"limit": 5})
    assert out["status"] == "ok"
    games = out["result"]
    raw = {int(g["match_id"]): g for g in load("opendota_live.json")}
    assert all(not raw[g["match_id"]]["deactivate_time"] for g in games)
    assert 9076056982 not in {g["match_id"] for g in games}  # ended, still listed by /live
    assert games[0]["league_id"]  # league games first
    first = raw[games[0]["match_id"]]
    assert games[0]["radiant_networth_lead"] == first["radiant_lead"]
    assert games[0]["delay_s"] == first["delay"]
    assert any("delayed" in c and str(first["delay"]) in c for c in out["caveats"])
    everything = await call("get_live_matches", {"limit": 30, "pro_only": False})
    assert len(everything["result"]) >= len(games)

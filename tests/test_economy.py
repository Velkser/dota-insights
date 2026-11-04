import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP

from dota_insights import sources
from dota_insights.analytics import economy
from dota_insights.cache import clear_all
from dota_insights.mcp.tools import economy as economy_tools
from dota_insights.models import Filters
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "economy"
# The fixtures hold the newest 150 parsed league matches on 7.41 (60 with Shadow Fiend).
SAMPLE = 150
HERO_SAMPLE = 60
SF = 11


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


EXPLORER = {
    fixture["sql"]: fixture
    for fixture in (
        _load(f"od_explorer_{name}.json")
        for name in ("matches", "players", f"matches_hero_{SF}", f"players_hero_{SF}")
    )
}
STRATZ = {
    json.dumps(fixture["variables"], sort_keys=True): fixture["data"]
    for fixture in (_load(f"stratz_comeback_{name}.json") for name in ("all", "divine_pos1"))
}


@pytest.fixture
def requests(monkeypatch):
    monkeypatch.setattr(economy, "SAMPLE_MATCHES", SAMPLE)
    monkeypatch.setattr(economy, "HERO_SAMPLE_MATCHES", HERO_SAMPLE)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        fixture = EXPLORER.get(request.url.params.get("sql"))
        if fixture is None:
            return httpx.Response(200, json={"rows": [], "rowCount": 0, "err": None})
        return httpx.Response(200, json=fixture)

    def stratz_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        variables = json.loads(request.content)["variables"]
        data = STRATZ.get(json.dumps(variables, sort_keys=True), {"heroStats": {"stats": []}})
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


async def _call(tool: str, args: dict) -> dict:
    mcp = FastMCP("test")
    economy_tools.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return json.loads(result.content[0].text)


def _match(adv, events=(), radiant_win=True, match_id=1):
    return economy.Match(
        match_id=match_id,
        start_time=0,
        duration=len(adv) * 60,
        radiant_win=radiant_win,
        adv=tuple(adv),
        events=tuple(events),
    )


def _player(slot, lane_role, gold_10, net_worth, match_id=1, **extra):
    values = {
        "hero_id": slot + 1,
        "roaming": False,
        "gold": {m: gold_10 * m // 10 for m in economy.FARM_MINUTES},
        "deaths": 0,
        "death_gold": None,
        "dead_s": None,
        "kills": (),
        "buybacks": (),
        "runes": {},
    }
    values.update(extra)
    return economy.Player(
        match_id=match_id, slot=slot, lane_role=lane_role, net_worth=net_worth, **values
    )


def _sample() -> economy.Sample:
    matches = _load("od_explorer_matches.json")["rows"]
    players = _load("od_explorer_players.json")["rows"]
    return economy.Sample(economy.parse_matches(matches), economy.parse_players(players), matches)


# Query builders and parsing


def test_recorded_sql_matches_query_builders():
    assert economy.match_sql("7.41", None, SAMPLE) in EXPLORER
    assert economy.player_sql("7.41", None, SAMPLE) in EXPLORER
    assert economy.match_sql("7.41", SF, HERO_SAMPLE) in EXPLORER
    assert economy.player_sql("7.41", SF, HERO_SAMPLE) in EXPLORER
    assert economy.comeback_variables(Filters()) == {}
    assert economy.comeback_variables(Filters(rank_bracket="divine+", position=1)) == {
        "brackets": ["DIVINE_IMMORTAL"],
        "positions": ["POSITION_1"],
    }


def test_parse_events_sorts_and_skips_junk():
    events = economy.parse_events(["900:roshan:3", "120:tower:2", "bad", "5:tower:9", None])
    assert events == (
        economy.Event(time=120, kind="tower", radiant=True),
        economy.Event(time=900, kind="roshan", radiant=False),
    )


def test_recorded_sample_parses():
    sample = _sample()
    assert len(sample.matches) == SAMPLE
    assert all(len(roster) == 10 for roster in sample.by_match().values())
    kinds = {e.kind for m in sample.matches for e in m.events}
    assert kinds == {"tower", "barracks", "roshan", "tormentor"}
    deaths, victims = economy.extract_deaths(sample)
    # kills_log misses deaths to creeps, towers and denies, but not many.
    recorded = sum(p.deaths for p in victims)
    assert 0.95 * recorded <= len(deaths) <= recorded


# Comebacks


def test_behind_side_and_lead():
    m = _match([0, -3000, 6000], radiant_win=False)
    assert m.lead(True, 1) == -3000 and m.lead(False, 1) == 3000
    assert m.lead(True, 3) is None
    assert economy.behind_side(m, 1, 3000) is True
    assert economy.behind_side(m, 2, 5000) is False
    assert economy.behind_side(m, 2, 7000) is None
    assert m.won(False)


def test_comeback_factor_uses_first_event_after_minute():
    roshan_dire = economy.Event(time=1500, kind="roshan", radiant=False)
    roshan_radiant = economy.Event(time=600, kind="roshan", radiant=True)
    a = _match([0] * 20 + [-6000] * 10, [roshan_radiant, roshan_dire], radiant_win=False)
    b = _match([0] * 20 + [-6000] * 10, [], radiant_win=True, match_id=2)
    factors = {f.name: f for f in economy.comeback_factors([(a, True), (b, True)], 20)}
    roshan = factors["took_next_roshan"]
    # a: first Roshan after minute 20 went to Dire, so the behind Radiant did not take it.
    # b: no Roshan after minute 20 -> left out.
    assert (roshan.present.games, roshan.absent.games, roshan.absent.wins) == (0, 1, 0)


def test_comeback_stats_on_recorded_matches():
    stats = economy.comeback_stats("7.41", _sample(), 20, 5000, [])
    assert 0 < stats.situations <= stats.matches_reaching_minute <= SAMPLE
    assert stats.comeback.win_rate < 0.3
    cell = next(c for c in stats.grid if (c.minute, c.deficit) == (20, 5000))
    assert (cell.games, cell.wins) == (stats.situations, stats.comeback.wins)
    # Deeper deficits are rarer at the same minute.
    at_20 = [c.games for c in stats.grid if c.minute == 20]
    assert at_20 == sorted(at_20, reverse=True)


def test_pub_comeback_heroes_rank_by_share():
    heroes = economy.pub_comeback_heroes(_load("stratz_comeback_all.json")["data"])
    assert len(heroes) == economy.TOP_HEROES
    shares = [h.comeback_win_share for h in heroes]
    assert shares == sorted(shares, reverse=True)
    assert all(h.matches >= economy.STRATZ_MIN_HERO_MATCHES for h in heroes)


# Objectives


def test_first_blood_is_earliest_enemy_kill():
    npc = {"antimage": 1, "axe": 2, "bane": 3}
    radiant_am = _player(0, 1, 0, 0, hero_id=1, kills=(economy.Kill(300, "axe"),))
    dire_axe = _player(128, 1, 0, 0, hero_id=2, kills=(economy.Kill(200, "bane"),))
    dire_bane = _player(129, 1, 0, 0, hero_id=3)  # Dire hero killed by its own ally: deny
    assert economy.first_blood([radiant_am, dire_axe, dire_bane], npc) == (True, 300)


def test_objective_stats_on_recorded_matches():
    stats = economy.objective_stats("7.41", _sample())
    by_name = {o.objective: o for o in stats.objectives}
    assert stats.matches == SAMPLE
    assert by_name["first_blood"].decided == SAMPLE
    roshan = by_name["first_roshan"]
    assert roshan.taker.win_rate > 0.5 and roshan.median_lead_at_event > 0
    assert roshan.when_even.games < roshan.decided
    assert by_name["more_power_runes"].median_lead_at_event is None
    assert by_name["more_power_runes"].when_even is None


# Farm distribution


def test_assign_positions_orders_lanes_by_gold_at_10():
    team = [
        _player(0, 1, 1000, 9000),
        _player(1, 1, 4000, 30000),
        _player(2, 2, 3500, 25000),
        _player(3, 3, 1200, 8000),
        _player(4, 3, 3000, 20000),
    ]
    assert [p.slot for p in economy.assign_positions(team)] == [1, 2, 4, 3, 0]
    roaming = [*team[:4], _player(4, 4, 3000, 20000)]
    assert economy.assign_positions(roaming) is None


def test_farm_distribution_on_recorded_matches():
    dist = economy.farm_distribution("7.41", _sample(), None)
    assert dist.teams + dist.skipped_teams == 2 * SAMPLE
    shares = [p.share for p in dist.positions]
    assert sum(shares) == pytest.approx(1.0)
    assert shares[0] > shares[3] and shares[0] > shares[4]
    assert sum(b.teams for b in dist.carry_share_buckets) == dist.teams
    at_10 = economy.farm_distribution("7.41", _sample(), 10)
    assert at_10.minute == 10 and at_10.teams > 0


# Deaths


def test_extract_deaths_marks_buybacks():
    m = _match([0] * 40)
    killer = _player(
        0,
        1,
        0,
        0,
        hero_id=1,
        kills=(economy.Kill(1500, "axe"), economy.Kill(2000, "axe")),
    )
    victim = _player(128, 1, 0, 0, hero_id=2, deaths=2, buybacks=(1540,))
    sample = economy.Sample([m], [killer, victim], [])
    deaths, victims = economy.extract_deaths(sample, hero_id=2)
    assert [(d.time, d.bought_back) for d in deaths] == [(1500, True), (2000, False)]
    assert victims == [victim]


def test_death_analysis_on_recorded_matches():
    analysis = economy.death_analysis("7.41", _sample(), None, 3)
    assert analysis.deaths == sum(b.deaths for b in analysis.by_minute)
    assert 0 < analysis.buyback_share < 0.2
    timed = [b for b in analysis.by_minute if b.respawn_s_median is not None]
    # Respawn timers and death gold grow as the game goes on.
    assert timed[0].respawn_s_median < timed[-1].respawn_s_median
    assert timed[0].gold_lost_median < timed[-1].gold_lost_median
    # A death costs the team gold lead on average.
    assert all(b.swing_after < 0 for b in analysis.by_minute if b.swing_after is not None)
    assert len(analysis.buybacks) == 6


# Tools


async def test_analyze_death_for_a_hero(requests):
    body = await _call(
        "analyze_death", {"hero": "sf", "later_minutes": 2, "filters": {"min_sample": 50}}
    )
    assert body["status"] == "ok", body
    result = body["result"]
    assert result["hero"] == "Shadow Fiend" and result["later_minutes"] == 2
    assert body["sample_size"] == result["deaths"] >= 50
    assert body["filters"]["patch"] == "7.41"
    assert body["source"] == ["opendota"]
    assert body["period"] and body["data_freshness"]
    assert any("clean" in c for c in body["caveats"])
    assert "swing_after" in body["definitions"]


async def test_analyze_death_all_heroes_and_errors(requests):
    body = await _call("analyze_death", {})
    assert body["status"] == "ok"
    assert body["result"]["hero"] is None
    assert any(k.startswith("swing_after[") for k in body["confidence"])
    body = await _call("analyze_death", {"hero": "zzzzqqq"})
    assert body["status"] == "error"
    body = await _call("analyze_death", {"filters": {"patch": "seven"}})
    assert body["status"] == "error"


async def test_get_farm_distribution(requests):
    body = await _call(
        "get_farm_distribution", {"minute": 20, "filters": {"rank_bracket": "divine"}}
    )
    assert body["status"] == "ok", body
    assert body["result"]["minute"] == 20
    assert len(body["result"]["positions"]) == 5
    assert body["sample_size"] == body["result"]["teams"]
    assert any("rank_bracket" in c for c in body["caveats"])
    assert "share[pos1]" in body["confidence"]


async def test_get_comeback_stats_needs_enough_situations(requests):
    body = await _call("get_comeback_stats", {"minute": 20, "deficit": 5000})
    assert body["status"] == "insufficient_data"
    assert body["sample_size"] < 100 and body["hint"]
    body = await _call(
        "get_comeback_stats",
        {"minute": 20, "deficit": 5000, "filters": {"min_sample": 30}},
    )
    assert body["status"] == "ok", body
    assert body["source"] == ["opendota", "stratz"]
    assert body["result"]["pub_heroes"]
    assert "comeback.win_rate" in body["confidence"]
    assert any("stratz.com" in c for c in body["caveats"])


async def test_get_comeback_stats_passes_pub_filters_and_survives_stratz_down(requests):
    body = await _call(
        "get_comeback_stats",
        {"deficit": 2000, "filters": {"rank_bracket": "divine+", "position": 1, "min_sample": 30}},
    )
    assert body["status"] == "ok"
    sent = [json.loads(r.content)["variables"] for r in requests if r.url.host == "api.stratz.com"]
    assert sent == [{"brackets": ["DIVINE_IMMORTAL"], "positions": ["POSITION_1"]}]
    sources.use_clients(opendota=sources.get_opendota(), stratz=StratzClient(token=None))
    body = await _call("get_comeback_stats", {"deficit": 2000, "filters": {"min_sample": 30}})
    assert body["status"] == "ok"
    assert body["source"] == ["opendota"]
    assert body["result"]["pub_heroes"] == []
    assert any("STRATZ was unavailable" in c for c in body["caveats"])


async def test_get_objective_impact(requests):
    body = await _call("get_objective_impact", {})
    assert body["status"] == "ok", body
    names = [o["objective"] for o in body["result"]["objectives"]]
    assert names[:3] == ["first_blood", "first_tower", "first_roshan"]
    assert body["sample_size"] == SAMPLE
    assert "first_roshan.win_rate" in body["confidence"]
    assert any("causation" in c for c in body["caveats"])


async def test_tools_report_old_patch_and_explorer_errors(requests):
    body = await _call("get_objective_impact", {"filters": {"patch": "7.30"}})
    assert body["status"] == "insufficient_data"
    assert body["sample_size"] == 0

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": [], "err": "statement timeout"})

    clear_all()
    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None, transport=httpx.MockTransport(failing), sleep=_no_sleep
        )
    )
    body = await _call("get_farm_distribution", {})
    assert body["status"] == "error"
    assert "statement timeout" in body["message"]

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import trends as t
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.models import Filters, GameMode
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "trends"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


class Stratz:
    """Mock STRATZ answering from fixtures and recording every request's variables."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.fail:
            return httpx.Response(401)
        query, variables = body["query"], body["variables"]
        if "HeroTrendSeries" in query:
            if variables["brackets"] == ["IMMORTAL"] and variables["positions"] == ["POSITION_1"]:
                data = load("stratz_series_immortal_pos1.json")
            else:
                data = load("stratz_series_ranked.json")
        elif "gameVersions" in query:
            data = load("stratz_game_versions.json")
        elif "patchNotes" in query:
            name = f"stratz_patch_notes_{variables['id']}.json"
            data = load(name) if (FIXTURES / name).exists() else {"constants": {"patchNotes": []}}
        else:  # pragma: no cover
            raise AssertionError(f"unexpected query {query}")
        return httpx.Response(200, json={"data": data})


@pytest.fixture
def stratz(monkeypatch):
    monkeypatch.setattr(t, "now", lambda: NOW)
    mock = Stratz()
    clear_all()
    sources.use_clients(
        stratz=StratzClient(token="t", transport=httpx.MockTransport(mock), sleep=_no_sleep)
    )
    yield mock
    sources.use_clients()
    clear_all()


async def call(name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    return result.structured_content


def month_sum(hero_id: int, months: list[tuple[int, int]], fixture="stratz_series_ranked.json"):
    starts = {int(datetime(y, m, 1, tzinfo=UTC).timestamp()) for y, m in months}
    rows = load(fixture)["heroStats"]["winMonth"]
    return sum(r["matchCount"] for r in rows if r["heroId"] == hero_id and r["month"] in starts)


# ------------------------------------------------------------------ analytics


def test_stratz_variables_map_filters():
    assert t.stratz_variables(Filters()) == {
        "brackets": None,
        "positions": None,
        "modes": ["ALL_PICK_RANKED"],
    }
    v = t.stratz_variables(Filters(rank_bracket="divine+", position=1, game_mode="turbo"))
    assert v["brackets"] == ["DIVINE", "IMMORTAL"]
    assert (v["positions"], v["modes"]) == (["POSITION_1"], ["TURBO"])
    assert t.stratz_variables(Filters(rank_bracket="archon-legend"))["brackets"] == [
        "ARCHON",
        "LEGEND",
    ]
    assert t.effective_filters(None).game_mode is GameMode.RANKED_ALL_PICK


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        ("patch", t.WindowSpec("patch")),
        ("7.40->7.41", t.WindowSpec("patch", before="7.40", after="7.41")),
        ("7.41e → 7.41f", t.WindowSpec("patch", before="7.41e", after="7.41f")),
        ("7.39 to 7.41", t.WindowSpec("patch", before="7.39", after="7.41")),
        ("7.40-7.41", t.WindowSpec("patch", before="7.40", after="7.41")),
        ("28d", t.WindowSpec("days", days=28)),
        ("last 14 days", t.WindowSpec("days", days=14)),
    ],
)
def test_parse_window(text, spec):
    assert t.parse_window(text) == spec


def test_parse_window_rejects_garbage():
    with pytest.raises(t.TrendInputError):
        t.parse_window("since forever")


def test_series_uses_weeks_then_earlier_months():
    series = t.parse_stratz_series(load("stratz_series_ranked.json"), 10)
    weeks = [b for b in series.buckets if b.kind == "week"]
    months = [b for b in series.buckets if b.kind == "month"]
    assert len(weeks) == 19
    assert weeks[0].start == datetime(2026, 5, 28, tzinfo=UTC)
    assert weeks[-1].end == datetime(2026, 10, 8, tzinfo=UTC)
    assert months[-1].end <= weeks[0].start  # May 2026 overlaps the first week, so it is dropped
    for a, b in zip(series.buckets, series.buckets[1:], strict=False):
        assert a.end <= b.start


def test_aggregate_counts_only_whole_buckets():
    series = t.parse_stratz_series(load("stratz_series_ranked.json"), 10)
    stats = t.aggregate(
        series, "7.40", datetime(2025, 12, 16, tzinfo=UTC), datetime(2026, 3, 24, tzinfo=UTC)
    )
    assert stats.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert stats.end == datetime(2026, 3, 1, tzinfo=UTC)
    assert stats.heroes[40][0] == month_sum(40, [(2026, 1), (2026, 2)])
    assert stats.matches == round(stats.hero_picks / 10)


def test_day_windows_round_up_to_weeks():
    series = t.parse_stratz_series(load("stratz_series_ranked.json"), 10)
    before, after = t.day_windows(series, 10)
    assert (before.buckets, after.buckets) == (2, 2)
    assert after.end == datetime(2026, 10, 8, tzinfo=UTC)
    assert before.end == after.start


def _window(label: str, heroes: dict[int, tuple[int, int]]) -> t.WindowStats:
    picks = sum(n for n, _ in heroes.values())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return t.WindowStats(label, now, now, 1, picks, heroes, 10)


def test_compare_separates_real_change_from_noise():
    before = _window("b", {1: (10_000, 5_000), 2: (10_000, 5_000), 3: (50, 25)})
    after = _window("a", {1: (10_000, 5_600), 2: (10_000, 5_030), 3: (50, 40)})
    comparison = t.compare(before, after, min_sample=100)
    assert comparison.skipped == [3]
    by_hero = {c.hero_id: c for c in comparison.changes}
    assert by_hero[1].win_delta == pytest.approx(0.06)
    assert by_hero[1].win_q < 0.001
    assert by_hero[2].win_q > t.BH_ALPHA  # +0.3 pp on 10k games is noise
    assert by_hero[1].win_q >= by_hero[1].win_p  # BH never lowers a p-value


def test_jumps_skip_thin_points_and_sort_by_size():
    pts = [
        ("a", t.Rate(1000, 500, 10_000)),
        ("b", t.Rate(1000, 560, 10_000)),
        ("c", t.Rate(10, 9, 10_000)),
        ("d", t.Rate(1000, 520, 10_000)),
    ]
    jumps = t.jumps(pts, min_sample=100)
    assert [(j.from_label, j.to_label) for j in jumps] == [("a", "b")]
    assert jumps[0].win_delta == pytest.approx(0.06)


def test_opendota_series_drops_partial_day():
    series = t.parse_opendota_series(load("opendota_hero_stats.json"), NOW)
    assert len(series.buckets) == 6
    assert series.buckets[-1].end == datetime(2026, 11, 4, tzinfo=UTC)
    am = next(r for r in load("opendota_hero_stats.json") if r["id"] == 1)
    assert series.buckets[0].heroes[1] == (am["pub_pick_trend"][0], am["pub_win_trend"][0])


def test_clean_note_strips_markup():
    raw = 'First = <font color=\\"#7CA1CC\\">First</font>  team'
    assert t.clean_note(raw) == "First = First team"


# ------------------------------------------------------------------ find_meta_trends


async def test_meta_trends_patch_window(stratz):
    out = await call("find_meta_trends", {"limit": 5})
    assert out["status"] == "ok"
    res = out["result"]
    assert (res["before"]["label"], res["after"]["label"]) == ("7.40", "7.41")
    assert res["before"]["patch_from"] == "2025-12-16"
    assert res["before"]["covered_from"] == "2026-01-01"
    assert res["after"]["covered_to"] == "2026-10-07"
    heroes = res["heroes"]
    assert 0 < len(heroes) <= 5
    deltas = [abs(h["win_rate_delta"]) for h in heroes]
    assert deltas == sorted(deltas, reverse=True)
    assert all(h["win_rate_q"] <= t.BH_ALPHA and abs(h["win_rate_delta"]) >= 0.005 for h in heroes)
    veno = next(h for h in heroes if h["hero"] == "Venomancer")
    assert veno["before"]["matches"] == month_sum(40, [(2026, 1), (2026, 2)])
    assert out["filters"]["game_mode"] == "ranked_all_pick"
    assert out["source"] == ["stratz"]
    assert out["sample_size"] == res["before"]["matches"] + res["after"]["matches"]
    assert any("stratz.com" in c for c in out["caveats"])
    assert any("Benjamini-Hochberg" in c for c in out["caveats"])
    assert any("2026-10-07" in c for c in out["caveats"])  # stale newest bucket
    assert "Venomancer.after.win_rate" in out["confidence"]


async def test_meta_trends_rising_pick_rate_only(stratz):
    out = await call(
        "find_meta_trends", {"direction": "rising", "sort_by": "pick_rate_delta", "limit": 20}
    )
    heroes = out["result"]["heroes"]
    assert heroes
    assert all(h["pick_rate_delta"] > 0 and h["pick_rate_q"] <= t.BH_ALPHA for h in heroes)


async def test_meta_trends_letter_patches_with_rank_and_position(stratz):
    out = await call(
        "find_meta_trends",
        {"window": "7.41e->7.41f", "filters": {"rank_bracket": "immortal", "position": 1}},
    )
    assert out["status"] == "ok"
    series_req = next(r for r in stratz.requests if "HeroTrendSeries" in r["query"])
    assert series_req["variables"] == {
        "brackets": ["IMMORTAL"],
        "positions": ["POSITION_1"],
        "modes": ["ALL_PICK_RANKED"],
    }
    res = out["result"]
    assert res["after"]["patch_from"] == "2026-09-15"
    assert res["after"]["covered_from"] == "2026-09-17"  # first whole week inside 7.41f
    assert res["heroes_skipped"] > 0
    assert any("position detection" in c for c in out["caveats"])


async def test_meta_trends_day_window(stratz):
    out = await call("find_meta_trends", {"window": "28d"})
    res = out["result"]
    assert (res["before"]["covered_from"], res["after"]["covered_to"]) == (
        "2026-08-13",
        "2026-10-07",
    )
    assert out["period"] == {"from_date": "2026-08-13", "to_date": "2026-10-07"}


async def test_meta_trends_falls_back_to_opendota(monkeypatch):
    monkeypatch.setattr(t, "now", lambda: NOW)
    clear_all()

    def opendota(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/heroStats")
        return httpx.Response(200, json=load("opendota_hero_stats.json"))

    sources.use_clients(
        stratz=StratzClient(token=None),
        opendota=OpenDotaClient(
            api_key=None, transport=httpx.MockTransport(opendota), sleep=_no_sleep
        ),
    )
    try:
        out = await call("find_meta_trends", {"window": "7d"})
        rank_out = await call("find_meta_trends", {"window": "7d", "filters": {"position": 2}})
        patch_out = await call("find_meta_trends", {"window": "patch"})
    finally:
        sources.use_clients()
        clear_all()
    assert out["status"] == "ok"
    assert out["source"] == ["opendota"]
    assert out["filters"]["game_mode"] is None
    assert out["period"] == {"from_date": "2026-10-29", "to_date": "2026-11-03"}
    assert any("OpenDota" in c for c in out["caveats"])
    assert rank_out["status"] == "error"
    assert patch_out["status"] == "error"


async def test_meta_trends_bad_inputs(stratz):
    assert (await call("find_meta_trends", {"window": "yesterday-ish"}))["status"] == "error"
    unknown = await call("find_meta_trends", {"window": "9.99->7.41"})
    assert unknown["status"] == "error"
    assert "7.41" in unknown["message"]
    thin = await call("find_meta_trends", {"filters": {"min_sample": 10**9}})
    assert thin["status"] == "insufficient_data"
    assert thin["hint"]


# ------------------------------------------------------------------ get_hero_patch_history


async def test_hero_patch_history(stratz):
    out = await call("get_hero_patch_history", {"hero": "am", "patches": 4})
    assert out["status"] == "ok"
    res = out["result"]
    assert res["hero"] == "Anti-Mage"
    assert [p["patch"] for p in res["patches"]] == ["7.38", "7.39", "7.40", "7.41"]
    p738, _, p740, p741 = res["patches"]
    assert p738["covered_from"] is None and p738["matches"] == 0  # no STRATZ buckets inside
    assert p740["matches"] == month_sum(1, [(2026, 1), (2026, 2)])
    lo, hi = p741["win_rate_ci"]
    assert lo <= p741["win_rate"] <= hi
    assert out["sample_size"] == sum(p["matches"] for p in res["patches"])
    jumps = res["biggest_jumps"]
    assert {(j["from_patch"], j["to_patch"]) for j in jumps} == {("7.39", "7.40"), ("7.40", "7.41")}
    assert abs(jumps[0]["win_rate_delta"]) >= abs(jumps[-1]["win_rate_delta"])
    assert any("7.38" in c for c in out["caveats"])
    assert "7.41.win_rate" in out["confidence"]


async def test_hero_patch_history_letter_patches_end_at_filter(stratz):
    out = await call(
        "get_hero_patch_history",
        {"hero": "Anti-Mage", "patches": 3, "letter_patches": True, "filters": {"patch": "7.41e"}},
    )
    assert [p["patch"] for p in out["result"]["patches"]] == ["7.41c", "7.41d", "7.41e"]


async def test_hero_patch_history_unknown_hero(stratz):
    out = await call("get_hero_patch_history", {"hero": "zzzzqqq"})
    assert out["status"] == "error"


# ------------------------------------------------------------------ get_patch_summary


async def test_patch_summary_with_notes(stratz):
    out = await call("get_patch_summary", {"patch": "7.40", "limit": 3})
    assert out["status"] == "ok"
    res = out["result"]
    assert res["patch"]["patch_from"] == "2025-12-16"
    assert res["previous_patch"]["label"] == "7.39"
    assert [s["name"] for s in res["sub_patches"]] == ["7.40", "7.40b", "7.40c"]
    assert res["patch_notes_available"] is True
    movers = res["win_rate_risers"] + res["win_rate_fallers"]
    assert all(len(m["patch_notes"]) <= 6 for m in movers)
    assert any(m["patch_notes"] for m in movers)
    assert all(m["delta"] > 0 for m in res["win_rate_risers"])
    assert all(m["delta"] < 0 for m in res["pick_rate_fallers"])
    assert out["filters"]["patch"] == "7.40"


async def test_patch_summary_default_has_no_notes(stratz):
    out = await call("get_patch_summary", {})
    res = out["result"]
    assert res["patch"]["label"] == "7.41"
    assert res["patch_notes_available"] is False
    assert [s["name"] for s in res["sub_patches"]][-1] == "7.41f"
    assert any("no patch notes for 7.41" in c for c in out["caveats"])


async def test_patch_summary_without_stratz(monkeypatch):
    clear_all()
    sources.use_clients(stratz=StratzClient(token=None))
    try:
        out = await call("get_patch_summary", {"patch": "7.41"})
    finally:
        sources.use_clients()
        clear_all()
    assert out["status"] == "error"
    assert out["source"] == ["stratz"]

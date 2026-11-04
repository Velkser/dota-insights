import json
import re
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import items as analytics
from dota_insights.analytics.items import TimingRow
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.models import Filters
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "items"
# Recorded 2026-11-04. STRATZ: every hero, DIVINE_IMMORTAL, no position, trimmed to BKB (116)
# for all heroes plus Linken's (123), Battle Fury (145) and Manta (147) for Anti-Mage and
# Storm Spirit. OpenDota: itemTimings for AM + BKB, and the payback explorer query for AM +
# Battle Fury in 7.41.
STRATZ = json.loads((FIXTURES / "stratz_divine_immortal.json").read_text())
ITEM_TIMINGS = json.loads((FIXTURES / "opendota_item_timings_am_bkb.json").read_text())
EXPLORER = json.loads((FIXTURES / "opendota_explorer_am_bfury.json").read_text())


async def _no_sleep(_: float) -> None:
    return None


def _stratz_handler(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        wanted = set(re.findall(r"\b([hs]\d+):", body["query"]))
        data = {key: rows for key, rows in STRATZ["heroStats"].items() if key in wanted}
        return httpx.Response(200, json={"data": {"heroStats": data}})

    return handler


def _opendota_handler(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/scenarios/itemTimings"):
            return httpx.Response(200, json=ITEM_TIMINGS)
        if request.url.path.endswith("/explorer"):
            return httpx.Response(200, json={"rowCount": len(EXPLORER["rows"]), **EXPLORER})
        return httpx.Response(404)

    return handler


@pytest.fixture
def calls():
    """Install mock clients; yields the recorded STRATZ bodies and OpenDota requests."""
    stratz_seen: list = []
    opendota_seen: list = []
    clear_all()
    sources.use_clients(
        stratz=StratzClient(
            token="t", transport=httpx.MockTransport(_stratz_handler(stratz_seen)), sleep=_no_sleep
        ),
        opendota=OpenDotaClient(
            api_key=None,
            transport=httpx.MockTransport(_opendota_handler(opendota_seen)),
            sleep=_no_sleep,
            requests_per_minute=6000,
            burst=100,
        ),
    )
    yield stratz_seen, opendota_seen
    clear_all()
    sources.use_clients()


async def _call(name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    return result.structured_content


DIVINE = {"rank_bracket": "divine+"}


# --- pure helpers ------------------------------------------------------------------------


def test_bucket_labels_and_counts():
    rows = [TimingRow(10, 5, 4), TimingRow(15, 10, 5), TimingRow(24, 4, 1), TimingRow(30, 1, 0)]
    buckets = analytics.timing_buckets(rows, [15, 20, 25])
    assert [b.label for b in buckets] == ["<15", "15-20", "20-25", "25+"]
    assert [(b.games, b.wins) for b in buckets] == [(5, 4), (10, 5), (4, 1), (1, 0)]
    assert buckets[0].win_rate == 0.8
    assert analytics.timing_buckets(rows, [])[0].games == 20


def test_median_minute_and_quartiles():
    rows = [TimingRow(20, 1, 0), TimingRow(30, 2, 1), TimingRow(40, 1, 1)]
    med, (low, high) = analytics.median_minute(rows)
    assert med == 30.0
    assert low <= med <= high
    assert analytics.median_minute([]) is None
    assert analytics.quartile_boundaries(rows) == [30]


def test_stratz_scope_maps_filters():
    scope = analytics.stratz_scope(Filters(rank_bracket="archon-ancient", position="pos3"))
    assert scope.brackets == ("CRUSADER_ARCHON", "LEGEND_ANCIENT")
    assert scope.position == "POSITION_3"
    assert any("widened" in c for c in scope.caveats)
    exact = analytics.stratz_scope(Filters(rank_bracket="divine+"))
    assert exact.brackets == ("DIVINE_IMMORTAL",) and exact.caveats == ()
    assert any("patch" in c for c in analytics.stratz_scope(Filters(patch="7.41")).caveats)


def test_payback_sql_rejects_unsafe_input():
    assert "pm.hero_id = 1" in analytics.payback_sql("bfury", 1, "7.41")
    with pytest.raises(ValueError):
        analytics.payback_sql("x'; drop table", 1, "7.41")


def test_payback_estimate_on_synthetic_games():
    # Controls earn 100 gold/min throughout; buyers 100 before and 300 after minute 10.
    flat = list(range(0, 3100, 100))
    buyer = [g if m <= 10 else 1000 + (m - 10) * 300 for m, g in enumerate(flat)]
    rows = [{"hero_id": 1, "gold_t": flat, "purchase_time": None}] * 6
    rows += [{"hero_id": 1, "gold_t": buyer, "purchase_time": 590}] * 3
    est = analytics.payback_estimate(rows, cost=2000)
    assert est.buyers == 3 and est.buyers_used == 3
    assert est.extra_gpm == 200.0
    assert est.payback_minutes == 10.0


# --- tools on recorded data ---------------------------------------------------------------


async def test_analyze_item_on_hero(calls):
    stratz_seen, _ = calls
    out = await _call(
        "analyze_item",
        {"item": "bkb", "hero": "am", "filters": DIVINE, "timing_buckets": [25, 30, 35]},
    )
    assert out["status"] == "ok"
    res = out["result"]
    assert res["item"] == "Black King Bar" and res["hero"] == "Anti-Mage"
    assert [b["label"] for b in res["win_rate_by_timing"]] == ["<25", "25-30", "30-35", "35+"]
    assert sum(b["games"] for b in res["win_rate_by_timing"]) == res["buyers"] == out["sample_size"]
    assert res["hero_games"] > res["buyers"]
    assert res["buyers_vs_non_buyers"]["hero"] == "Anti-Mage"
    assert any("selection" in c for c in out["caveats"])
    assert "stratz" in out["source"]
    assert "win_rate[<25]" in out["confidence"]
    assert stratz_seen[0]["variables"] == {"b": ["DIVINE_IMMORTAL"], "p": None}


async def test_analyze_item_all_heroes_ranks_best_heroes(calls):
    out = await _call("analyze_item", {"item": "black king bar", "filters": DIVINE})
    assert out["status"] == "ok"
    best = out["result"]["best_heroes"]
    assert best and best == sorted(best, key=lambda b: -b["lift"])
    assert all(b["buyers"] >= 100 and b["non_buyers"] >= 100 for b in best)
    assert all(0 <= b["q_value"] <= 1 for b in best)


async def test_analyze_item_falls_back_to_opendota(calls):
    sources.use_clients(stratz=StratzClient(token=None), opendota=sources.get_opendota())
    out = await _call("analyze_item", {"item": "bkb", "hero": "am", "filters": {"min_sample": 50}})
    assert out["status"] == "ok"
    assert out["source"] == ["opendota"]
    assert out["sample_size"] == sum(int(r["games"]) for r in ITEM_TIMINGS)
    assert any("fell back" in c for c in out["caveats"])


async def test_get_item_overview(calls):
    stratz_seen, _ = calls
    out = await _call("get_item_overview", {"item": "бкб", "filters": DIVINE, "top_heroes": 5})
    assert out["status"] == "ok"
    res = out["result"]
    assert 0 < res["purchase_rate"] < 1
    top = res["top_heroes_by_purchase_rate"]
    assert len(top) == 5
    assert top == sorted(top, key=lambda h: -h["purchase_rate"])
    assert len(stratz_seen) == -(-len(analytics.all_hero_ids()) // analytics.HERO_BATCH)


async def test_compare_items_within_buckets(calls):
    out = await _call(
        "compare_items", {"items": ["bkb", "linken"], "hero": "storm", "filters": DIVINE}
    )
    assert out["status"] == "ok", out
    res = out["result"]
    assert [i["item"] for i in res["items"]] == ["Black King Bar", "Linken's Sphere"]
    assert len(res["timing_adjusted_win_rate"]) == 2
    tests = res["tests"]
    assert any(t["bucket"] == "timing_adjusted" for t in tests)
    assert all(0 <= t["q_value"] <= 1 for t in tests)


async def test_compare_items_rejects_duplicates_and_reports_small_samples(calls):
    dup = await _call("compare_items", {"items": ["bkb", "black king bar"], "hero": "am"})
    assert dup["status"] == "error"
    small = await _call(
        "compare_items",
        {"items": ["bkb", "linken"], "hero": "am", "filters": {**DIVINE, "min_sample": 10**6}},
    )
    assert small["status"] == "insufficient_data"
    assert small["hint"]


async def test_get_item_payback(calls):
    _, opendota_seen = calls
    out = await _call("get_item_payback", {"items": ["battle fury"], "hero": "am"})
    assert out["status"] == "ok", out
    item = out["result"]["items"][0]
    assert item["item"] == "Battle Fury"
    assert item["buyers_used"] >= 100
    assert item["extra_gpm"] > 0 and item["payback_minutes"] > 0
    low, high = out["confidence"]["extra_gpm[Battle Fury]"]
    assert low <= item["extra_gpm"] <= high
    assert "hero_id = 1" in opendota_seen[0].url.params["sql"]


async def test_get_item_payback_insufficient(calls):
    out = await _call(
        "get_item_payback", {"items": ["bfury"], "hero": "am", "filters": {"min_sample": 1000}}
    )
    assert out["status"] == "insufficient_data"
    assert "usable" in out["message"]


async def test_unknown_item_is_an_error(calls):
    out = await _call("analyze_item", {"item": "zzqqxx"})
    assert out["status"] == "error"

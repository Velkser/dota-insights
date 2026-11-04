import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP

from dota_insights import sources
from dota_insights.analytics import farming
from dota_insights.cache import clear_all
from dota_insights.mcp.tools import farming as farming_tools
from dota_insights.models import Filters
from dota_insights.sources.opendota import OpenDotaClient

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


@pytest.fixture
def requests():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.url.path, tuple(sorted(request.url.params.items())))
        name = OPENDOTA_ROUTES.get(key)
        if name is None:
            return httpx.Response(404, json={"error": "not recorded"})
        return httpx.Response(200, json=_load(name))

    clear_all()
    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None, transport=httpx.MockTransport(handler), sleep=_no_sleep
        )
    )
    yield seen
    sources.use_clients()
    clear_all()


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


async def test_get_hero_power_curve(requests):
    body = await _call("get_hero_power_curve", {"hero": "Anti-Mage"})
    assert body["status"] == "ok"
    rows = _load("od_heroes_1_durations.json")
    assert body["sample_size"] == sum(r["games_played"] for r in rows)
    result = body["result"]
    assert result["classification"] == "late"
    assert result["late_win_rate"] > result["early_win_rate"]
    assert "win_rate[30-35]" in body["confidence"]
    assert "classification" in body["definitions"]


async def test_get_hero_power_curve_reports_ignored_filters_and_min_sample(requests):
    body = await _call(
        "get_hero_power_curve",
        {"hero": "am", "filters": {"rank_bracket": "divine", "min_sample": 1_000_000}},
    )
    assert body["status"] == "insufficient_data"
    assert body["filters"]["rank_bracket"] is None
    assert any("rank_bracket" in c for c in body["caveats"])

import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import general
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.models import Filters, GameMode
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "general"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


async def _no_sleep(_: float) -> None:
    return None


class FakeOpenDota:
    """Serves recorded OpenDota responses by path and remembers the requests."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path.removeprefix("/api/")
        if path == "explorer":
            body = load("explorer_ranked_all_pick.json")
        elif path == "distributions":
            body = load("distributions.json")
        elif path.startswith("constants/"):
            body = load(f"constants_{path.split('/', 1)[1]}.json")
        elif path.startswith("records/"):
            body = load(f"records_{path.split('/', 1)[1]}.json")
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=body)

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix("/api/") for r in self.requests]


@pytest.fixture
def opendota():
    fake = FakeOpenDota()
    client = OpenDotaClient(
        api_key=None,
        transport=httpx.MockTransport(fake),
        requests_per_minute=60000,
        sleep=_no_sleep,
        max_retries=0,
    )
    clear_all()
    sources.use_clients(opendota=client, stratz=_stratz(_stratz_ok))
    yield fake
    sources.use_clients()
    clear_all()


def _stratz_ok(request: httpx.Request) -> httpx.Response:
    query = json.loads(request.content)["query"]
    name = "stratz_clusters.json" if "clusters" in query else "stratz_rank_distribution.json"
    return httpx.Response(200, json={"data": load(name)})


def _stratz(handler) -> StratzClient:
    return StratzClient(
        token="t", transport=httpx.MockTransport(handler), sleep=_no_sleep, max_retries=0
    )


async def call(name: str, args: dict | None = None) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    return result.structured_content


# ---------------------------------------------------------------- general stats


def test_sql_applies_mode_and_rank_filters():
    sql = general.build_general_sql(Filters(game_mode=GameMode.TURBO, rank_bracket="archon-legend"))
    assert "game_mode = 23" in sql
    assert "lobby_type" not in sql.split("f as")[1].split("select")[1]
    assert "avg_rank_tier >= 40 and avg_rank_tier < 60" in sql

    ranked = general.build_general_sql(Filters(rank_bracket="divine+"))
    assert "game_mode = 22 and lobby_type = 7" in ranked
    # Immortal is folded into Divine: avg_rank_tier never exceeds 75.
    assert "avg_rank_tier >= 70 and avg_rank_tier < 80" in ranked


def test_immortal_only_rank_is_rejected():
    with pytest.raises(general.UnsupportedFilter):
        general.build_general_sql(Filters(rank_bracket="immortal"))


async def test_general_stats_from_fixture(opendota):
    data = await call("get_general_stats")
    assert data["status"] == "ok"
    result = data["result"]
    rows = load("explorer_ranked_all_pick.json")["rows"]
    total = next(
        r
        for r in rows
        if r["part"] == "filtered"
        and r["radiant_win"] is None
        and r["bin"] is None
        and r["cluster"] is None
    )
    radiant = next(r for r in rows if r["part"] == "filtered" and r["radiant_win"] is True)
    assert result["matches"] == total["n"] == data["sample_size"]
    assert result["radiant_wins"] == radiant["n"]
    assert result["radiant_win_rate"] == pytest.approx(radiant["n"] / total["n"])
    assert result["radiant_win_rate"] + result["dire_win_rate"] == pytest.approx(1)
    low, high = data["confidence"]["radiant_win_rate"]
    assert low < result["radiant_win_rate"] < high
    assert result["duration"]["median_minutes"] == pytest.approx(total["p50"] / 60, abs=0.01)
    assert sum(b["matches"] for b in result["duration"]["bins"]) == total["n"]
    assert sum(r["matches"] for r in result["regions"]) == total["n"]
    assert sum(m["share"] for m in result["game_modes"]) == pytest.approx(1)
    modes = {m["name"] for m in result["game_modes"]}
    assert {"ranked_all_pick", "turbo", "all_pick"} <= modes
    assert data["filters"]["game_mode"] == "ranked_all_pick"
    assert data["source"] == ["opendota", "stratz"]
    regions = {r["name"]: r["matches"] for r in result["regions"]}
    # Clusters 413/417 are only in STRATZ's map (US East); 189 is in both (Russia).
    assert regions["US East"] > 0 and regions["Russia"] > 0
    assert any("unknown" in c and "%" in c for c in data["caveats"])
    assert data["period"] and data["data_freshness"]
    assert "radiant_win_rate" in data["definitions"]


async def test_general_stats_falls_back_to_opendota_regions(opendota):
    sources.use_clients(
        opendota=sources.get_opendota(), stratz=_stratz(lambda _: httpx.Response(401))
    )
    with_stratz = general.cluster_regions(
        load("constants_cluster.json"),
        load("constants_region.json"),
        load("stratz_clusters.json"),
    )
    data = await call("get_general_stats")
    assert data["status"] == "ok"
    assert data["source"] == ["opendota"]
    unknown = next(r for r in data["result"]["regions"] if r["name"] == "unknown")
    od_only = general.cluster_regions(
        load("constants_cluster.json"), load("constants_region.json"), None
    )
    assert len(with_stratz) > len(od_only)
    assert unknown["matches"] > 0


async def test_general_stats_insufficient_below_min_sample(opendota):
    data = await call("get_general_stats", {"filters": {"min_sample": 10_000_000}})
    assert data["status"] == "insufficient_data"
    assert data["hint"]


@pytest.mark.parametrize(
    "filters",
    [{"position": 1}, {"patch": "7.30"}, {"rank_bracket": "immortal"}],
)
async def test_general_stats_rejects_unsupported_filters(opendota, filters):
    data = await call("get_general_stats", {"filters": filters})
    assert data["status"] == "error"
    assert "explorer" not in opendota.paths()


async def test_general_stats_reports_upstream_failure():
    clear_all()
    client = OpenDotaClient(
        api_key=None,
        transport=httpx.MockTransport(FakeOpenDota(status=500)),
        sleep=_no_sleep,
        max_retries=0,
    )
    sources.use_clients(opendota=client)
    try:
        data = await call("get_general_stats")
    finally:
        sources.use_clients()
        clear_all()
    assert data["status"] == "error"


# ---------------------------------------------------------------- records


async def test_records_single_metric(opendota):
    data = await call("get_records", {"metric": "Kills", "limit": 3})
    assert data["status"] == "ok"
    (kills,) = data["result"]
    raw = load("records_kills.json")
    assert [r["value"] for r in kills["rows"]] == [r["score"] for r in raw[:3]]
    assert kills["rows"][0]["rank"] == 1
    assert kills["rows"][0]["hero"]
    assert opendota.paths() == ["records/kills"]


async def test_records_alias_and_hero_filter(opendota):
    raw = load("records_kills.json")
    hero_id = raw[1]["hero_id"]
    from dota_insights.reference import hero_by_id

    name = hero_by_id()[hero_id].display_name
    data = await call("get_records", {"metric": "kills", "hero": name, "limit": 100})
    rows = data["result"][0]["rows"]
    assert rows and all(r["hero_id"] == hero_id for r in rows)
    assert len(rows) == sum(1 for r in raw if r["hero_id"] == hero_id)

    gpm = await call("get_records", {"metric": "GPM"})
    assert gpm["result"][0]["metric"] == "gold_per_min"
    assert len(gpm["result"][0]["rows"]) == 10


async def test_records_all_metrics_top_one(opendota):
    data = await call("get_records")
    assert data["status"] == "ok"
    assert [r["metric"] for r in data["result"]] == [m.value for m in general.RecordMetric]
    duration = next(r for r in data["result"] if r["metric"] == "duration")
    assert duration["unit"] == "seconds"
    assert duration["rows"][0]["hero"] is None
    assert duration["rows"][0]["value"] == load("records_duration.json")[0]["score"]


async def test_records_errors(opendota):
    assert (await call("get_records", {"metric": "mmr"}))["status"] == "error"
    assert (await call("get_records", {"metric": "duration", "hero": "PA"}))["status"] == "error"
    assert (await call("get_records", {"hero": "qqqqzz"}))["status"] == "error"


async def test_records_hero_absent_from_top_list(opendota):
    raw = load("records_kills.json")
    from dota_insights.reference import heroes

    absent = next(h for h in heroes() if h.id not in {r["hero_id"] for r in raw})
    data = await call("get_records", {"metric": "kills", "hero": absent.display_name})
    assert data["status"] == "insufficient_data"


# ---------------------------------------------------------------- rank distribution


async def test_rank_distribution_combines_sources(opendota):
    data = await call("get_rank_distribution")
    assert data["status"] == "ok"
    result = data["result"]
    od_rows = load("distributions.json")["ranks"]["rows"]
    assert result["opendota_total"] == sum(r["count"] for r in od_rows) == data["sample_size"]
    medals = {m["medal"]: m for m in result["medals"]}
    assert list(medals) == [
        "herald",
        "guardian",
        "crusader",
        "archon",
        "legend",
        "ancient",
        "divine",
        "immortal",
    ]
    immortal_od = next(r["count"] for r in od_rows if r["bin"] == 80)
    assert medals["immortal"]["opendota_players"] == immortal_od
    assert medals["immortal"]["opendota_cumulative"] == pytest.approx(1)
    assert sum(m["opendota_share"] for m in medals.values()) == pytest.approx(1)
    assert sum(m["stratz_share"] for m in medals.values()) == pytest.approx(1)

    stratz = load("stratz_rank_distribution.json")
    st_rows = stratz["stratz"]["page"]["players"]["steamAccountByRank"]
    assert medals["immortal"]["stratz_players"] == next(
        r["playerCount"] for r in st_rows if r["rank"] == 80
    )
    assert result["stratz_uncalibrated"] == next(
        r["playerCount"] for r in st_rows if r["rank"] == 0
    )
    assert {b["division"] for b in result["leaderboards"]} == {
        "Americas",
        "Europe",
        "SE Asia",
        "China",
    }
    assert data["source"] == ["opendota", "stratz"]
    assert any("stratz.com" in c for c in data["caveats"])
    low, high = data["confidence"]["immortal_opendota_share"]
    assert low <= medals["immortal"]["opendota_share"] <= high


async def test_rank_distribution_without_stratz(opendota):
    clear_all()
    sources.use_clients(
        opendota=sources.get_opendota(), stratz=_stratz(lambda _: httpx.Response(401))
    )
    data = await call("get_rank_distribution")
    assert data["status"] == "ok"
    assert data["source"] == ["opendota"]
    assert data["result"]["stratz_total"] is None
    assert all(m["stratz_share"] is None for m in data["result"]["medals"])

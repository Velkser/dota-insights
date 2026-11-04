import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import builds
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.mcp.tools.builds import ability_stats, hero_builds, neutral_items
from dota_insights.models.filters import Filters
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

FIXTURES = Path(__file__).parent / "fixtures" / "builds"
AM_POS1 = Filters(rank_bracket="divine+", position=1)


def _load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


async def _no_sleep(_: float) -> None:
    return None


class Recorder:
    """Serves recorded fixtures and remembers what was asked."""

    def __init__(self, *, stratz_status: int = 200, neutral_rows: bool = False) -> None:
        self.stratz_status = stratz_status
        self.neutral_rows = neutral_rows
        self.stratz_queries: list[dict] = []
        self.opendota_paths: list[str] = []
        self.sql: list[str] = []
        self.guide_batch = 0

    def stratz(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.stratz_queries.append(body)
        if self.stratz_status != 200:
            return httpx.Response(self.stratz_status)
        query = body["query"]
        if "itemFullPurchase" in query:
            return httpx.Response(200, json=_load("stratz_aggregates_am_pos1_divine_immortal"))
        if "itemNeutral" in query:
            if self.neutral_rows:
                rows = [
                    {"itemId": 1605, "matchCount": 400, "winCount": 230},
                    {"itemId": 950, "matchCount": 300, "winCount": 150},
                ]
                return httpx.Response(200, json={"data": {"heroStats": {"itemNeutral": rows}}})
            return httpx.Response(200, json=_load("stratz_neutral_am_pos1_divine_immortal"))
        if "guide(" in query:
            return httpx.Response(200, json=_load("stratz_guides_am_pos1"))
        if "m0: match(" in query:
            name = f"stratz_guide_matches_am_pos1_{self.guide_batch}"
            self.guide_batch += 1
            return httpx.Response(200, json=_load(name))
        if "ability(id:" in query:
            return httpx.Response(200, json=_load("stratz_ability_names_am"))
        raise AssertionError(f"unexpected STRATZ query: {query[:80]}")

    def opendota(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        self.opendota_paths.append(path)
        if path == "/constants/items":
            return httpx.Response(200, json=_load("opendota_constants_items_neutral"))
        if path == "/heroes/1/itemPopularity":
            return httpx.Response(200, json=_load("opendota_item_popularity_am"))
        if path == "/explorer":
            sql = request.url.params["sql"]
            self.sql.append(sql)
            name = "opendota_neutral_patch_7.41"
            if "pm.hero_id = 1 " in sql:
                name += "_am"
            return httpx.Response(200, json=_load(name))
        raise AssertionError(f"unexpected OpenDota path: {path}")


@pytest.fixture
def recorder():
    clear_all()
    rec = Recorder()

    def install(**kwargs) -> Recorder:
        nonlocal rec
        rec = Recorder(**kwargs)
        sources.use_clients(
            stratz=StratzClient(
                token="t",
                transport=httpx.MockTransport(lambda r: rec.stratz(r)),
                sleep=_no_sleep,
                max_retries=0,
            ),
            opendota=OpenDotaClient(
                api_key=None,
                transport=httpx.MockTransport(lambda r: rec.opendota(r)),
                sleep=_no_sleep,
            ),
        )
        return rec

    install.current = lambda: rec  # type: ignore[attr-defined]
    yield install
    sources.use_clients()
    clear_all()


# --- analytics helpers ---------------------------------------------------------------------


def test_weighted_median_and_ci():
    buckets = [(10, 1), (12, 2), (14, 1)]
    assert builds.weighted_median(buckets) == 12
    assert builds.weighted_median([(1, 1), (3, 1)]) == 2
    assert builds.weighted_median([]) is None
    spread = [(m, 100) for m in range(20)]
    lo, hi = builds.median_ci(spread)
    assert lo <= builds.weighted_median(spread) <= hi
    assert lo >= 8 and hi <= 11
    assert builds.median_ci([(5, 0)]) is None


def test_bracket_mapping_widens_to_pairs():
    assert builds.stratz_brackets(Filters()) == ((), None)
    assert builds.stratz_brackets(Filters(rank_bracket="immortal")) == (
        ("DIVINE_IMMORTAL",),
        (7, 8),
    )
    names, tiers = builds.stratz_brackets(Filters(rank_bracket="archon-legend"))
    assert names == ("CRUSADER_ARCHON", "LEGEND_ANCIENT")
    assert tiers == (3, 6)


def test_neutral_sql_is_parameterised_safely():
    sql = builds.neutral_sql("7.41", 1)
    assert "pm.hero_id = 1 AND" in sql
    assert "mp.patch = '7.41'" in sql
    with pytest.raises(ValueError):
        builds.neutral_sql("7.41'; drop table x; --", None)


def test_reference_matches_uses_level_10_talents():
    agg = _load("stratz_aggregates_am_pos1_divine_immortal")["data"]
    # 9,390 + 31,515 level-10 talent picks; Power Treads (40,908) is larger, so it wins.
    assert builds.reference_matches(agg) == 40908


def test_talents_pair_shares_sum_to_one():
    agg = _load("stratz_aggregates_am_pos1_divine_immortal")["data"]
    names = {
        r["id"]: r["name"] for r in _load("stratz_ability_names_am")["data"]["constants"].values()
    }
    rows = builds.talents(agg, names)
    assert [r["level"] for r in rows] == [10, 10, 15, 15, 20, 20, 25, 25]
    for level in (10, 15, 20, 25):
        shares = [r["pick_share"] for r in rows if r["level"] == level]
        assert sum(shares) == pytest.approx(1, abs=1e-3)
    assert all("{s:" not in r["name"] for r in rows)


# --- tools -------------------------------------------------------------------------------


async def test_hero_builds_from_stratz(recorder):
    rec = recorder()
    result = await hero_builds("am", AM_POS1)
    assert result.status == "ok", result.message
    assert result.source == ["stratz"]
    assert result.sample_size == 40908
    data = result.result
    assert data["hero"]["name"] == "Anti-Mage"
    starting = {r["item"]: r for r in data["starting_items"]}
    assert starting["branches"]["matches"] == 37418
    assert starting["quelling_blade"]["popularity"] == pytest.approx(36373 / 40908, abs=1e-4)
    core = [r["item"] for r in data["items"]["core"]]
    assert core[:3] == ["bfury", "yasha", "manta"]
    assert data["boots"][0]["item"] == "power_treads"
    assert len(data["talents"]) == 8
    skill = data["skill_build"]
    assert skill["guide_matches"] == 60
    assert skill["max_order"] and skill["first_levels"]
    assert data["facets"] == []
    assert "items.core.bfury.win_rate" in result.confidence
    assert any("correlational" in c for c in result.caveats)
    assert any("stratz.com" in c for c in result.caveats)
    assert result.filters.patch == builds.current_patch()
    # Variables carry the paired bracket and position.
    agg_vars = next(q for q in rec.stratz_queries if "itemFullPurchase" in q["query"])["variables"]
    assert agg_vars == {"h": 1, "b": ["DIVINE_IMMORTAL"], "p": ["POSITION_1"]}
    assert rec.opendota_paths == []


async def test_hero_builds_falls_back_to_opendota(recorder):
    rec = recorder(stratz_status=403)
    result = await hero_builds("Anti-Mage", AM_POS1)
    assert result.status == "ok", result.message
    assert result.source == ["opendota"]
    assert result.filters.position is None and result.filters.rank_bracket is None
    assert result.sample_size == 195  # most-bought starting item: Iron Branch
    assert result.result["item_counts"]["start_game"][0]["item"] == "branches"
    assert rec.opendota_paths == ["/heroes/1/itemPopularity"]


async def test_hero_builds_rejects_unknown_hero_and_old_patch(recorder):
    recorder()
    assert (await hero_builds("zzzzqqq", None)).status == "error"
    result = await hero_builds("am", Filters(patch="7.30"))
    assert result.status == "error"
    assert "current patch" in result.message
    result = await hero_builds("am", Filters(game_mode="turbo"))
    assert result.status == "error"


async def test_hero_builds_insufficient_data(recorder):
    recorder()
    result = await hero_builds("am", Filters(rank_bracket="divine+", position=1, min_sample=10**6))
    assert result.status == "insufficient_data"
    assert result.sample_size == 40908
    assert result.hint


async def test_ability_stats(recorder):
    recorder()
    result = await ability_stats("am", AM_POS1)
    assert result.status == "ok", result.message
    data = result.result
    maxed = {r["ability"]: r for r in data["skill_max_level"]}
    assert maxed["antimage_blink"]["median_level_maxed"] == 9
    assert data["skill_max_level"][0]["ability"] == "antimage_blink"
    scepter = data["aghanims"]["scepter"]
    assert scepter["matches"] == 2929
    assert scepter["median_minute"] == 28
    assert sum(b["matches"] for b in scepter["by_timing"]) == 2929
    assert data["aghanims"]["shard"] is None
    assert any("Shard" in c for c in result.caveats)
    assert "aghanims.scepter.purchase_rate" in result.confidence


async def test_neutral_items_for_hero_falls_back_to_opendota(recorder):
    rec = recorder()
    result = await neutral_items(None, "am", None)
    assert result.status == "ok", result.message
    assert result.source == ["opendota"]
    assert result.sample_size == 289
    assert result.period is not None
    tiers = result.result["tiers"]
    assert set(tiers) <= {"1", "2", "3", "4", "5", "unknown"}
    shiv = next(r for r in tiers["3"] if r["item"] == "serrated_shiv")
    assert shiv["matches"] == 52
    assert shiv["win_rate"] == round(28 / 52, 4)
    assert all(b["matches"] >= 20 for b in result.result["best_for_hero"])
    assert any("pm.hero_id = 1" in s for s in rec.sql)


async def test_neutral_items_prefers_stratz_rows(recorder):
    recorder(neutral_rows=True)
    result = await neutral_items(AM_POS1, "am", None)
    assert result.status == "ok", result.message
    assert result.source == ["stratz"]
    assert result.sample_size == 700
    assert result.filters.position == 1


async def test_neutral_items_all_heroes_by_tier(recorder):
    recorder()
    result = await neutral_items(None, None, 1)
    assert result.status == "ok", result.message
    assert list(result.result["tiers"]) == ["1"]
    rows = result.result["tiers"]["1"]
    assert sum(r["share_of_tier"] for r in rows) == pytest.approx(1, abs=1e-3)
    assert result.result["best_for_hero"] == []


async def test_tools_registered_on_server(recorder):
    recorder()
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"get_hero_builds", "get_ability_stats", "get_neutral_items"} <= names
        result = await client.call_tool("get_neutral_items", {"hero": "am", "tier": 3})
    assert result.structured_content["status"] == "ok"

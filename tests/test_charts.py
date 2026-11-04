import base64
import json
from pathlib import Path

import pytest
from fastmcp import Client

from dota_insights.analytics import charts
from dota_insights.mcp.server import mcp

# Result shapes of get_gold_xp_curves, get_tier_list and analyze_draft (envelopes as the
# tools return them; the numbers are illustrative).
FIXTURES = Path(__file__).parent / "fixtures" / "charts"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


async def call(args: dict):
    async with Client(mcp) as client:
        return await client.call_tool("render_chart", args, raise_on_error=False)


def images(result) -> list[bytes]:
    return [base64.b64decode(b.data) for b in result.content if b.type == "image"]


def test_gold_xp_curves_become_one_line_per_hero_and_outcome():
    chart = charts.build_chart(load("gold_xp_curves"), shape="gold_xp_curves", metric="xp")
    assert chart.kind == "line"
    assert chart.series == ["Anti-Mage", "Medusa"]
    rows = chart.spec["data"]["values"]
    # Anti-Mage: 31 won + 36 lost minutes; Medusa: 31 won, no lost curve.
    assert len(rows) == 31 + 36 + 31
    assert {r["line"] for r in rows} == {"Anti-Mage (won)", "Anti-Mage (lost)", "Medusa (won)"}
    am_lost_10 = next(r for r in rows if r["line"] == "Anti-Mage (lost)" and r["x"] == 10)
    assert am_lost_10["y"] == 6200
    enc = chart.spec["encoding"]
    assert enc["strokeDash"]["scale"]["domain"] == ["won", "lost"]
    assert enc["strokeDash"]["scale"]["range"][0] == [1, 0]  # won is solid
    assert enc["color"]["scale"]["range"] == list(charts.CATEGORICAL[:2])
    assert chart.spec["$schema"].endswith("vega-lite/v5.json")


def test_tier_list_bars_keep_rank_order_with_intervals_and_tier_colors():
    chart = charts.build_chart(load("tier_list"), shape="tier_list")
    assert chart.kind == "bar"
    assert chart.series == ["S", "A", "B", "C", "D"]
    bar, rule = chart.spec["layer"]
    assert bar["encoding"]["y"]["sort"] == ["Abaddon", "Lich", "Viper", "Axe", "Lina", "Invoker"]
    assert bar["encoding"]["color"]["scale"]["domain"] == ["S", "A", "B", "C", "D"]
    assert rule["encoding"]["x"]["field"] == "low"
    first = chart.spec["data"]["values"][0]
    assert first == {
        "x": "Abaddon",
        "y": 55.0,
        "tier": "S",
        "low": 53.0,
        "high": 57.0,
        "matches": 1000,
    }


def test_matchup_matrix_heatmap_is_diverging_around_zero():
    chart = charts.build_chart(load("analyze_draft")["result"], shape="matchup_matrix")
    assert chart.kind == "heatmap"
    rect, text = chart.spec["layer"]
    scale = rect["encoding"]["color"]["scale"]
    assert scale["domain"] == [-3.1, 0, 3.1]
    assert scale["range"] == list(charts.DIVERGING)
    assert rect["encoding"]["x"]["sort"] == ["Lina", "Invoker"]
    assert rect["encoding"]["y"]["sort"] == ["Axe", "Lich", "Viper"]
    assert text["encoding"]["text"]["format"] == "+.1f"


def test_one_signed_heatmap_uses_sequential_scale():
    rows = [{"x": "a", "y": "b", "value": 3}, {"x": "c", "y": "b", "value": 5}]
    chart = charts.build_chart(rows, kind="heatmap")
    assert chart.spec["layer"][0]["encoding"]["color"]["scale"]["range"] == list(charts.SEQUENTIAL)


def test_plain_rows_with_custom_fields():
    rows = [{"min": m, "nw": 1000 * m, "team": t} for m in range(3) for t in ("Radiant", "Dire")]
    chart = charts.build_chart(
        rows, kind="line", x_field="min", y_field="nw", series_field="team", title="NW"
    )
    assert chart.series == ["Radiant", "Dire"]
    assert chart.spec["data"]["values"][1] == {"x": 0.0, "y": 0.0, "series": "Dire"}
    assert chart.spec["title"]["text"] == "NW"


@pytest.mark.parametrize(
    ("data", "kwargs", "message"),
    [
        ([], {"kind": "line"}, "non-empty list"),
        ([{"x": 1}], {"kind": "line"}, r"lacks field\(s\) y"),
        ([{"x": "a", "y": 1}], {"kind": "line"}, "finite number"),
        ([{"x": 1, "y": float("nan")}], {"kind": "bar"}, "finite number"),
        ([{"x": 1, "y": 2}], {}, "Pass kind"),
        ({"status": "error", "result": None}, {"shape": "tier_list"}, "status 'error'"),
        ({"heroes": []}, {"shape": "tier_list"}, "get_tier_list"),
        ([{"x": 1, "y": 2}], {"shape": "tier_list", "kind": "line"}, "drawn as a bar"),
        (
            [{"x": i, "y": i, "series": f"s{i}"} for i in range(9)],
            {"kind": "line"},
            "9 series",
        ),
        ([{"x": i, "y": i} for i in range(5001)], {"kind": "line"}, "at most 5000"),
    ],
)
def test_bad_input_is_explained(data, kwargs, message):
    with pytest.raises(charts.ChartInputError, match=message):
        charts.build_chart(data, **kwargs)


async def test_render_chart_tool_returns_spec_and_png():
    result = await call({"data": load("tier_list"), "shape": "tier_list", "title": "Carries"})
    body = result.structured_content
    assert body["status"] == "ok"
    assert body["result"]["kind"] == "bar"
    assert body["result"]["vega_lite_spec"]["title"]["text"] == "Carries"
    assert body["result"]["png_attached"] is True
    pngs = images(result)
    assert len(pngs) == 1
    assert pngs[0].startswith(PNG_MAGIC)
    assert body["result"]["png_bytes"] == len(pngs[0])
    assert body["definitions"]["vega_lite_spec"]
    assert any("caveats" in c for c in body["caveats"])
    # The text block repeats the envelope for clients that ignore structured content.
    assert json.loads(result.content[0].text)["status"] == "ok"


async def test_render_chart_vega_only_skips_png():
    result = await call(
        {"data": load("analyze_draft"), "shape": "matchup_matrix", "output": "vega"}
    )
    assert result.structured_content["result"]["png_attached"] is False
    assert images(result) == []


async def test_render_chart_bad_input_is_an_error_envelope():
    result = await call({"data": [{"x": 1}], "kind": "line"})
    body = result.structured_content
    assert body["status"] == "error"
    assert "lacks field(s) y" in body["message"]
    assert images(result) == []


async def test_render_failure_keeps_the_spec(monkeypatch):
    def broken(spec, scale=2.0):
        raise RuntimeError("no renderer")

    monkeypatch.setattr(charts, "render_png", broken)
    result = await call({"data": [{"x": "a", "y": 1}], "kind": "bar"})
    body = result.structured_content
    assert body["status"] == "ok"
    assert body["result"]["png_attached"] is False
    assert any("no renderer" in c for c in body["caveats"])

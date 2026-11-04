import json
from datetime import UTC, date, datetime

import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel, ValidationError

from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.models import Filters


class WinRate(BaseModel):
    hero: str
    win_rate: float


FILTERS = Filters(patch="7.37d", rank_bracket="divine+", min_sample=100)
PERIOD = Period(from_date=date(2026, 10, 1), to_date=date(2026, 10, 31))


def make_ok(sample_size: int = 250) -> ToolResult[WinRate]:
    return ok(
        WinRate(hero="pudge", win_rate=0.5),
        filters=FILTERS,
        sample_size=sample_size,
        period=PERIOD,
        definitions={"win_rate": "wins / matches"},
        confidence={"win_rate": (0.44, 0.56)},
        caveats=["test data"],
        source=["opendota"],
        data_freshness=datetime(2026, 11, 1, tzinfo=UTC),
    )


def test_ok():
    r = make_ok()
    assert r.status == "ok"
    assert r.result.hero == "pudge"
    assert r.message is None and r.hint is None


def test_ok_rejects_small_sample():
    with pytest.raises(ValidationError, match="insufficient_data"):
        make_ok(sample_size=99)


def test_ok_at_threshold():
    assert make_ok(sample_size=100).sample_size == 100


def test_ok_with_filters_needs_sample_size():
    with pytest.raises(ValidationError):
        ok({"x": 1}, filters=FILTERS)


def test_ok_without_filters():
    assert ok({"version": "0.1.0"}).result == {"version": "0.1.0"}


def test_insufficient_data():
    r = insufficient_data(
        "Only 12 matches",
        "Drop the position filter or use rank_bracket 'ancient+'.",
        filters=FILTERS,
        sample_size=12,
    )
    assert r.status == "insufficient_data"
    assert r.result is None
    assert r.message == "Only 12 matches"
    assert "ancient+" in r.hint
    assert r.sample_size == 12


def test_error():
    r = error("OpenDota timed out", hint="Retry in a minute.", source=["opendota"])
    assert r.status == "error"
    assert r.result is None
    assert r.source == ["opendota"]


def test_non_ok_needs_message_and_no_result():
    with pytest.raises(ValidationError):
        ToolResult(status="error")
    with pytest.raises(ValidationError):
        ToolResult(status="insufficient_data", message="x", result=1)


def test_bad_confidence_and_period():
    with pytest.raises(ValidationError):
        ok(1, confidence={"win_rate": (0.6, 0.4)})
    with pytest.raises(ValidationError):
        Period(from_date=date(2026, 2, 1), to_date=date(2026, 1, 1))


def test_bad_source():
    with pytest.raises(ValidationError):
        ok(1, source=["dotabuff"])


def test_round_trip():
    r = make_ok()
    raw = r.model_dump_json()
    back = ToolResult[WinRate].model_validate_json(raw)
    assert back == r
    data = json.loads(raw)
    assert data["period"] == {"from_date": "2026-10-01", "to_date": "2026-10-31"}
    assert data["confidence"] == {"win_rate": [0.44, 0.56]}
    assert data["filters"]["rank_bracket"] == "divine+"


def test_json_schema():
    schema = ToolResult[WinRate].model_json_schema()
    json.dumps(schema)
    props = schema["properties"]
    assert props["status"]["enum"] == ["ok", "insufficient_data", "error"]
    assert {"WinRate", "Filters", "Period"} <= set(schema["$defs"])
    assert all(p.get("description") for p in props.values())


async def test_fastmcp_output_schema_and_call():
    server = FastMCP("envelope-test")

    @server.tool
    def hero_win_rate(hero: str, filters: Filters | None = None) -> ToolResult[WinRate]:
        """Win rate of a hero."""
        filters = filters or Filters()
        if hero == "rare":
            return insufficient_data("Only 3 matches", "Widen rank_bracket.", filters=filters)
        return ok(WinRate(hero=hero, win_rate=0.5), filters=filters, sample_size=500)

    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "status" in tools["hero_win_rate"].output_schema["properties"]
        assert "filters" in tools["hero_win_rate"].input_schema["properties"]

        good = await client.call_tool("hero_win_rate", {"hero": "pudge"})
        assert good.structured_content["status"] == "ok"
        assert good.structured_content["result"]["hero"] == "pudge"

        rare = await client.call_tool(
            "hero_win_rate", {"hero": "rare", "filters": {"rank_bracket": "immortal"}}
        )
        assert rare.structured_content["status"] == "insufficient_data"
        assert rare.structured_content["filters"]["rank_bracket"] == "immortal"

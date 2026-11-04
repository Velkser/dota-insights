import asyncio
import logging
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from dota_insights import sources
from dota_insights.cache import clear_all
from dota_insights.logging import TOOL_CALL_LOGGER, ToolCallLoggingMiddleware
from dota_insights.mcp import errors
from dota_insights.mcp.envelope import ToolResult, insufficient_data, ok
from dota_insights.mcp.errors import (
    FALLBACK_HINT,
    ErrorEnvelopeMiddleware,
    map_exception,
    retry_hint,
    source_error,
)
from dota_insights.mcp.server import mcp
from dota_insights.models import Filters
from dota_insights.sources.opendota import (
    OpenDotaClient,
    OpenDotaError,
    OpenDotaNotFoundError,
    OpenDotaRateLimitedError,
    OpenDotaUnavailableError,
)
from dota_insights.sources.stratz import (
    StratzAuthError,
    StratzClient,
    StratzError,
    StratzQueryError,
    StratzRateLimitedError,
    StratzUnavailableError,
)

MAPPINGS = [
    (OpenDotaRateLimitedError("429", retry_after=42), "opendota", "rate limit", "42 seconds"),
    (OpenDotaRateLimitedError("429"), "opendota", "rate limit", "Retry in a minute"),
    (OpenDotaNotFoundError("match 1 not found", status_code=404), "opendota", "no such", "id"),
    (OpenDotaUnavailableError("HTTP 503"), "opendota", "unavailable", "few minutes"),
    (OpenDotaError("HTTP 400", status_code=400), "opendota", "failed", "Retry"),
    (StratzAuthError("no token"), "stratz", "token", "DOTA_INSIGHTS_STRATZ_API_TOKEN"),
    (StratzRateLimitedError("429", retry_after=5), "stratz", "rate limit", "5 seconds"),
    (StratzUnavailableError("HTTP 502"), "stratz", "unavailable", "few minutes"),
    (StratzQueryError("bad field", errors=[{"message": "x"}]), "stratz", "rejected", "change"),
    (StratzError("HTTP 418", status_code=418), "stratz", "failed", "Retry"),
]


@pytest.mark.parametrize(("exc", "source", "in_message", "in_hint"), MAPPINGS)
def test_source_error_maps_each_source_exception(exc, source, in_message, in_hint):
    result = source_error(exc)
    assert result is not None
    assert result.status == "error"
    assert result.source == [source]
    assert in_message.lower() in result.message.lower()
    assert in_hint in result.hint


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        httpx.ReadTimeout("slow"),
        MCPError(code=-32000, message="Tool 'x' execution timed out after 5s"),
    ],
)
def test_source_error_maps_timeouts(exc):
    result = map_exception(exc, timeout=5)
    assert result is not None
    assert "timed out" in result.message
    assert "Retry" in result.hint


def test_source_error_keeps_filters_and_overrides_source():
    filters = Filters(patch="7.39c")
    result = source_error(StratzUnavailableError("down"), filters=filters, source=["opendota"])
    assert result.filters == filters
    assert result.source == ["opendota"]


def test_other_exceptions_are_not_mapped_but_still_get_a_hint():
    assert map_exception(ValueError("boom")) is None
    result = source_error(ValueError("boom"))
    assert result.status == "error"
    assert result.hint == errors.SOURCE_RETRY_HINT


def test_retry_hint():
    assert "30 seconds" in retry_hint(OpenDotaRateLimitedError("429", retry_after=30))


def test_heavy_tools_get_longer_timeouts():
    assert errors.tool_timeout("get_item_overview") > errors.tool_timeout("analyze_match")
    assert errors.tool_timeout("a_tool_added_later") == errors.DEFAULT_TOOL_TIMEOUT


async def test_every_timeout_override_names_a_registered_tool():
    names = {t.name for t in await mcp.list_tools()}
    assert set(errors.TOOL_TIMEOUTS) <= names


def test_insufficient_data_requires_a_hint():
    with pytest.raises(ValueError, match="widen"):
        insufficient_data("too few matches", " ")


def _server(raise_exc: BaseException | None = None, sleep: float = 0) -> FastMCP:
    server = FastMCP("errors-test")
    server.add_middleware(ToolCallLoggingMiddleware())
    server.add_middleware(ErrorEnvelopeMiddleware())

    @server.tool
    async def stats() -> ToolResult[dict[str, Any]]:
        if sleep:
            await asyncio.sleep(sleep)
        if raise_exc is not None:
            raise raise_exc
        return ok({"x": 1})

    @server.tool
    async def plain() -> dict[str, int]:
        raise OpenDotaUnavailableError("down")

    @server.tool
    async def hintless() -> ToolResult[dict[str, Any]]:
        return ToolResult(status="error", message="bad input")

    @server.tool
    async def refuses() -> ToolResult[dict[str, Any]]:
        raise ToolError("deliberate")

    return server


@pytest.mark.parametrize(("exc", "source", "in_message", "in_hint"), MAPPINGS)
async def test_middleware_turns_source_exceptions_into_envelopes(exc, source, in_message, in_hint):
    async with Client(_server(exc)) as client:
        result = await client.call_tool("stats", {})
    body = result.structured_content
    assert not result.is_error
    assert body["status"] == "error"
    assert body["result"] is None
    assert body["source"] == [source]
    assert in_hint in body["hint"]


async def test_middleware_hides_details_of_unexpected_exceptions(caplog):
    caplog.set_level(logging.ERROR, logger="dota_insights.mcp.errors")
    async with Client(_server(KeyError("secret-token-123"))) as client:
        result = await client.call_tool("stats", {})
    body = result.structured_content
    assert body["status"] == "error"
    assert "KeyError" in body["message"]
    assert "secret-token-123" not in str(body)
    assert body["hint"]
    assert any(r.message == "unexpected tool failure" for r in caplog.records)


async def test_middleware_times_out_slow_tools(monkeypatch):
    monkeypatch.setitem(errors.TOOL_TIMEOUTS, "stats", 0.05)
    async with Client(_server(sleep=5)) as client:
        result = await client.call_tool("stats", {})
    body = result.structured_content
    assert body["status"] == "error"
    assert "timed out after 0.05 seconds" in body["message"]


async def test_middleware_passes_deliberate_and_non_envelope_errors_through():
    async with Client(_server()) as client:
        assert (await client.call_tool("stats", {})).structured_content["status"] == "ok"
        with pytest.raises(ToolError, match="deliberate"):
            await client.call_tool("refuses", {})
        with pytest.raises(ToolError):
            await client.call_tool("plain", {})


async def test_middleware_adds_a_hint_to_hintless_error_envelopes():
    async with Client(_server()) as client:
        result = await client.call_tool("hintless", {})
    assert result.structured_content["hint"] == FALLBACK_HINT
    assert FALLBACK_HINT in result.content[0].text


async def test_envelope_errors_are_logged_as_errors(caplog):
    caplog.set_level(logging.INFO, logger=TOOL_CALL_LOGGER)
    async with Client(_server(OpenDotaUnavailableError("down"))) as client:
        await client.call_tool("stats", {})
    records = [r for r in caplog.records if r.name == TOOL_CALL_LOGGER]
    assert [r.status for r in records] == ["error"]


# Plausible arguments for every required tool parameter, by name.
SAMPLE_ARGS: dict[str, Any] = {
    "hero": "Pudge",
    "heroes": ["Pudge", "Axe"],
    "item": "Black King Bar",
    "items": ["Black King Bar", "Blink Dagger"],
    "match_id": 8000000000,
    "player": "56939869",
    "player_a": "56939869",
    "player_b": "86745912",
    "team": "Team Spirit",
    "team_a": "Team Spirit",
    "team_b": "OG",
    "league": "The International 2025",
    "query": "pudge",
    "allies": ["Axe"],
    "enemies": ["Lion"],
    "radiant": ["Axe", "Lion", "Pudge", "Sniper", "Lich"],
    "dire": ["Juggernaut", "Crystal Maiden", "Zeus", "Tidehunter", "Lina"],
    "group_a": [{"field": "item_minute", "item": "bkb", "op": "<", "value": 20}],
    "metric": "gpm",
    "table": "player_matches",
    "data": [{"x": 10, "y": 4200, "series": "Anti-Mage"}],
}


async def _no_sleep(_seconds: float) -> None:
    return None


def _failing_clients(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    sources.use_clients(
        opendota=OpenDotaClient(
            api_key=None,
            transport=httpx.MockTransport(respond),
            sleep=_no_sleep,
            max_retries=0,
            requests_per_minute=1e9,
        ),
        stratz=StratzClient(
            token="t",
            transport=httpx.MockTransport(respond),
            sleep=_no_sleep,
            max_retries=0,
            requests_per_minute=1e9,
        ),
    )


@pytest.mark.parametrize("status", [429, 503, 401])
async def test_no_tool_leaks_a_raw_source_exception(status):
    """Audit: with every source failing, each tool still answers with a valid envelope."""
    clear_all()
    _failing_clients(status)
    problems = []
    try:
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                if tool.name == "server_info":
                    continue
                required = tool.input_schema.get("required", [])
                args = {name: SAMPLE_ARGS[name] for name in required}
                result = await client.call_tool(tool.name, args, raise_on_error=False)
                body = result.structured_content or {}
                if result.is_error:
                    problems.append((tool.name, "raised", str(result.content)[:200]))
                elif body.get("status") not in {"ok", "insufficient_data", "error"}:
                    problems.append((tool.name, "not an envelope", str(body)[:200]))
                elif body["status"] != "ok" and body.get("hint") in (None, "", FALLBACK_HINT):
                    problems.append((tool.name, "no hint of its own", body["message"]))
    finally:
        sources.use_clients()
        clear_all()
    assert not problems, "\n".join(map(str, problems))

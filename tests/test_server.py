import json
import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from dota_insights import __version__
from dota_insights.logging import TOOL_CALL_LOGGER, JsonFormatter, ToolCallLoggingMiddleware
from dota_insights.mcp.server import mcp


async def test_server_info():
    async with Client(mcp) as client:
        result = await client.call_tool("server_info", {})
    assert result.data["version"] == __version__
    assert result.data["name"] == "dota-insights"


async def test_tool_call_is_logged(caplog):
    caplog.set_level(logging.INFO, logger=TOOL_CALL_LOGGER)
    async with Client(mcp) as client:
        await client.call_tool("server_info", {})

    records = [r for r in caplog.records if r.name == TOOL_CALL_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.tool == "server_info"
    assert record.status == "ok"
    assert record.duration_ms >= 0


async def test_failed_tool_call_is_logged(caplog):
    server = FastMCP("test")
    server.add_middleware(ToolCallLoggingMiddleware())

    @server.tool
    def boom() -> str:
        raise ValueError("nope")

    caplog.set_level(logging.INFO, logger=TOOL_CALL_LOGGER)
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("boom", {})

    records = [r for r in caplog.records if r.name == TOOL_CALL_LOGGER]
    assert len(records) == 1
    assert records[0].tool == "boom"
    assert records[0].status == "error"


def test_json_formatter_includes_extra_fields():
    record = logging.makeLogRecord(
        {"name": TOOL_CALL_LOGGER, "levelname": "INFO", "msg": "tool call", "tool": "x"}
    )
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "tool call"
    assert line["level"] == "INFO"
    assert line["tool"] == "x"

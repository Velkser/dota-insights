"""Client reports: wrong answers (p5-02) and questions no tool could answer (p5-03).

Both are stored in ``usage_feedback`` (through the usage queue, see ``dota_insights.usage``)
and appear in the usage report for the monthly review. A wrong-answer report is linked to the
same client's latest call of that tool in the last hour, when there is one. Per-client limits
(``rate_limit_feedback_*``) keep the tables from being flooded.
"""

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import Field

from dota_insights.mcp.envelope import ToolResult, error, ok
from dota_insights.usage import store
from dota_insights.usage.events import client_info, current_client, new_id, now_utc

NOT_STORED = "The report could not be stored right now (the server's storage is unavailable)."
NOT_STORED_HINT = "Retry later; nothing else is needed from the user."


async def _known_tool(name: str) -> bool:
    try:
        return await get_context().fastmcp.get_tool(name) is not None
    except Exception:
        return False


async def _store(kind: str, tool: str | None, text: str, extra: str | None) -> ToolResult:
    client = await current_client()
    call_id = await store.last_call(client, tool) if client and tool else None
    name, _ = client_info(None)
    event = {
        "type": "feedback",
        "feedback_id": new_id(),
        "ts": now_utc().isoformat(),
        "kind": kind,
        "tool": tool,
        "call_id": call_id,
        "text": text.strip(),
        "expected": extra.strip() if extra and extra.strip() else None,
        "client_name": name,
        "client_hash": client,
    }
    if not await store.enqueue(event):
        return error(NOT_STORED, hint=NOT_STORED_HINT)
    return ok(
        {
            "recorded": True,
            "feedback_id": event["feedback_id"],
            "linked_to_recent_call": call_id is not None,
        },
        caveats=["Reports are reviewed by the server's maintainer; there is no reply."],
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def report_wrong_answer(
        tool_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                description="The tool whose answer was wrong, e.g. get_hero_overview.",
            ),
        ],
        what_was_wrong: Annotated[
            str,
            Field(
                min_length=3,
                max_length=2000,
                description="What was wrong with the answer, in a sentence or two.",
            ),
        ],
        expected_answer: Annotated[
            str | None,
            Field(max_length=1000, description="What the right answer is, if known."),
        ] = None,
    ) -> ToolResult[dict]:
        """Report to the server's maintainer that a tool's answer was wrong, for instance when
        the user points out a number that contradicts the game or another trusted site. The
        report is linked to your latest call of that tool. Do not include personal data."""
        tool = tool_name.strip()
        if not await _known_tool(tool):
            return error(
                f"There is no tool named {tool!r}.",
                hint="Pass the exact name of the tool whose answer was wrong.",
            )
        return await _store("wrong_answer", tool, what_was_wrong, expected_answer)

    @mcp.tool
    async def suggest_feature(
        question: Annotated[
            str,
            Field(
                min_length=3,
                max_length=2000,
                description="The user's question (or the capability needed), in your words.",
            ),
        ],
        why_no_tool_fits: Annotated[
            str | None,
            Field(max_length=1000, description="Why the available tools could not answer it."),
        ] = None,
    ) -> ToolResult[dict]:
        """Record a Dota 2 question that no tool on this server can answer, so it can be
        considered for a new tool. Call it when you had to tell the user the data is not
        available here. Do not include personal data."""
        return await _store("feature_request", None, question, why_no_tool_fits)

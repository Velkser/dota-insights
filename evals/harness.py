"""Run eval questions through a Claude model that calls the MCP server's tools in a loop.

The model sees only what a Claude client would: the server's instructions and its tool list with
descriptions and input schemas. ``ToolSession`` wraps a FastMCP client for either the in-process
server object or a remote URL; the model client is anything with an async ``create(**params)``
that returns an Anthropic ``Message``-shaped object, so tests can script it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from fastmcp import Client, FastMCP

from evals.dataset import Case
from evals.transcript import ToolCall, Transcript

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TURNS = 8
DEFAULT_MAX_TOKENS = 16000
# Tool results longer than this are cut before they go back to the model.
MAX_RESULT_CHARS = 60_000
# Envelopes kept in transcripts are cut to this many characters of JSON.
MAX_RECORDED_ENVELOPE_CHARS = 20_000

SYSTEM_PROMPT = """\
You are Claude, a helpful assistant. Today is {today}. The user is a Dota 2 player. You are \
connected to the "{server}" MCP server; use its tools when they help answer the question.\
{instructions}"""


class ModelClient(Protocol):
    async def create(self, **params: Any) -> Any: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class CallOutcome:
    text: str
    envelope: dict[str, Any] | None
    is_error: bool


def parse_envelope(structured: Any, text: str) -> dict[str, Any] | None:
    """The tool's result envelope, if the result is one (a dict with a ``status`` field)."""
    candidates = [structured]
    if isinstance(structured, dict) and set(structured) == {"result"}:
        candidates.insert(0, structured["result"])
    with contextlib.suppress(TypeError, ValueError):
        candidates.append(json.loads(text))
    for candidate in candidates:
        if isinstance(candidate, dict) and "status" in candidate:
            return candidate
    return None


class ToolSession:
    """An open MCP connection: tool list, server instructions and tool calls."""

    def __init__(self, client: Client, tools: list[ToolSpec], instructions: str | None, name: str):
        self.client = client
        self.tools = tools
        self.instructions = instructions
        self.server_name = name

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return {t.name: t.input_schema for t in self.tools}

    async def call(self, name: str, arguments: dict[str, Any], timeout: float) -> CallOutcome:
        try:
            result = await self.client.call_tool(
                name, arguments, raise_on_error=False, timeout=timeout
            )
        except Exception as exc:  # transport failures, timeouts, unknown tools
            return CallOutcome(f"Error calling tool {name}: {exc}", None, True)
        text = "\n".join(getattr(block, "text", "") for block in result.content).strip()
        if not text and result.structured_content is not None:
            text = json.dumps(result.structured_content, ensure_ascii=False, default=str)
        envelope = parse_envelope(result.structured_content, text)
        return CallOutcome(text, envelope, result.is_error)


def _instructions(client: Client, server: FastMCP | str) -> str | None:
    if isinstance(server, FastMCP):
        return server.instructions
    init = getattr(client, "initialize_result", None)
    init = init() if callable(init) else init
    return getattr(init, "instructions", None)


class open_session:  # noqa: N801 - used as ``async with open_session(...)``
    """Connect to ``server`` (a FastMCP object or an MCP URL) and list its tools."""

    def __init__(self, server: FastMCP | str):
        self.server = server
        self.client = Client(server)

    async def __aenter__(self) -> ToolSession:
        await self.client.__aenter__()
        tools = [
            ToolSpec(t.name, t.description or "", t.input_schema)
            for t in await self.client.list_tools()
        ]
        name = self.server.name if isinstance(self.server, FastMCP) else "dota-insights"
        return ToolSession(self.client, tools, _instructions(self.client, self.server), name)

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.__aexit__(*exc)


def system_prompt(session: ToolSession, today: date | None = None) -> str:
    instructions = (
        f"\n\nServer instructions:\n{session.instructions}" if session.instructions else ""
    )
    return SYSTEM_PROMPT.format(
        today=(today or date.today()).isoformat(),
        server=session.server_name,
        instructions=instructions,
    )


def _recorded(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """The envelope as stored in a transcript: results too large to keep are cut."""
    if envelope is None:
        return None
    dumped = json.dumps(envelope, ensure_ascii=False, default=str)
    if len(dumped) <= MAX_RECORDED_ENVELOPE_CHARS:
        return json.loads(dumped)
    kept = {k: v for k, v in envelope.items() if k != "result"}
    kept["result_truncated"] = dumped[:MAX_RECORDED_ENVELOPE_CHARS]
    return json.loads(json.dumps(kept, ensure_ascii=False, default=str))


def _usage(response: Any, transcript: Transcript) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    transcript.input_tokens += (getattr(usage, "input_tokens", 0) or 0) + (
        getattr(usage, "cache_creation_input_tokens", 0) or 0
    )
    transcript.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
    transcript.output_tokens += getattr(usage, "output_tokens", 0) or 0


@dataclass
class RunConfig:
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str | None = "medium"
    tool_timeout_s: float = 90.0
    extra_params: dict[str, Any] | None = None


async def run_case(
    case: Case,
    session: ToolSession,
    model: ModelClient,
    config: RunConfig,
    *,
    today: date | None = None,
) -> Transcript:
    """Let the model answer one question, calling tools for at most ``max_turns`` requests."""
    transcript = Transcript(case_id=case.id, model=config.model)
    params: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": system_prompt(session, today),
        "tools": [t.to_anthropic() for t in session.tools],
        **(config.extra_params or {}),
    }
    if config.effort:
        params["output_config"] = {"effort": config.effort}
    messages: list[dict[str, Any]] = [{"role": "user", "content": case.question}]
    started = time.perf_counter()
    try:
        while True:
            if transcript.turns >= config.max_turns:
                transcript.hit_turn_cap = True
                break
            t0 = time.perf_counter()
            response = await model.create(messages=messages, **params)
            transcript.model_latency_s += time.perf_counter() - t0
            transcript.api_calls += 1
            transcript.turns += 1
            _usage(response, transcript)
            transcript.stop_reason = response.stop_reason
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if text:
                transcript.answer = text
            if response.stop_reason == "refusal":
                transcript.error = "model refused"
                break
            if response.stop_reason != "tool_use":
                break
            # Pass the whole content back unchanged (thinking blocks included).
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            results = await asyncio.gather(
                *(_call(session, transcript, u, config.tool_timeout_s) for u in uses)
            )
            messages.append({"role": "user", "content": list(results)})
    except Exception as exc:  # API errors: record and score the case as failed
        transcript.error = f"{type(exc).__name__}: {exc}"
    transcript.latency_s = time.perf_counter() - started
    return transcript


async def _call(session: ToolSession, transcript: Transcript, use: Any, timeout: float) -> dict:
    arguments = use.input if isinstance(use.input, dict) else {}
    t0 = time.perf_counter()
    outcome = await session.call(use.name, arguments, timeout)
    latency = time.perf_counter() - t0
    transcript.tool_latency_s += latency
    status = outcome.envelope.get("status") if outcome.envelope else None
    transcript.calls.append(
        ToolCall(
            name=use.name,
            arguments=arguments,
            status=status if isinstance(status, str) else None,
            envelope=_recorded(outcome.envelope),
            is_error=outcome.is_error,
            error=outcome.text[:500] if outcome.is_error else None,
            latency_s=round(latency, 3),
        )
    )
    text = outcome.text
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[truncated by eval harness]"
    return {
        "type": "tool_result",
        "tool_use_id": use.id,
        "content": text or "(empty result)",
        "is_error": outcome.is_error,
    }


async def run_cases(
    cases: list[Case],
    session: ToolSession,
    model: ModelClient,
    config: RunConfig,
    *,
    concurrency: int = 3,
    on_done: Callable[[Case, Transcript], Awaitable[None] | None] | None = None,
) -> list[Transcript]:
    """Run cases with bounded concurrency, returning transcripts in case order."""
    gate = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> Transcript:
        async with gate:
            transcript = await run_case(case, session, model, config)
        if on_done is not None:
            maybe = on_done(case, transcript)
            if asyncio.iscoroutine(maybe):
                await maybe
        return transcript

    return list(await asyncio.gather(*(one(c) for c in cases)))


class AnthropicModel:
    """``ModelClient`` backed by the Anthropic SDK. Reads ``ANTHROPIC_API_KEY`` itself."""

    def __init__(self, max_retries: int = 4):
        import anthropic  # the evals dependency group; offline tests never import it

        self._client = anthropic.AsyncAnthropic(max_retries=max_retries)

    async def create(self, **params: Any) -> Any:
        if "betas" in params:
            return await self._client.beta.messages.create(**params)
        return await self._client.messages.create(**params)

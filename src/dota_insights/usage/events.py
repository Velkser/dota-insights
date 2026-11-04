"""What is stored about a tool call, and how the caller is identified without storing their IP.

- Arguments keep their shape: hero, item and patch names, numbers and filters stay; player
  identifiers become ``"<player>"``; free text the client writes becomes ``"<text: N chars>"``;
  any other string is cut to ``MAX_STRING`` characters.
- Clients are a 16-hex-digit SHA-256 of the IP and a salt that rotates every ISO week. The salt
  lives only in Redis (``di:usage:salt:<week>``, expiring after the week) and never reaches
  Postgres, so stored hashes cannot be matched back to addresses once it is gone. Weekly
  rotation still lets the report count unique clients per day and per week.
- The client's name and version come from the MCP ``initialize`` client info when the session
  has it, else from the ``User-Agent`` header.
- ``channel`` is ``"api"`` for calls made through the Developer API (``/api/v1``), else
  ``"mcp"``. The API key itself is not stored.
"""

import asyncio
import hashlib
import logging
import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import MiddlewareContext

from dota_insights.config import settings
from dota_insights.kv import KVError, get_kv
from dota_insights.metrics import CallMetrics

log = logging.getLogger(__name__)

MAX_STRING = 200
MAX_ITEMS = 20
MAX_DEPTH = 4
# Arguments that identify a person: never stored.
PLAYER_ARGS = frozenset({"player", "player_a", "player_b", "account_id"})
# Free text written by the client in feedback tools: stored only in usage_feedback.
FREE_TEXT_ARGS = frozenset({"what_was_wrong", "expected_answer", "question", "why_no_tool_fits"})

SALT_KEY = "di:usage:salt:{week}"
SALT_TTL = 9 * 86400

_salts: dict[str, str] = {}
_local_salt = secrets.token_hex(16)


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _shape(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return _cut(value, MAX_STRING)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= MAX_DEPTH:
        return "<nested>"
    if isinstance(value, Mapping):
        return {
            _cut(str(k), 64): _shape_item(str(k), v, depth + 1)
            for k, v in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, list | tuple):
        return [_shape(v, depth + 1) for v in value[:MAX_ITEMS]]
    return _cut(str(value), MAX_STRING)


def _shape_item(key: str, value: Any, depth: int) -> Any:
    if value is None:
        return None
    if key in PLAYER_ARGS:
        return "<player>"
    if key in FREE_TEXT_ARGS:
        return f"<text: {len(str(value))} chars>"
    return _shape(value, depth)


def shape_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """The arguments of a call as stored: see the module docstring."""
    return {
        _cut(str(k), 64): _shape_item(str(k), v, 1)
        for k, v in list((arguments or {}).items())[:MAX_ITEMS]
    }


def week_of(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


async def _salt(week: str) -> str | None:
    """This week's shared salt from Redis (created by whichever instance asks first)."""
    if week in _salts:
        return _salts[week]
    kv = get_kv()
    if kv is None:
        return None
    key = SALT_KEY.format(week=week)
    try:
        await kv.set_nx(key, secrets.token_hex(16), ex=SALT_TTL)
        salt = await kv.get(key)
    except KVError:
        return None
    if salt:
        _salts.clear()
        _salts[week] = salt
    return salt


async def client_hash(ip: str, moment: datetime | None = None) -> str:
    """A weekly-rotating, salted hash of ``ip``.

    Without Redis the salt is this process's own, so hashes from different instances differ.
    """
    salt = await _salt(week_of(moment or now_utc())) or _local_salt
    material = f"{settings.usage_hash_secret or ''}:{salt}:{ip}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def client_info(context: MiddlewareContext | None) -> tuple[str | None, str | None]:
    """``(name, version)`` from the MCP client info, else ``(User-Agent, None)``."""
    info = None
    try:
        params = context.fastmcp_context.session.client_params  # type: ignore[union-attr]
        info = params.client_info if params is not None else None
    except Exception:
        info = None
    if info is not None and info.name:
        return _cut(info.name, 100), _cut(info.version, 50) if info.version else None
    try:
        agent = get_http_request().headers.get("user-agent")
    except RuntimeError:
        agent = None
    return (_cut(agent, 100) if agent else None), None


async def current_client() -> str | None:
    """Hash of the caller's IP, or None outside an HTTP request (stdio, tests)."""
    from dota_insights.mcp.ratelimit import client_ip

    ip = client_ip()
    return await client_hash(ip) if ip else None


def call_event(
    *,
    tool: str,
    arguments: Mapping[str, Any] | None,
    status: str,
    latency_ms: float,
    call: CallMetrics,
    error_kind: str | None,
    message: str | None,
    client: str | None,
    client_name: str | None,
    client_version: str | None,
    channel: str = "mcp",
) -> dict[str, Any]:
    """One ``usage_events`` row as queued (JSON-safe; ``ts`` is ISO 8601)."""
    hits = call.cache_hits + call.shared_cache_hits
    return {
        "type": "call",
        "event_id": new_id(),
        "ts": now_utc().isoformat(),
        "tool": _cut(tool, 64),
        "arguments": shape_arguments(arguments),
        "status": status,
        "error_kind": _cut(error_kind, 64) if error_kind else None,
        "message": _cut(message, 300) if message and status != "ok" else None,
        "latency_ms": round(latency_ms),
        "sources": sorted(call.source_calls),
        "cache_hit": hits > 0 and not call.source_calls,
        "client_name": client_name,
        "client_version": client_version,
        "client_hash": client,
        "channel": channel,
    }


async def _record(context: MiddlewareContext, **fields: Any) -> None:
    from dota_insights.api import context as api_context
    from dota_insights.usage import store

    client = await current_client()
    name, version = client_info(context)
    event = call_event(
        tool=context.message.name,
        arguments=context.message.arguments,
        client=client,
        client_name=name,
        client_version=version,
        channel="api" if api_context.current() is not None else "mcp",
        **fields,
    )
    writes = [store.enqueue(event)]
    if client is not None:
        writes.append(store.remember_call(client, event["tool"], event["event_id"]))
    await asyncio.gather(*writes)


async def record_call(
    context: MiddlewareContext,
    *,
    status: str,
    latency_ms: float,
    call: CallMetrics,
    error_kind: str | None = None,
    message: str | None = None,
) -> None:
    """Store one call's usage event. Bounded by ``usage_write_timeout``; never raises."""
    if not settings.usage_enabled:
        return
    try:
        async with asyncio.timeout(settings.usage_write_timeout):
            await _record(
                context,
                status=status,
                latency_ms=latency_ms,
                call=call,
                error_kind=error_kind,
                message=message,
            )
    except Exception as exc:
        log.warning("usage event dropped", extra={"error": type(exc).__name__})

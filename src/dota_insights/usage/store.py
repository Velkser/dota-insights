"""Where usage events go: a Redis list on the server, Postgres after the collector flushes it.

Serverless instances must not open a database connection per tool call, so each event is one
``RPUSH`` (capped at ``usage_queue_max``) to ``di:usage:queue``. The collector's ``usage`` job
(hourly, ``.github/workflows/collect.yml``) moves the queue into ``usage_events`` /
``usage_feedback`` and deletes rows past the retention. Rows are keyed by event id and inserted
with ``ON CONFLICT DO NOTHING``, so a flush interrupted between insert and trim is harmless.

Without Redis (local runs), events are inserted directly when a database is configured, still
within the caller's timeout; with neither, they are dropped.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dota_insights.collector.db import upsert
from dota_insights.config import settings
from dota_insights.db.access import read_engine
from dota_insights.db.models import UsageEvent, UsageFeedback
from dota_insights.kv import KV, KVError, get_kv

log = logging.getLogger(__name__)

QUEUE_KEY = "di:usage:queue"
LAST_CALL_KEY = "di:usage:last:{client}:{tool}"
# How long a wrong-answer report can still be linked to the call it is about.
LAST_CALL_TTL = 3600
FLUSH_BATCH = 500

_TABLES = {"call": (UsageEvent, "event_id"), "feedback": (UsageFeedback, "feedback_id")}
_COLUMNS = {kind: {c.name for c in table.__table__.columns} for kind, (table, _) in _TABLES.items()}
_last_calls: dict[tuple[str, str], str] = {}


# Fields an entry must have to be stored, and defaults for the other non-null columns.
_REQUIRED = {
    "call": ("event_id", "ts", "tool", "status"),
    "feedback": ("feedback_id", "ts", "kind", "text"),
}
_DEFAULTS: dict[str, dict[str, Any]] = {
    # Events queued before the channel column existed were all MCP calls.
    "call": {
        "arguments": {},
        "sources": [],
        "latency_ms": 0,
        "cache_hit": False,
        "channel": "mcp",
    },
    "feedback": {},
}


def _row(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    kind = event.get("type")
    if kind not in _TABLES or any(not event.get(f) for f in _REQUIRED[kind]):
        return None
    # Every row gets every column: one multi-row INSERT needs the same keys throughout.
    row = {column: event.get(column) for column in _COLUMNS[kind]}
    for column, default in _DEFAULTS[kind].items():
        if row[column] is None:
            row[column] = default
    try:
        row["ts"] = datetime.fromisoformat(row["ts"]) if isinstance(row["ts"], str) else row["ts"]
    except ValueError:
        return None
    return kind, row


def _rows(events: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _TABLES}
    for event in events:
        found = _row(event) if isinstance(event, dict) else None
        if found is None:
            log.warning("malformed usage event skipped")
            continue
        rows[found[0]].append(found[1])
    return rows


def store_events(session: Session, events: Iterable[dict[str, Any]]) -> int:
    """Insert queued events; ones already stored (same id) are skipped."""
    count = 0
    for kind, rows in _rows(events).items():
        if rows:
            table, key = _TABLES[kind]
            upsert(session, table.__table__, rows, [key])
            count += len(rows)
    return count


def _insert_now(engine: Engine, event: dict[str, Any]) -> None:
    with Session(engine) as session, session.begin():
        store_events(session, [event])


async def enqueue(event: dict[str, Any]) -> bool:
    """Queue one event (or insert it without Redis); True if it was stored. Never raises."""
    kv = get_kv()
    if kv is not None:
        try:
            payload = json.dumps(event, default=str)
            await kv.push_capped(QUEUE_KEY, payload, max_len=settings.usage_queue_max)
            return True
        except KVError:
            pass
    engine = read_engine()
    if engine is not None:
        try:
            await asyncio.to_thread(_insert_now, engine, event)
            return True
        except (SQLAlchemyError, OSError) as exc:
            log.warning("usage insert failed", extra={"error": type(exc).__name__})
    return False


async def remember_call(client: str, tool: str, event_id: str) -> None:
    """Note ``event_id`` as the client's latest call of ``tool``, for linking feedback."""
    kv = get_kv()
    if kv is not None:
        try:
            await kv.set(LAST_CALL_KEY.format(client=client, tool=tool), event_id, ex=LAST_CALL_TTL)
            return
        except KVError:
            pass
    if len(_last_calls) > 10_000:
        _last_calls.clear()
    _last_calls[(client, tool)] = event_id


async def last_call(client: str, tool: str) -> str | None:
    """The client's latest call of ``tool`` in the last hour, if known."""
    kv = get_kv()
    if kv is not None:
        try:
            found = await kv.get(LAST_CALL_KEY.format(client=client, tool=tool))
            if found:
                return found
        except KVError:
            pass
    return _last_calls.get((client, tool))


async def queue_length(kv: KV | None = None) -> int | None:
    kv = kv or get_kv()
    if kv is None:
        return None
    try:
        return await kv.list_len(QUEUE_KEY)
    except KVError:
        return None


async def flush_queue(
    kv: KV,
    session_factory: sessionmaker[Session],
    *,
    batch: int = FLUSH_BATCH,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """Move queued events into Postgres, oldest first; returns how many were stored.

    Only one flusher may run at a time (the collector's concurrency group ensures it). A queue
    at its cap trims its oldest entries while being read; those events may be lost.
    """
    moved = 0
    while not should_stop():
        raw = await kv.list_head(QUEUE_KEY, batch)
        if not raw:
            break
        events = []
        for item in raw:
            try:
                events.append(json.loads(item))
            except ValueError:
                log.warning("unreadable usage event skipped")
        with session_factory.begin() as session:
            moved += store_events(session, events)
        await kv.list_drop(QUEUE_KEY, len(raw))
        if len(raw) < batch:
            break
    return moved


def prune(session: Session, now: datetime) -> tuple[int, int]:
    """Delete calls and feedback past their retention; ``(calls, feedback)`` deleted."""
    calls = session.execute(
        delete(UsageEvent).where(
            UsageEvent.ts < now - timedelta(days=settings.usage_retention_days)
        )
    ).rowcount
    feedback = session.execute(
        delete(UsageFeedback).where(
            UsageFeedback.ts < now - timedelta(days=settings.feedback_retention_days)
        )
    ).rowcount
    return calls or 0, feedback or 0

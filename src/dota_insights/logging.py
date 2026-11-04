"""Structured logging: one JSON object per line, level from DOTA_INSIGHTS_LOG_LEVEL."""

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from dota_insights import metrics, usage
from dota_insights.config import settings

TOOL_CALL_LOGGER = "dota_insights.tool_calls"

_ENVELOPE_STATUSES = {"ok", "insufficient_data", "error"}

# Attributes every LogRecord has; anything else was passed via ``extra=`` and is emitted.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON with ``extra`` fields merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger to write JSON lines to stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((level or settings.log_level).upper())
    # httpx logs every request URL at INFO: OpenDota's api_key query parameter and the Telegram
    # bot token (part of its URL) would end up in the logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class ToolCallLoggingMiddleware(Middleware):
    """Log every tool call with its metrics: duration, status, sources, cache use, quota left.

    The fields come from ``dota_insights.metrics``; when Telegram alerts are configured the
    call's outcome also feeds the error-rate monitor. Each call is also stored as a usage event
    (``dota_insights.usage``), after the result is computed and within a strict timeout.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(TOOL_CALL_LOGGER)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        tool = context.message.name
        start = time.perf_counter()
        with metrics.track_call() as call:
            try:
                result = await call_next(context)
            except Exception as exc:
                self.logger.warning(
                    "tool call failed",
                    extra={
                        "tool": tool,
                        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                        "status": "error",
                        "error": type(exc).__name__,
                        **call.as_log(),
                    },
                )
                await _record_outcome(failed=True)
                await usage.record_call(
                    context,
                    status="error",
                    latency_ms=(time.perf_counter() - start) * 1000,
                    call=call,
                    error_kind=call.error_kind or type(exc).__name__,
                )
                raise
        status = "error" if getattr(result, "is_error", False) else "ok"
        message = None
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and structured.get("status") in _ENVELOPE_STATUSES:
            status = structured["status"]
            message = structured.get("message")
        self.logger.info(
            "tool call",
            extra={
                "tool": tool,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "status": status,
                "rate_limited": call.rate_limited,
                **call.as_log(),
            },
        )
        # Calls refused by the per-client rate limiter are not server errors.
        await _record_outcome(failed=status == "error" and not call.rate_limited)
        kind = None
        if status == "error":
            kind = "rate_limited" if call.rate_limited else call.error_kind or "error_envelope"
        await usage.record_call(
            context,
            status=status,
            latency_ms=(time.perf_counter() - start) * 1000,
            call=call,
            error_kind=kind,
            message=message,
        )
        return result


async def _record_outcome(*, failed: bool) -> None:
    from dota_insights.telegram.alerts import get_alerter

    alerter = get_alerter()
    if alerter is None:
        return
    try:
        await alerter.record_call(failed)
    except Exception:
        logging.getLogger(__name__).exception("error-rate monitor failed")

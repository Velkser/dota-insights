"""Structured logging: one JSON object per line, level from DOTA_INSIGHTS_LOG_LEVEL."""

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from dota_insights.config import settings

TOOL_CALL_LOGGER = "dota_insights.tool_calls"

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


class ToolCallLoggingMiddleware(Middleware):
    """Log every tool call with its name, duration and status."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(TOOL_CALL_LOGGER)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        tool = context.message.name
        start = time.perf_counter()
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
                },
            )
            raise
        status = "error" if getattr(result, "is_error", False) else "ok"
        self.logger.info(
            "tool call",
            extra={
                "tool": tool,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "status": status,
            },
        )
        return result

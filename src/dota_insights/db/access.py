"""Read access to the collector's database for MCP tools, safe for serverless (Vercel).

Tools call ``await read(fn, *args)``: ``fn(session, *args)`` runs in a worker thread on a
session that opens one connection and closes it afterwards (``NullPool``), so a frozen or
recycled function instance never holds a connection. Point ``DATABASE_URL`` at Neon's pooled
(``-pooler``) endpoint in production; prepared statements are off so PgBouncer's transaction
mode works.

The database is an optimisation, never a dependency: ``read`` returns None when no database is
configured, and logs and returns None when the database fails or is slow, so the caller falls
back to the live APIs.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from dota_insights.config import settings
from dota_insights.db.session import database_url

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 4000
READ_TIMEOUT_SECONDS = 8.0

_override: list[Engine | None] = []


@cache
def _engine() -> Engine:
    url = database_url()
    connect_args: dict[str, Any] = {}
    if url.drivername.startswith("postgresql"):
        connect_args = {"connect_timeout": CONNECT_TIMEOUT_SECONDS, "prepare_threshold": None}
    return create_engine(url, poolclass=NullPool, connect_args=connect_args)


def read_engine() -> Engine | None:
    """The engine tools read from, or None when no database is configured."""
    if _override:
        return _override[-1]
    if not settings.database_url:
        return None
    return _engine()


@contextmanager
def use_database(engine: Engine | None) -> Iterator[None]:
    """Read from ``engine`` (None: no database) inside the block; for tests."""
    _override.append(engine)
    try:
        yield
    finally:
        _override.pop()


def _run[T](engine: Engine, fn: Callable[..., T], args: tuple) -> T:
    with Session(engine) as session, session.begin():
        if engine.dialect.name == "postgresql":
            session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        return fn(session, *args)


async def read[T](fn: Callable[..., T], *args: Any) -> T | None:
    """``fn(session, *args)`` on the read database, or None if there is none or it failed."""
    engine = read_engine()
    if engine is None:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run, engine, fn, args), READ_TIMEOUT_SECONDS
        )
    except (SQLAlchemyError, TimeoutError, OSError) as exc:
        log.warning(
            "database read failed, using live sources",
            extra={"reader": getattr(fn, "__name__", str(fn)), "error": type(exc).__name__},
        )
        return None

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from dota_insights.config import settings


def database_url(url: str | None = None) -> URL:
    """Parse ``url`` (default: ``settings.database_url``) and select the psycopg 3 driver.

    ``postgresql://`` URLs would otherwise pick psycopg2, which is not installed.
    """
    raw = url if url is not None else settings.database_url
    if not raw:
        raise RuntimeError("DOTA_INSIGHTS_DATABASE_URL is not set")
    parsed = make_url(raw)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed


@cache
def get_engine() -> Engine:
    return create_engine(database_url(), pool_pre_ping=True)


@cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    with get_sessionmaker().begin() as session:
        yield session

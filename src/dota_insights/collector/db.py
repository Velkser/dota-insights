"""Database helpers shared by collector jobs: dialect-aware upserts and UTC handling.

Jobs run against PostgreSQL in production and SQLite in tests, so upserts go through
``upsert()`` (``INSERT ... ON CONFLICT``, which both dialects support) instead of dialect
imports scattered across modules.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

# Rows per INSERT statement: keeps bind parameters well under SQLite's 32k limit.
CHUNK_ROWS = 500


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes for ``timestamptz`` columns; they are stored as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_postgresql(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _insert(session: Session, table: Table):
    module = postgresql if is_postgresql(session) else sqlite
    return module.insert(table)


def _chunks(rows: Sequence[Mapping[str, Any]]) -> Iterable[Sequence[Mapping[str, Any]]]:
    for start in range(0, len(rows), CHUNK_ROWS):
        yield rows[start : start + CHUNK_ROWS]


def upsert(
    session: Session,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    *,
    replace: Sequence[str] = (),
    add: Sequence[str] = (),
    touch: str | None = None,
) -> None:
    """Insert ``rows``; on a key conflict overwrite ``replace`` columns and add ``add`` columns.

    With neither, conflicting rows are left alone. ``touch`` names a timestamp column set to
    now on update (inserts get it from the row or the server default).
    """
    for chunk in _chunks(rows):
        stmt = _insert(session, table).values(list(chunk))
        if replace or add:
            set_: dict[str, Any] = {column: stmt.excluded[column] for column in replace}
            set_ |= {column: table.c[column] + stmt.excluded[column] for column in add}
            if touch is not None:
                set_[touch] = utc_now()
            stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_=set_)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
        session.execute(stmt)


def insert_new(
    session: Session,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> list[tuple]:
    """Insert rows that don't exist yet and return the keys of the ones actually inserted."""
    inserted: list[tuple] = []
    for chunk in _chunks(rows):
        stmt = (
            _insert(session, table)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=list(keys))
            .returning(*(table.c[key] for key in keys))
        )
        inserted.extend(tuple(row) for row in session.execute(stmt))
    return inserted


def ensure_month_partitions(session: Session, start_times: Iterable[datetime]) -> None:
    """Create the monthly partitions for ``start_times`` (PostgreSQL only; see migration 0001)."""
    if not is_postgresql(session):
        return
    months = {
        as_utc(ts).replace(day=1, hour=0, minute=0, second=0, microsecond=0) for ts in start_times
    }
    for month in sorted(months):
        session.execute(text("SELECT ensure_month_partitions(:ts)"), {"ts": month})

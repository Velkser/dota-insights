"""Patch versioning (p2-09): register patches, re-label matches, mark aggregates stale.

A match belongs to the latest patch released at or before its ``start_time``. When a new patch
shows up (OpenDota ``/constants/patch`` or the bundled reference data), matches collected after
its release but labelled with the previous patch are moved to it, and every patch whose match
set changed is marked stale in ``aggregate_status``. The aggregate job then rebuilds those
patches from the raw tables (see ``aggregates.rebuild_patch``).
"""

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from dota_insights.collector.db import as_utc, upsert, utc_now
from dota_insights.db.models import AggregateStatus, Match, MatchPlayer, Patch
from dota_insights.reference import patches as reference_patches


@dataclass(frozen=True)
class PatchWindow:
    patch_id: int
    name: str
    start: datetime
    end: datetime | None  # the next patch's release; None for the current patch


def _parse_date(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value)
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def sync_patches(session: Session, raw: Iterable[Mapping[str, Any]]) -> list[int]:
    """Upsert patches from ``{"id", "name", "date"}`` rows; return ids that are new or changed.

    New or moved patches trigger match re-labelling and stale marks.
    """
    existing = {patch.patch_id: patch for patch in session.scalars(select(Patch))}
    changed: list[int] = []
    for row in raw:
        patch_id, name, released_at = int(row["id"]), str(row["name"]), _parse_date(row["date"])
        current = existing.get(patch_id)
        if current is None:
            session.add(Patch(patch_id=patch_id, name=name, released_at=released_at))
            changed.append(patch_id)
        elif current.name != name or as_utc(current.released_at) != released_at:
            current.name, current.released_at = name, released_at
            changed.append(patch_id)
    if not changed:
        return []
    session.flush()
    affected = reassign_matches(session)
    if affected:
        mark_stale(session, affected, f"patch list changed: {sorted(changed)}")
    return changed


def ensure_patches(session: Session) -> None:
    """Seed ``patches`` from the bundled reference data when the table is empty (no API call)."""
    if session.scalar(select(Patch.patch_id).limit(1)) is None:
        sync_patches(
            session,
            ({"id": p.id, "name": p.name, "date": p.date} for p in reference_patches()),
        )


def patch_windows(session: Session) -> list[PatchWindow]:
    rows = session.execute(select(Patch).order_by(Patch.released_at)).scalars().all()
    windows = []
    for index, patch in enumerate(rows):
        end = as_utc(rows[index + 1].released_at) if index + 1 < len(rows) else None
        windows.append(PatchWindow(patch.patch_id, patch.name, as_utc(patch.released_at), end))
    return windows


class PatchLookup:
    """Maps a start time to its patch id; built once per batch."""

    def __init__(self, windows: Sequence[PatchWindow]) -> None:
        self._windows = list(windows)
        self._starts = [window.start for window in self._windows]

    def __call__(self, start_time: datetime) -> int | None:
        index = bisect.bisect_right(self._starts, as_utc(start_time)) - 1
        return self._windows[index].patch_id if index >= 0 else None


def reassign_matches(session: Session) -> set[int]:
    """Move matches whose ``patch_id`` disagrees with the patch windows; return affected patches."""
    affected: set[int] = set()
    for window in patch_windows(session):
        conditions = [Match.start_time >= window.start, Match.patch_id != window.patch_id]
        if window.end is not None:
            conditions.append(Match.start_time < window.end)
        old = set(session.scalars(select(Match.patch_id).where(*conditions).distinct()))
        if not old:
            continue
        affected |= old | {window.patch_id}
        session.execute(
            update(Match).where(*conditions).values(patch_id=window.patch_id),
            execution_options={"synchronize_session": False},
        )
        player_conditions = [MatchPlayer.start_time >= window.start]
        if window.end is not None:
            player_conditions.append(MatchPlayer.start_time < window.end)
        session.execute(
            update(MatchPlayer)
            .where(and_(*player_conditions), MatchPlayer.patch_id != window.patch_id)
            .values(patch_id=window.patch_id),
            execution_options={"synchronize_session": False},
        )
    return affected


def mark_stale(session: Session, patch_ids: Iterable[int], reason: str) -> None:
    now = utc_now()
    rows = [
        {"patch_id": patch_id, "stale": True, "stale_reason": reason[:200], "stale_since": now}
        for patch_id in sorted(set(patch_ids))
    ]
    upsert(
        session,
        AggregateStatus.__table__,
        rows,
        ("patch_id",),
        replace=("stale", "stale_reason", "stale_since"),
    )


def stale_patches(session: Session) -> list[int]:
    return list(
        session.scalars(
            select(AggregateStatus.patch_id)
            .where(AggregateStatus.stale)
            .order_by(AggregateStatus.patch_id)
        )
    )


def mark_fresh(session: Session, patch_id: int) -> None:
    upsert(
        session,
        AggregateStatus.__table__,
        [{"patch_id": patch_id, "stale": False, "stale_reason": None, "rebuilt_at": utc_now()}],
        ("patch_id",),
        replace=("stale", "stale_reason", "rebuilt_at"),
    )

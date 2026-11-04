"""Hero picks and wins per rank bracket for one week, from the collector's tables.

Two sources:

- ``matches`` (default): the collector's own ranked matches (``matches`` + ``match_players``)
  that started inside the week, bracketed by ``avg_rank_tier``.
- ``opendota``: the ``hero_stats_daily`` snapshot of OpenDota ``/heroStats`` taken on the last
  day of the week. OpenDota counts its own trailing window, which is not exactly seven days, and
  only knows picks, so matches per bracket are estimated as picks / 10.

Immortal is folded into Divine (OpenDota public matches cannot tell them apart, see
``db.readers.TOP_BRACKET``); matches of unknown rank are left out. ``ALL_RANKS`` sums all brackets.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from dota_insights.collector.ingest import bracket_of
from dota_insights.db.models import HeroStatsDaily, Match, MatchPlayer, Patch
from dota_insights.db.readers import TOP_BRACKET

Source = Literal["matches", "opendota"]
SOURCES: tuple[Source, ...] = ("matches", "opendota")
ALL_RANKS = 0  # key of the all-brackets total (bracket 0, "unknown", is never stored)
BRACKETS = tuple(range(1, TOP_BRACKET + 1))


@dataclass(slots=True)
class WeekCounts:
    """Counts for ``[start, end)``; ``heroes`` maps (bracket, hero_id) to (games, wins)."""

    start: date
    end: date
    source: Source
    matches: Counter[int] = field(default_factory=Counter)
    heroes: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    patches: Counter[str] = field(default_factory=Counter)  # patch name -> matches
    snapshot_day: date | None = None  # opendota: the hero_stats_daily day used

    def add(self, bracket: int, hero_id: int, games: int, wins: int) -> None:
        for key in ((bracket, hero_id), (ALL_RANKS, hero_id)):
            g, w = self.heroes.get(key, (0, 0))
            self.heroes[key] = (g + games, w + wins)

    def add_matches(self, bracket: int, matches: int) -> None:
        self.matches[bracket] += matches
        self.matches[ALL_RANKS] += matches

    @property
    def total_matches(self) -> int:
        return self.matches[ALL_RANKS]

    @property
    def main_patch(self) -> str | None:
        return self.patches.most_common(1)[0][0] if self.patches else None


def fold(bracket: int) -> int:
    """Bracket 1-7, Immortal (8) folded into Divine; 0 stays unknown."""
    return min(bracket, TOP_BRACKET)


def _instant(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def matches_week(session: Session, start: date, end: date) -> WeekCounts:
    week = WeekCounts(start, end, "matches")
    since, until = _instant(start), _instant(end)
    names = dict(session.execute(select(Patch.patch_id, Patch.name)).all())
    totals = session.execute(
        select(Match.avg_rank_tier, Match.patch_id, func.count())
        .where(Match.start_time >= since, Match.start_time < until)
        .group_by(Match.avg_rank_tier, Match.patch_id)
    )
    for rank_tier, patch_id, count in totals:
        bracket = fold(bracket_of(rank_tier))
        if bracket:
            week.add_matches(bracket, count)
            week.patches[names.get(patch_id, str(patch_id))] += count
    picks = session.execute(
        select(
            Match.avg_rank_tier,
            MatchPlayer.hero_id,
            func.count(),
            func.sum(case((MatchPlayer.win, 1), else_=0)),
        )
        .join(
            Match,
            and_(
                Match.match_id == MatchPlayer.match_id, Match.start_time == MatchPlayer.start_time
            ),
        )
        .where(MatchPlayer.start_time >= since, MatchPlayer.start_time < until)
        .group_by(Match.avg_rank_tier, MatchPlayer.hero_id)
    )
    for rank_tier, hero_id, games, wins in picks:
        bracket = fold(bracket_of(rank_tier))
        if bracket:
            week.add(bracket, hero_id, int(games), int(wins or 0))
    return week


def patch_on(session: Session, day: date) -> str | None:
    """Name of the newest patch released before the end of ``day``."""
    return session.scalar(
        select(Patch.name)
        .where(Patch.released_at < _instant(day + timedelta(days=1)))
        .order_by(Patch.released_at.desc())
        .limit(1)
    )


def opendota_week(session: Session, start: date, end: date) -> WeekCounts:
    week = WeekCounts(start, end, "opendota")
    day = session.scalar(
        select(func.max(HeroStatsDaily.day)).where(
            HeroStatsDaily.scope == "pub", HeroStatsDaily.day < end, HeroStatsDaily.day >= start
        )
    )
    if day is None:
        return week
    week.snapshot_day = day
    rows = session.execute(
        select(
            HeroStatsDaily.rank_bracket,
            HeroStatsDaily.hero_id,
            HeroStatsDaily.picks,
            HeroStatsDaily.wins,
        ).where(HeroStatsDaily.scope == "pub", HeroStatsDaily.day == day)
    )
    picks: Counter[int] = Counter()
    for bracket, hero_id, games, wins in rows:
        week.add(fold(bracket), hero_id, games, wins)
        picks[fold(bracket)] += games
    for bracket, count in picks.items():
        week.add_matches(bracket, count // 10)
    patch = patch_on(session, day)
    if patch:
        week.patches[patch] = week.total_matches
    return week


def load_week(session: Session, source: Source, start: date, end: date) -> WeekCounts:
    loader = matches_week if source == "matches" else opendota_week
    return loader(session, start, end)

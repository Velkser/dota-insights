"""Readers that answer tool filters from the collector's aggregates.

``scope_for(session, filters)`` decides whether the database can answer ``filters`` at all and
returns a ``Scope`` (patch, rank brackets, position, period) or the reason it cannot: a game mode
other than ranked, a letter patch, an Immortal-only rank filter, a patch with no aggregates, one
being rebuilt (``aggregate_status.stale``) or a live patch the collector has not updated for
``MAX_AGE``. The readers below sum the additive counters over a scope; tools then apply their own
``min_sample`` check and fall back to the live APIs when the sample is too small.

Coverage differs from STRATZ: matches are ranked lobbies from OpenDota public matches, the medal
is the lobby's average rank capped at Divine (Immortal games count as Divine, no Immortal split),
patches are major patches (7.41 covers 7.41a-f), and positions, per-game stats, percentiles and
item timings come only from the subset enriched with STRATZ match details.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dota_insights.db.models import (
    AggregateStatus,
    HeroAggregate,
    HeroDraftAggregate,
    HeroMatchupAggregate,
    HeroStatHistogram,
    HeroStatsDaily,
    HistogramStat,
    ItemTimingAggregate,
    MatchupRelation,
    Patch,
    PatchBracketTotal,
)
from dota_insights.mcp.envelope import DB_SOURCE, Period, Source
from dota_insights.models.filters import Filters, GameMode, RankBracket

# A live patch whose newest aggregated match is older than this is treated as out of date.
MAX_AGE = timedelta(hours=24)
# OpenDota caps a public match's average rank at Divine, so bracket 8 never fills.
TOP_BRACKET = 7

DRAFT_SOURCES: list[Source] = [DB_SOURCE, "opendota"]
DETAIL_SOURCES: list[Source] = [DB_SOURCE, "opendota", "stratz"]

IMMORTAL_CAVEAT = (
    "Medals here are the lobby's average rank from OpenDota public matches, capped at Divine: "
    "Immortal games are counted as Divine and cannot be split out, unlike STRATZ."
)
DETAIL_CAVEAT = (
    "Positions, per-game stats, percentiles and item timings come from the subset of collected "
    "matches enriched with STRATZ match details (https://stratz.com), a smaller sample than "
    "the win and pick counts."
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Scope:
    """Where in the aggregates a set of filters points."""

    patch_id: int
    patch: str
    current: bool
    brackets: tuple[int, ...] | None  # None: every bracket, unknown (0) included
    position: int | None
    first_match_at: datetime
    last_match_at: datetime

    @property
    def period(self) -> Period:
        return Period(from_date=self.first_match_at.date(), to_date=self.last_match_at.date())

    @property
    def freshness(self) -> datetime:
        return self.last_match_at

    def caveats(self, *, detail: bool = False) -> list[str]:
        notes = [
            f"Computed from the dota-insights collector's database: ranked matchmaking games "
            f"collected from OpenDota public matches between {self.period.from_date} and "
            f"{self.period.to_date}, patch {self.patch} with all its letter patches together."
        ]
        if self.brackets is None or TOP_BRACKET in self.brackets:
            notes.append(IMMORTAL_CAVEAT)
        if detail:
            notes.append(DETAIL_CAVEAT)
        return notes

    def filters(self, filters: Filters) -> Filters:
        """``filters`` as the database applied them."""
        return filters.model_copy(
            update={"patch": self.patch, "game_mode": GameMode.RANKED_ALL_PICK}
        )


def _brackets(filters: Filters) -> tuple[int, ...] | None | str:
    rank = filters.rank_range
    if rank is None:
        return None
    low, high = rank
    if low.tier > TOP_BRACKET:
        return "Immortal is not separable in OpenDota public matches"
    return tuple(range(low.tier, min(high.tier, TOP_BRACKET) + 1))


def scope_for(session: Session, filters: Filters, *, now: datetime | None = None) -> Scope | str:
    """The scope for ``filters``, or a short reason why the database cannot answer them."""
    if filters.game_mode not in (None, GameMode.RANKED_ALL_PICK):
        return "only ranked matches are collected"
    brackets = _brackets(filters)
    if isinstance(brackets, str):
        return brackets
    patches = list(session.execute(select(Patch.patch_id, Patch.name).order_by(Patch.released_at)))
    if not patches:
        return "no patches registered"
    if filters.patch is None:
        patch_id, name = patches[-1]
    else:
        wanted = filters.patch.strip().lower()
        match = [p for p in patches if p.name == wanted]
        if not match:
            return f"patch {filters.patch!r} is not a major patch in the database"
        patch_id, name = match[0]
    current = patch_id == patches[-1].patch_id
    status = session.get(AggregateStatus, patch_id)
    if status is None or status.last_match_at is None or status.first_match_at is None:
        return f"no aggregates for patch {name}"
    if status.stale:
        return f"aggregates for patch {name} are being rebuilt"
    last = _utc(status.last_match_at)
    assert last is not None
    if current and last < (now or datetime.now(UTC)) - MAX_AGE:
        return f"aggregates for patch {name} are older than {MAX_AGE}"
    return Scope(
        patch_id=patch_id,
        patch=name,
        current=current,
        brackets=brackets,
        position=int(filters.position) if filters.position is not None else None,
        first_match_at=_utc(status.first_match_at),  # type: ignore[arg-type]
        last_match_at=last,
    )


def _in(column, values: Sequence[int] | None) -> list:
    return [] if values is None else [column.in_(list(values))]


Counts = tuple[int, int]  # games, wins


def match_totals(session: Session, scope: Scope) -> tuple[int, int]:
    """(matches, matches with details) in the scope's patch and brackets."""
    games, detailed = session.execute(
        select(
            func.coalesce(func.sum(PatchBracketTotal.games), 0),
            func.coalesce(func.sum(PatchBracketTotal.detailed_games), 0),
        ).where(
            PatchBracketTotal.patch_id == scope.patch_id,
            *_in(PatchBracketTotal.rank_bracket, scope.brackets),
        )
    ).one()
    return int(games), int(detailed)


def draft_counts(
    session: Session, scope: Scope, hero_ids: Sequence[int] | None = None
) -> dict[int, Counts]:
    """Games and wins per hero over every collected match (positions not applied)."""
    rows = session.execute(
        select(
            HeroDraftAggregate.hero_id,
            func.sum(HeroDraftAggregate.games),
            func.sum(HeroDraftAggregate.wins),
        )
        .where(
            HeroDraftAggregate.patch_id == scope.patch_id,
            *_in(HeroDraftAggregate.rank_bracket, scope.brackets),
            *_in(HeroDraftAggregate.hero_id, hero_ids),
        )
        .group_by(HeroDraftAggregate.hero_id)
    )
    return {hero: (int(games), int(wins)) for hero, games, wins in rows}


def position_counts(
    session: Session,
    scope: Scope,
    hero_ids: Sequence[int] | None = None,
    positions: Sequence[int] | None = None,
) -> dict[int, dict[int, Counts]]:
    """Games and wins per hero and position (1-5) from enriched matches."""
    wanted = positions if positions is not None else (1, 2, 3, 4, 5)
    rows = session.execute(
        select(
            HeroAggregate.hero_id,
            HeroAggregate.position,
            func.sum(HeroAggregate.games),
            func.sum(HeroAggregate.wins),
        )
        .where(
            HeroAggregate.patch_id == scope.patch_id,
            *_in(HeroAggregate.rank_bracket, scope.brackets),
            *_in(HeroAggregate.hero_id, hero_ids),
            HeroAggregate.position.in_(list(wanted)),
        )
        .group_by(HeroAggregate.hero_id, HeroAggregate.position)
    )
    out: dict[int, dict[int, Counts]] = defaultdict(dict)
    for hero, position, games, wins in rows:
        out[hero][position] = (int(games), int(wins))
    return dict(out)


def hero_counts(
    session: Session, scope: Scope, hero_ids: Sequence[int] | None = None
) -> dict[int, Counts]:
    """Per-hero counts under the scope: draft level, or the scope's position when it has one."""
    if scope.position is None:
        return draft_counts(session, scope, hero_ids)
    by_position = position_counts(session, scope, hero_ids, [scope.position])
    return {hero: rows[scope.position] for hero, rows in by_position.items()}


def bracket_counts(session: Session, scope: Scope, hero_id: int) -> dict[int, Counts]:
    """One hero's games and wins per bracket (1-7), at the scope's position if it has one."""
    if scope.position is None:
        model, extra = HeroDraftAggregate, []
    else:
        model, extra = HeroAggregate, [HeroAggregate.position == scope.position]
    rows = session.execute(
        select(model.rank_bracket, func.sum(model.games), func.sum(model.wins))
        .where(
            model.patch_id == scope.patch_id,
            model.hero_id == hero_id,
            model.rank_bracket.between(1, TOP_BRACKET),
            *extra,
        )
        .group_by(model.rank_bracket)
    )
    return {bracket: (int(games), int(wins)) for bracket, games, wins in rows}


@dataclass(frozen=True, slots=True)
class PatchCounts:
    patch: str
    games: int
    wins: int
    partial: bool  # the live patch, or collection started after the patch was released


def patch_trend(session: Session, scope: Scope, hero_id: int, patches: int) -> list[PatchCounts]:
    """The hero's games and wins in the last ``patches`` patches up to the scope's, oldest first."""
    if scope.position is None:
        model, extra = HeroDraftAggregate, []
    else:
        model, extra = HeroAggregate, [HeroAggregate.position == scope.position]
    released = session.scalar(select(Patch.released_at).where(Patch.patch_id == scope.patch_id))
    recent = list(
        session.execute(
            select(Patch.patch_id, Patch.name, Patch.released_at, AggregateStatus.first_match_at)
            .join(AggregateStatus, AggregateStatus.patch_id == Patch.patch_id)
            .where(Patch.released_at <= released, AggregateStatus.first_match_at.is_not(None))
            .order_by(Patch.released_at.desc())
            .limit(patches)
        )
    )
    counts = dict(
        (patch_id, (int(games), int(wins)))
        for patch_id, games, wins in session.execute(
            select(model.patch_id, func.sum(model.games), func.sum(model.wins))
            .where(
                model.patch_id.in_([r.patch_id for r in recent]),
                model.hero_id == hero_id,
                *_in(model.rank_bracket, scope.brackets),
                *extra,
            )
            .group_by(model.patch_id)
        )
    )
    out = []
    for row in reversed(recent):
        games, wins = counts.get(row.patch_id, (0, 0))
        partial = row.patch_id == scope.patch_id and scope.current
        partial = partial or _utc(row.first_match_at) > _utc(row.released_at) + timedelta(days=1)
        out.append(PatchCounts(row.name, games, wins, partial))
    return out


@dataclass(frozen=True, slots=True)
class ProCounts:
    day: datetime
    rows: dict[int, tuple[int, int, int]]  # hero -> picks, wins, bans


def pro_counts(session: Session) -> ProCounts | None:
    """The latest daily snapshot of OpenDota's pro pick/win/ban counts, if collected."""
    day = session.scalar(select(func.max(HeroStatsDaily.day)).where(HeroStatsDaily.scope == "pro"))
    if day is None:
        return None
    rows = session.execute(
        select(
            HeroStatsDaily.hero_id, HeroStatsDaily.picks, HeroStatsDaily.wins, HeroStatsDaily.bans
        ).where(HeroStatsDaily.scope == "pro", HeroStatsDaily.day == day)
    )
    return ProCounts(
        datetime(day.year, day.month, day.day, tzinfo=UTC),
        {hero: (picks, wins, bans or 0) for hero, picks, wins, bans in rows},
    )


def matchup_counts(
    session: Session, scope: Scope, hero_ids: Sequence[int] | None = None
) -> dict[tuple[MatchupRelation, int, int], Counts]:
    """(relation, hero, other) -> games and wins of ``hero``, for pairs involving ``hero_ids``."""
    rows = session.execute(
        select(
            HeroMatchupAggregate.relation,
            HeroMatchupAggregate.hero_id,
            HeroMatchupAggregate.other_hero_id,
            func.sum(HeroMatchupAggregate.games),
            func.sum(HeroMatchupAggregate.wins),
        )
        .where(
            HeroMatchupAggregate.patch_id == scope.patch_id,
            *_in(HeroMatchupAggregate.rank_bracket, scope.brackets),
            *_in(HeroMatchupAggregate.hero_id, hero_ids),
        )
        .group_by(
            HeroMatchupAggregate.relation,
            HeroMatchupAggregate.hero_id,
            HeroMatchupAggregate.other_hero_id,
        )
    )
    return {
        (MatchupRelation(relation), hero, other): (int(games), int(wins))
        for relation, hero, other, games, wins in rows
    }


@dataclass(frozen=True, slots=True)
class HeroAverages:
    games: int
    wins: int
    kills: float
    deaths: float
    assists: float
    gpm: float
    xpm: float
    last_hits: float
    net_worth: float
    duration_minutes: float


def hero_averages(
    session: Session, scope: Scope, hero_ids: Sequence[int]
) -> dict[int, HeroAverages]:
    """Per-game averages from enriched matches, at the scope's position if it has one."""
    sums = ("sum_kills", "sum_deaths", "sum_assists", "sum_gpm", "sum_xpm", "sum_last_hits",
            "sum_net_worth", "sum_duration")  # fmt: skip
    rows = session.execute(
        select(
            HeroAggregate.hero_id,
            func.sum(HeroAggregate.games),
            func.sum(HeroAggregate.wins),
            *(func.sum(getattr(HeroAggregate, s)) for s in sums),
        )
        .where(
            HeroAggregate.patch_id == scope.patch_id,
            HeroAggregate.hero_id.in_(list(hero_ids)),
            *_in(HeroAggregate.rank_bracket, scope.brackets),
            *_in(HeroAggregate.position, None if scope.position is None else [scope.position]),
        )
        .group_by(HeroAggregate.hero_id)
    )
    out = {}
    for hero, games, wins, *values in rows:
        if not games:
            continue
        kills, deaths, assists, gpm, xpm, last_hits, net_worth, duration = (
            float(v) / games for v in values
        )
        out[hero] = HeroAverages(
            int(games), int(wins), kills, deaths, assists, gpm, xpm, last_hits, net_worth,
            duration / 60,
        )  # fmt: skip
    return out


def item_timings(
    session: Session, scope: Scope, item_ids: Sequence[int], hero_ids: Sequence[int] | None
) -> dict[int, dict[int, list[tuple[int, int, int]]]]:
    """hero -> item -> sorted (minute, games, wins) of first purchases, from enriched matches.

    Minute -1 is a purchase before the horn.
    """
    rows = session.execute(
        select(
            ItemTimingAggregate.hero_id,
            ItemTimingAggregate.item_id,
            ItemTimingAggregate.minute,
            func.sum(ItemTimingAggregate.games),
            func.sum(ItemTimingAggregate.wins),
        )
        .where(
            ItemTimingAggregate.patch_id == scope.patch_id,
            ItemTimingAggregate.item_id.in_(list(item_ids)),
            *_in(ItemTimingAggregate.hero_id, hero_ids),
            *_in(ItemTimingAggregate.rank_bracket, scope.brackets),
            *_in(
                ItemTimingAggregate.position, None if scope.position is None else [scope.position]
            ),
        )
        .group_by(
            ItemTimingAggregate.hero_id, ItemTimingAggregate.item_id, ItemTimingAggregate.minute
        )
        .order_by(ItemTimingAggregate.minute)
    )
    out: dict[int, dict[int, list[tuple[int, int, int]]]] = defaultdict(lambda: defaultdict(list))
    for hero, item, minute, games, wins in rows:
        out[hero][item].append((int(minute), int(games), int(wins)))
    return {hero: dict(items) for hero, items in out.items()}


def detail_hero_counts(
    session: Session, scope: Scope, hero_ids: Sequence[int] | None
) -> dict[int, Counts]:
    """Per-hero games and wins in enriched matches, every position or the scope's."""
    rows = session.execute(
        select(HeroAggregate.hero_id, func.sum(HeroAggregate.games), func.sum(HeroAggregate.wins))
        .where(
            HeroAggregate.patch_id == scope.patch_id,
            *_in(HeroAggregate.hero_id, hero_ids),
            *_in(HeroAggregate.rank_bracket, scope.brackets),
            *_in(HeroAggregate.position, None if scope.position is None else [scope.position]),
        )
        .group_by(HeroAggregate.hero_id)
    )
    return {hero: (int(games), int(wins)) for hero, games, wins in rows}


def stat_histogram(
    session: Session,
    scope: Scope,
    hero_id: int,
    stat: HistogramStat,
    positions: Sequence[int] | None = None,
) -> dict[int, int]:
    """Bucket lower bound -> games for one hero's GPM, XPM or net worth."""
    if positions is None and scope.position is not None:
        positions = [scope.position]
    rows = session.execute(
        select(HeroStatHistogram.bucket, func.sum(HeroStatHistogram.games))
        .where(
            HeroStatHistogram.patch_id == scope.patch_id,
            HeroStatHistogram.hero_id == hero_id,
            HeroStatHistogram.stat == stat,
            *_in(HeroStatHistogram.rank_bracket, scope.brackets),
            *_in(HeroStatHistogram.position, positions),
        )
        .group_by(HeroStatHistogram.bucket)
    )
    return {int(bucket): int(games) for bucket, games in rows}


def rank_label(bracket: int) -> str:
    """Medal name of a bracket number; 7 is Divine with Immortal folded in."""
    if bracket == TOP_BRACKET:
        return "divine+immortal"
    return list(RankBracket)[bracket - 1].value

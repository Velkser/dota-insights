"""Incremental aggregates (p2-03): hero x position x rank bracket x patch.

Aggregates store additive counters (see docs/architecture/db-schema.md), so new matches are
folded in by adding their deltas with an upsert, never by recomputing a whole patch:

1. **Draft level**, every match with ``aggregated_at IS NULL``: ``patch_bracket_totals.games``,
   ``hero_draft_aggregates`` (games, wins) and ``hero_matchup_aggregates`` (with/vs pairs).
2. **Detail level**, every match with ``has_details AND details_aggregated_at IS NULL``:
   ``patch_bracket_totals.detailed_games`` / ``ban_sample_games``, bans, ``hero_aggregates``
   (sums by position), ``hero_stat_histograms`` (GPM/XPM/net worth percentiles) and
   ``item_timing_aggregates`` (first-purchase minute).

Each batch adds its deltas and stamps the matches in the same transaction, so a crash either
counts a batch fully or not at all. Patches marked stale (see ``patches``) are skipped here and
rebuilt from scratch by ``rebuild_patch``.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Table, delete, select, tuple_, update
from sqlalchemy.orm import Session

from dota_insights.collector.db import upsert, utc_now
from dota_insights.collector.ingest import bracket_of
from dota_insights.collector.patches import mark_fresh, stale_patches
from dota_insights.db.models import (
    HISTOGRAM_BUCKET_WIDTH,
    EventType,
    HeroAggregate,
    HeroDraftAggregate,
    HeroMatchupAggregate,
    HeroStatHistogram,
    HistogramStat,
    ItemTimingAggregate,
    Match,
    MatchEvent,
    MatchPlayer,
    MatchupRelation,
    PatchBracketTotal,
)

HERO_SUMS = {
    "sum_kills": "kills",
    "sum_deaths": "deaths",
    "sum_assists": "assists",
    "sum_gpm": "gpm",
    "sum_xpm": "xpm",
    "sum_last_hits": "last_hits",
    "sum_net_worth": "net_worth",
    "sum_hero_damage": "hero_damage",
    "sum_tower_damage": "tower_damage",
    "sum_healing": "healing",
}
HISTOGRAM_SOURCES = {
    HistogramStat.GPM: "gpm",
    HistogramStat.XPM: "xpm",
    HistogramStat.NET_WORTH: "net_worth",
}

Key = tuple
Counters = dict[str, int]


@dataclass
class Deltas:
    """Counter increments per aggregate table, keyed by that table's primary key."""

    tables: dict[str, dict[Key, Counters]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    )

    def add(self, table: str, key: Key, **counters: int) -> None:
        row = self.tables[table][key]
        for name, value in counters.items():
            row[name] += value


# Table name -> (model table, key columns, counter columns).
TABLES: dict[str, tuple[Table, tuple[str, ...], tuple[str, ...]]] = {
    "totals": (
        PatchBracketTotal.__table__,
        ("patch_id", "rank_bracket"),
        ("games", "wins", "detailed_games", "ban_sample_games"),
    ),
    "draft": (
        HeroDraftAggregate.__table__,
        ("patch_id", "rank_bracket", "hero_id"),
        ("games", "wins", "bans"),
    ),
    "matchups": (
        HeroMatchupAggregate.__table__,
        ("patch_id", "rank_bracket", "hero_id", "relation", "other_hero_id"),
        ("games", "wins"),
    ),
    "heroes": (
        HeroAggregate.__table__,
        ("patch_id", "rank_bracket", "position", "hero_id"),
        ("games", "wins", "sum_duration", *HERO_SUMS),
    ),
    "histograms": (
        HeroStatHistogram.__table__,
        ("patch_id", "rank_bracket", "position", "hero_id", "stat", "bucket"),
        ("games", "wins"),
    ),
    "item_timings": (
        ItemTimingAggregate.__table__,
        ("patch_id", "rank_bracket", "hero_id", "position", "item_id", "minute"),
        ("games", "wins"),
    ),
}


def draft_deltas(matches: Sequence[Any], players: Iterable[Any]) -> Deltas:
    """Deltas from match headers and their draft rows (hero, side, win)."""
    deltas = Deltas()
    by_match: dict[int, list[Any]] = defaultdict(list)
    for player in players:
        by_match[player.match_id].append(player)
    for match in matches:
        bracket = bracket_of(match.avg_rank_tier)
        deltas.add("totals", (match.patch_id, bracket), games=1, wins=int(match.radiant_win))
        team = by_match.get(match.match_id, [])
        for player in team:
            deltas.add(
                "draft", (match.patch_id, bracket, player.hero_id), games=1, wins=int(player.win)
            )
            for other in team:
                if other.hero_id == player.hero_id:
                    continue
                relation = (
                    MatchupRelation.WITH
                    if other.is_radiant == player.is_radiant
                    else MatchupRelation.VS
                )
                deltas.add(
                    "matchups",
                    (match.patch_id, bracket, player.hero_id, relation, other.hero_id),
                    games=1,
                    wins=int(player.win),
                )
    return deltas


def detail_deltas(
    matches: Sequence[Any], players: Iterable[Any], purchases: Iterable[Any]
) -> Deltas:
    """Deltas from enriched matches: per-position sums, histograms, bans and item timings."""
    deltas = Deltas()
    by_key = {match.match_id: match for match in matches}
    player_of: dict[tuple[int, int], Any] = {}
    for match in matches:
        bracket = bracket_of(match.avg_rank_tier)
        known_bans = match.banned_hero_ids is not None
        deltas.add(
            "totals",
            (match.patch_id, bracket),
            detailed_games=1,
            ban_sample_games=int(known_bans),
        )
        for hero_id in match.banned_hero_ids or ():
            deltas.add("draft", (match.patch_id, bracket, hero_id), bans=1)

    for player in players:
        match = by_key[player.match_id]
        player_of[(player.match_id, player.slot)] = player
        bracket = bracket_of(match.avg_rank_tier)
        position = player.position or 0
        sums = {column: getattr(player, source) or 0 for column, source in HERO_SUMS.items()}
        deltas.add(
            "heroes",
            (match.patch_id, bracket, position, player.hero_id),
            games=1,
            wins=int(player.win),
            sum_duration=match.duration,
            **sums,
        )
        for stat, source in HISTOGRAM_SOURCES.items():
            value = getattr(player, source)
            if value is None:
                continue
            width = HISTOGRAM_BUCKET_WIDTH[stat]
            deltas.add(
                "histograms",
                (match.patch_id, bracket, position, player.hero_id, stat, value // width * width),
                games=1,
                wins=int(player.win),
            )

    first_purchase: dict[tuple[int, int, int], int] = {}
    for event in purchases:
        key = (event.match_id, event.slot, event.item_id)
        if key not in first_purchase or event.time < first_purchase[key]:
            first_purchase[key] = event.time
    for (match_id, slot, item_id), time in first_purchase.items():
        player = player_of.get((match_id, slot))
        if player is None:
            continue
        match = by_key[match_id]
        minute = time // 60 if time >= 0 else -1
        deltas.add(
            "item_timings",
            (
                match.patch_id,
                bracket_of(match.avg_rank_tier),
                player.hero_id,
                player.position or 0,
                item_id,
                minute,
            ),
            games=1,
            wins=int(player.win),
        )
    return deltas


def apply_deltas(session: Session, deltas: Deltas) -> int:
    """Add every delta to its aggregate row; return the number of rows touched."""
    touched = 0
    for name, rows in deltas.tables.items():
        table, keys, counters = TABLES[name]
        values = []
        for key, row in rows.items():
            values.append(dict(zip(keys, key, strict=True)) | {c: row.get(c, 0) for c in counters})
        upsert(session, table, values, keys, add=counters, touch="updated_at")
        touched += len(values)
    return touched


@dataclass
class AggregateResult:
    draft_matches: int = 0
    detail_matches: int = 0
    rebuilt_patches: list[int] = field(default_factory=list)


def _pending(session: Session, column, extra, patch_ids, exclude, limit: int) -> list[Any]:
    query = select(
        Match.match_id,
        Match.start_time,
        Match.patch_id,
        Match.avg_rank_tier,
        Match.radiant_win,
        Match.duration,
        Match.banned_hero_ids,
    ).where(column.is_(None), *extra)
    if patch_ids is not None:
        query = query.where(Match.patch_id.in_(patch_ids))
    if exclude:
        query = query.where(Match.patch_id.not_in(exclude))
    return list(session.execute(query.order_by(Match.start_time).limit(limit)))


def _keys(matches: Sequence[Any]):
    return [(m.match_id, m.start_time) for m in matches]


def _stamp(session: Session, matches: Sequence[Any], column: str) -> None:
    session.execute(
        update(Match)
        .where(tuple_(Match.match_id, Match.start_time).in_(_keys(matches)))
        .values({column: utc_now()}),
        execution_options={"synchronize_session": False},
    )


def aggregate_batch(
    session: Session,
    batch_size: int,
    *,
    patch_ids: Sequence[int] | None = None,
    exclude: Sequence[int] = (),
) -> AggregateResult:
    """Fold one batch of not-yet-aggregated matches into the aggregates."""
    result = AggregateResult()
    matches = _pending(session, Match.aggregated_at, (), patch_ids, exclude, batch_size)
    if matches:
        players = session.execute(
            select(
                MatchPlayer.match_id, MatchPlayer.hero_id, MatchPlayer.is_radiant, MatchPlayer.win
            ).where(tuple_(MatchPlayer.match_id, MatchPlayer.start_time).in_(_keys(matches)))
        )
        apply_deltas(session, draft_deltas(matches, players))
        _stamp(session, matches, "aggregated_at")
        result.draft_matches = len(matches)

    matches = _pending(
        session, Match.details_aggregated_at, (Match.has_details,), patch_ids, exclude, batch_size
    )
    if matches:
        keys = _keys(matches)
        players = session.execute(
            select(MatchPlayer).where(
                tuple_(MatchPlayer.match_id, MatchPlayer.start_time).in_(keys)
            )
        ).scalars()
        purchases = session.execute(
            select(MatchEvent.match_id, MatchEvent.slot, MatchEvent.item_id, MatchEvent.time).where(
                tuple_(MatchEvent.match_id, MatchEvent.start_time).in_(keys),
                MatchEvent.event_type == EventType.PURCHASE,
                MatchEvent.item_id.is_not(None),
            )
        )
        apply_deltas(session, detail_deltas(matches, list(players), purchases))
        _stamp(session, matches, "details_aggregated_at")
        result.detail_matches = len(matches)
    return result


def rebuild_patch(session: Session, patch_id: int, batch_size: int) -> AggregateResult:
    """Drop one patch's aggregates, re-fold all its matches and mark it fresh."""
    for table, _, _ in TABLES.values():
        session.execute(delete(table).where(table.c.patch_id == patch_id))
    session.execute(
        update(Match)
        .where(Match.patch_id == patch_id)
        .values(aggregated_at=None, details_aggregated_at=None),
        execution_options={"synchronize_session": False},
    )
    total = AggregateResult(rebuilt_patches=[patch_id])
    while True:
        batch = aggregate_batch(session, batch_size, patch_ids=[patch_id])
        if not batch.draft_matches and not batch.detail_matches:
            break
        total.draft_matches += batch.draft_matches
        total.detail_matches += batch.detail_matches
    mark_fresh(session, patch_id)
    return total


def run_aggregation(
    session_factory,
    batch_size: int,
    max_batches: int = 50,
    should_stop: Callable[[], bool] = lambda: False,
) -> AggregateResult:
    """Rebuild stale patches, then fold pending matches of fresh patches, one batch per commit.

    ``should_stop`` is checked between patches and batches; whatever is left stays pending for
    the next run.
    """
    total = AggregateResult()
    with session_factory.begin() as session:
        stale = stale_patches(session)
    for patch_id in stale:
        if should_stop():
            return total
        with session_factory.begin() as session:
            rebuilt = rebuild_patch(session, patch_id, batch_size)
        total.rebuilt_patches.append(patch_id)
        total.draft_matches += rebuilt.draft_matches
        total.detail_matches += rebuilt.detail_matches
    for _ in range(max_batches):
        if should_stop():
            break
        with session_factory.begin() as session:
            # Re-read: a patch job may have marked more patches stale since the rebuilds.
            batch = aggregate_batch(session, batch_size, exclude=stale_patches(session))
        total.draft_matches += batch.draft_matches
        total.detail_matches += batch.detail_matches
        if not batch.draft_matches and not batch.detail_matches:
            break
    return total

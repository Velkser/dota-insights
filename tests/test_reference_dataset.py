"""Reference dataset (p2-10): a frozen slice of one patch with hand-checked expected values.

``fixtures/reference/patch_7_41_slice.json`` holds 24 ranked matches of patch 7.41, built by hand
so every number below can be worked out on paper:

- Phantom Assassin (44) is Radiant carry (slot 0) in matches 0-19, Anti-Mage (1) in 20-23.
- Axe (2) is Dire offlaner in matches 0-9, Mars (129) in 10-23.
- Radiant wins matches 0-6, 10-15, 20 and 22. So PA wins 13 of 20: 7 of the 10 against Axe and
  6 of the other 10; Axe wins 3 of 10.
- Even matches are Legend lobbies (avg rank 55), odd ones Divine (72): PA is 7-3 in Legend
  (wins in 0, 2, 4, 6, 10, 12, 14) and 6-4 in Divine.
- PA's GPM in match i is 500 + 10·i (500..690); everyone else has 400.
- PA buys BKB (116) in matches 0-14 at minute 14 + i, 30 s into the minute (14:30..28:30), and a
  second BKB at 40:00 in match 0, which first-purchase timings must ignore.

The matches go through the collector's own aggregation into SQLite, and the tools' database
readers and analytics functions run on the result, so a change to a metric definition anywhere in
that chain (bracket mapping, histogram buckets, percentile interpolation, Wilson interval, log5
matchup advantage, first-purchase minute, median interval, Fisher test) fails here. The expected
values are written as literals with the arithmetic beside them; do not regenerate them from code.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from dota_insights.analytics import analysis as an
from dota_insights.analytics import heroes, items, matchups
from dota_insights.analytics.farming import _db_benchmarks, place_value
from dota_insights.analytics.stats import wilson_interval
from dota_insights.collector.aggregates import run_aggregation
from dota_insights.collector.patches import sync_patches
from dota_insights.db import readers
from dota_insights.db.models import (
    Base,
    EventType,
    Match,
    MatchEvent,
    MatchPlayer,
    MatchSource,
)
from dota_insights.models import Filters

SLICE = json.loads(
    (Path(__file__).parent / "fixtures" / "reference" / "patch_7_41_slice.json").read_text()
)
# A later patch keeps 7.41 from being the live patch, whose aggregates must be under 24 h old.
PATCHES = [SLICE["patch"], {"id": 61, "name": "7.42", "date": "2026-12-01T00:00:00Z"}]
PA, AM, AXE, MARS = 44, 1, 2, 129
BKB = 116
F741 = Filters(patch="7.41")


def _naive(ts: str) -> datetime:
    # SQLite stores timestamps naive (in UTC).
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    path = tmp_path_factory.mktemp("reference") / "reference.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    Base.metadata.create_all(engine)
    make = sessionmaker(engine, expire_on_commit=False)
    with make.begin() as session:
        sync_patches(session, PATCHES)
        for m in SLICE["matches"]:
            start = _naive(m["start_time"])
            session.add(
                Match(
                    match_id=m["match_id"],
                    start_time=start,
                    duration=m["duration"],
                    patch_id=SLICE["patch"]["id"],
                    game_mode=22,
                    lobby_type=7,
                    avg_rank_tier=m["avg_rank_tier"],
                    radiant_win=m["radiant_win"],
                    source=MatchSource.OPENDOTA,
                    has_details=True,
                )
            )
            for p in m["players"]:
                session.add(
                    MatchPlayer(
                        match_id=m["match_id"],
                        start_time=start,
                        patch_id=SLICE["patch"]["id"],
                        win=p["is_radiant"] == m["radiant_win"],
                        **p,
                    )
                )
            for seq, e in enumerate(m["purchases"]):
                session.add(
                    MatchEvent(
                        match_id=m["match_id"],
                        start_time=start,
                        seq=seq,
                        event_type=EventType.PURCHASE,
                        payload={},
                        **e,
                    )
                )
    run_aggregation(make, batch_size=1000)
    yield make
    engine.dispose()


@pytest.fixture
def session(factory):
    with factory() as s:
        yield s


def _scope(session: Session, filters: Filters = F741) -> readers.Scope:
    scope = readers.scope_for(session, filters)
    assert isinstance(scope, readers.Scope), scope
    return scope


def test_slice_shape():
    """Guards the fixture itself: if it changes, every expected value below must be rechecked."""
    matches = SLICE["matches"]
    assert len(matches) == 24
    assert sum(m["radiant_win"] for m in matches) == 15
    assert all(len({p["hero_id"] for p in m["players"]}) == 10 for m in matches)


# --- win and pick rates (heroes) ---------------------------------------------------------------


def test_win_rate_with_wilson_interval(session):
    pool = heroes._db_pool(session, F741)
    assert pool is not None and pool.total_matches == 24
    rate = heroes.hero_rate(pool, PA)
    assert (rate.matches, rate.wins) == (20, 13)
    assert rate.win_rate == pytest.approx(0.65)
    # Wilson, n = 20, p̂ = 0.65, z = 1.959964 (z² = 3.841459):
    #   denom  = 1 + z²/n                      = 1.192073
    #   centre = (0.65 + z²/2n) / denom        = 0.746036 / 1.192073 = 0.625833
    #   margin = z·√(p̂q̂/n + z²/4n²) / denom = 1.959964 · √0.013776 / 1.192073 = 0.192977
    assert rate.win_rate_ci[0] == pytest.approx(0.432856, abs=1e-5)
    assert rate.win_rate_ci[1] == pytest.approx(0.818810, abs=1e-5)
    # Pick rate 20 / 24 = 0.8333 (PA is in 20 of the 24 matches); Wilson with z²/n = 0.160061:
    #   centre = (0.833333 + 0.080030) / 1.160061 = 0.787342
    #   margin = 1.959964 · √(0.005787 + 0.001667) / 1.160061 = 0.145870
    assert rate.pick_rate == pytest.approx(20 / 24)
    assert rate.pick_rate_ci[0] == pytest.approx(0.641472, abs=1e-5)
    assert rate.pick_rate_ci[1] == pytest.approx(0.933212, abs=1e-5)


def test_wilson_matches_reference_table():
    # Published reference values (Newcombe 1998, table I, method 3): 15/148 and 0/20.
    low, high = wilson_interval(15, 148)
    assert (round(low, 4), round(high, 4)) == (0.0624, 0.1605)
    assert wilson_interval(0, 20) == (0.0, pytest.approx(0.1611, abs=1e-4))


def test_rank_bracket_split(session):
    """avg_rank_tier 55 -> Legend (5) and 72 -> Divine (7), by tens."""
    legend = readers.hero_counts(
        session, _scope(session, Filters(patch="7.41", rank_bracket="legend"))
    )
    divine = readers.hero_counts(
        session, _scope(session, Filters(patch="7.41", rank_bracket="divine"))
    )
    assert legend[PA] == (10, 7)
    assert divine[PA] == (10, 6)
    assert legend[AXE] == (5, 1)  # Axe wins matches 7, 8 and 9; only 8 is a Legend lobby


# --- GPM percentiles (farming benchmarks) ------------------------------------------------------


def test_gpm_percentiles_from_histogram(session):
    found = _db_benchmarks(session, PA, F741)
    assert found is not None and found.sample_size == 20
    gpm = {p.percentile: p.value for p in found.metrics["gold_per_min"]}
    # GPM 500..690 in 25-wide buckets: [500,525) 3, [525,550) 2, [550,575) 3, [575,600) 2,
    # [600,625) 3, [625,650) 2, [650,675) 3, [675,700) 2. Cumulative 3, 5, 8, 10, 13, 15, 18, 20.
    # Quantile q: target = 20q, interpolated linearly inside the bucket that reaches it.
    assert gpm[0.5] == 600.0  # target 10: 575 + 25 · (10 - 8)/2
    assert gpm[0.1] == pytest.approx(516.7, abs=0.05)  # target 2: 500 + 25 · 2/3
    assert gpm[0.9] == 675.0  # target 18: 650 + 25 · (18 - 15)/3
    assert gpm[0.99] == 697.5  # target 19.8: 675 + 25 · (19.8 - 18)/2
    # A 640 GPM game sits between p70 (target 14 -> 637.5) and p80 (target 16 -> 658.3).
    pct, label = place_value(found.metrics["gold_per_min"], 640)
    assert label == "p71"
    assert pct == pytest.approx(0.7 + 0.1 * (640 - 637.5) / (658.3 - 637.5))


# --- item timing (items) -----------------------------------------------------------------------


def test_bkb_first_purchase_minute(session):
    timings = readers.item_timings(session, _scope(session), [BKB], [PA])[PA][BKB]
    # 15 first purchases at minutes 14..28 (14:30 -> 14), one each; the second BKB is dropped.
    assert [(m, g) for m, g, _ in timings] == [(m, 1) for m in range(14, 29)]
    rows = [items.TimingRow(m, g, w) for m, g, w in timings]
    median, (low, high) = items.median_minute(rows)
    # n = 15: median = 8th = minute 21. Interval ranks: half = 1.96·√15/2 = 3.795,
    # j = ⌊7.5 - 3.795⌋ = 3 -> minute 16, k = ⌈7.5 + 3.795⌉ + 1 = 13 -> minute 26.
    assert (median, low, high) == (21.0, 16.0, 26.0)
    # Wins by minute: matches 0-6 won (minutes 14-20), 7-9 lost (21-23), 10-14 won (24-28).
    assert items.totals(rows) == (15, 12)
    # Default quartile edges: ranks ⌈15·q⌉ = 4, 8, 12 -> minutes 17, 21, 25.
    assert items.quartile_boundaries(rows) == [17, 21, 25]


# --- matchup delta (matchups) ------------------------------------------------------------------


def test_matchup_advantage_log5(session):
    data = matchups._db_matchups(session, F741, (PA,))
    assert data is not None
    pair = data.against(PA, AXE)
    assert (pair.games, pair.wins) == (10, 7)
    assert (pair.base_hero, pair.base_other) == (0.65, 0.3)
    # log5 expected = a(1-b) / (a(1-b) + b(1-a)) = 0.455 / (0.455 + 0.105) = 0.8125;
    # advantage = 0.7 - 0.8125 = -0.1125: PA beats Axe, but by less than their base rates predict.
    assert matchups.matchup_advantage(pair) == pytest.approx(-0.1125)
    # Seen from Axe's side, the same games: 3/10 against expected 0.1875 -> +0.1125.
    assert matchups.matchup_advantage(pair.flipped()) == pytest.approx(0.1125)
    # With: PA and Juggernaut (8) share all 20 games, so their pair win rate equals PA's.
    with_jugg = data.alongside(PA, 8)
    assert (with_jugg.games, with_jugg.wins) == (20, 13)


# --- hypothesis test (analysis) ----------------------------------------------------------------


def _explorer_rows() -> list[dict]:
    """PA's rows as ``player_rows_sql`` returns them for the BKB question."""
    rows = []
    for m in SLICE["matches"]:
        pa = next((p for p in m["players"] if p["hero_id"] == PA), None)
        if pa is None:
            continue
        bkb = [e["time"] for e in m["purchases"] if e["slot"] == 0 and e["item_id"] == BKB]
        rows.append(
            {
                "match_id": m["match_id"],
                "player_slot": pa["slot"],
                "radiant_win": m["radiant_win"],
                "start_time": 0,
                "duration": m["duration"],
                "gold_per_min": pa["gpm"],
                "t_black_king_bar": min(bkb) if bkb else None,
            }
        )
    return rows


def test_bkb_hypothesis_fisher(session):
    early = [
        an.resolve_condition(an.Condition(field="item_minute", op="<", value=20), "black_king_bar")
    ]
    late = [
        an.resolve_condition(an.Condition(field="item_minute", op=">=", value=20), "black_king_bar")
    ]
    a, b, overlap = an.split_groups(_explorer_rows(), early, late)
    # BKB before 20:00 in matches 0-5 (all won), from 20:30 on in 6-14 (6 of 9 won); 15-19 none.
    assert (len(a), len(b), overlap) == (6, 9, 0)
    test = an.compare_groups(a, b, "win")
    # Expected losses in A: 6 · 3/15 = 1.2 < 5 -> Fisher exact. Hypergeometric over A's losses
    # x = 0..3: C(12, 6-x)·C(3, x) / C(15, 6) = 924, 2376, 1485, 220 (/5005). Two-sided p sums
    # the tables no more likely than the observed x = 0: (924 + 220) / 5005 = 0.228571.
    assert test["test"] == "fisher_exact"
    assert test["p_value"] == pytest.approx(1144 / 5005)
    assert test["effect_size"] == pytest.approx(1 - 6 / 9)
    # GPM outcome: A = 500..550, B = 560..640; every A value is below every B value, so U = 0
    # and rank-biserial r = 2U/(n₁n₂) - 1 = -1.
    gpm = an.compare_groups(a, b, "gold_per_min")
    assert gpm["effect_size"] == pytest.approx(525 - 600)
    assert gpm["standardized_effect"] == pytest.approx(-1.0)


def test_unused_heroes_are_not_invented(session):
    counts = readers.hero_counts(session, _scope(session))
    assert counts[AM] == (4, 2)  # matches 20 and 22
    assert counts[MARS] == (14, 6)  # Dire wins in 16-19, 21, 23

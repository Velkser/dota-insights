"""Week-over-week meta changes: risers and fallers per bracket, and statistically notable shifts.

Win rate is wins / games; pick rate is games / matches in the bracket (a hero is picked at most
once per match, so it is a share of matches). Heroes need ``min_games`` in **both** weeks.

Each hero x bracket gets two pooled two-proportion z-tests (``analytics.stats``): wins out of
games, and games out of matches, this week against the previous one. An anomaly is a test whose
Benjamini-Hochberg q-value over all tests of the report is at most ``alpha`` and whose change is
at least ``min_wr_delta`` / ``min_pr_delta``, so that a large sample alone does not make a
0.3-point wobble a post.
"""

from dataclasses import dataclass
from typing import Literal

from dota_insights.analytics.stats import benjamini_hochberg, two_proportion_ztest
from dota_insights.insights.data import ALL_RANKS, BRACKETS, WeekCounts

Metric = Literal["winrate", "pickrate"]

DEFAULT_MIN_GAMES = 200
DEFAULT_TOP = 5
DEFAULT_ALPHA = 0.05
DEFAULT_MIN_WR_DELTA = 0.015
DEFAULT_MIN_PR_DELTA = 0.01
DEFAULT_MAX_ANOMALIES = 10


@dataclass(frozen=True, slots=True)
class HeroChange:
    bracket: int
    hero_id: int
    games_before: int
    wins_before: int
    matches_before: int
    games_after: int
    wins_after: int
    matches_after: int

    @property
    def winrate_before(self) -> float:
        return self.wins_before / self.games_before

    @property
    def winrate_after(self) -> float:
        return self.wins_after / self.games_after

    @property
    def pickrate_before(self) -> float:
        return self.games_before / self.matches_before

    @property
    def pickrate_after(self) -> float:
        return self.games_after / self.matches_after

    def before(self, metric: Metric) -> float:
        return self.winrate_before if metric == "winrate" else self.pickrate_before

    def after(self, metric: Metric) -> float:
        return self.winrate_after if metric == "winrate" else self.pickrate_after

    def delta(self, metric: Metric) -> float:
        return self.after(metric) - self.before(metric)

    def ztest(self, metric: Metric) -> tuple[float, float]:
        if metric == "winrate":
            args = (self.wins_after, self.games_after, self.wins_before, self.games_before)
        else:
            args = (self.games_after, self.matches_after, self.games_before, self.matches_before)
        return tuple(two_proportion_ztest(*args))


@dataclass(frozen=True, slots=True)
class Movers:
    """Top changes of one bracket, largest first; ``up`` only rises, ``down`` only falls."""

    bracket: int
    matches_before: int
    matches_after: int
    heroes: int  # heroes with enough games in both weeks
    winrate_up: list[HeroChange]
    winrate_down: list[HeroChange]
    pickrate_up: list[HeroChange]
    pickrate_down: list[HeroChange]


@dataclass(frozen=True, slots=True)
class Anomaly:
    change: HeroChange
    metric: Metric
    z: float
    p_value: float
    q_value: float


@dataclass(frozen=True, slots=True)
class MetaReport:
    before: WeekCounts
    after: WeekCounts
    min_games: int
    changes: list[HeroChange]
    movers: list[Movers]  # ALL_RANKS first, then brackets with any eligible hero
    anomalies: list[Anomaly]

    @property
    def patch(self) -> str | None:
        return self.after.main_patch

    @property
    def patch_changed(self) -> bool:
        """The compared weeks do not share one patch."""
        patches = set(self.before.patches) | set(self.after.patches)
        return len(patches) > 1


def compare(before: WeekCounts, after: WeekCounts, min_games: int) -> list[HeroChange]:
    changes = []
    for (bracket, hero_id), (games_after, wins_after) in after.heroes.items():
        games_before, wins_before = before.heroes.get((bracket, hero_id), (0, 0))
        if games_before < min_games or games_after < min_games:
            continue
        matches_before, matches_after = before.matches[bracket], after.matches[bracket]
        if games_before > matches_before or games_after > matches_after:
            continue  # inconsistent counts (e.g. a partial snapshot); never chart them
        changes.append(
            HeroChange(
                bracket,
                hero_id,
                games_before,
                wins_before,
                matches_before,
                games_after,
                wins_after,
                matches_after,
            )
        )
    return sorted(changes, key=lambda c: (c.bracket, c.hero_id))


def _top(changes: list[HeroChange], metric: Metric, top: int, rising: bool) -> list[HeroChange]:
    sign = 1 if rising else -1
    picked = [c for c in changes if sign * c.delta(metric) > 0]
    return sorted(picked, key=lambda c: -sign * c.delta(metric))[:top]


def movers(
    changes: list[HeroChange], before: WeekCounts, after: WeekCounts, top: int
) -> list[Movers]:
    result = []
    for bracket in (ALL_RANKS, *BRACKETS):
        own = [c for c in changes if c.bracket == bracket]
        if not own:
            continue
        result.append(
            Movers(
                bracket,
                before.matches[bracket],
                after.matches[bracket],
                len(own),
                _top(own, "winrate", top, True),
                _top(own, "winrate", top, False),
                _top(own, "pickrate", top, True),
                _top(own, "pickrate", top, False),
            )
        )
    return result


def detect_anomalies(
    changes: list[HeroChange],
    *,
    alpha: float = DEFAULT_ALPHA,
    min_wr_delta: float = DEFAULT_MIN_WR_DELTA,
    min_pr_delta: float = DEFAULT_MIN_PR_DELTA,
    limit: int = DEFAULT_MAX_ANOMALIES,
) -> list[Anomaly]:
    """Notable changes, strongest first, at most one per hero and metric (its strongest bracket)."""
    tests = [(c, m, *c.ztest(m)) for c in changes for m in ("winrate", "pickrate")]
    if not tests:
        return []
    q_values, rejected = benjamini_hochberg([p for *_, p in tests], alpha)
    minimum = {"winrate": min_wr_delta, "pickrate": min_pr_delta}
    found = [
        Anomaly(c, m, z, p, q)
        for (c, m, z, p), q, reject in zip(tests, q_values, rejected, strict=True)
        if reject and abs(c.delta(m)) >= minimum[m]
    ]
    found.sort(key=lambda a: (-abs(a.z), a.change.bracket, a.change.hero_id, a.metric))
    seen: set[tuple[int, Metric]] = set()
    result = []
    for anomaly in found:
        key = (anomaly.change.hero_id, anomaly.metric)
        if key not in seen:
            seen.add(key)
            result.append(anomaly)
    return result[:limit]


def weekly_report(
    before: WeekCounts,
    after: WeekCounts,
    *,
    min_games: int = DEFAULT_MIN_GAMES,
    top: int = DEFAULT_TOP,
    alpha: float = DEFAULT_ALPHA,
    min_wr_delta: float = DEFAULT_MIN_WR_DELTA,
    min_pr_delta: float = DEFAULT_MIN_PR_DELTA,
    max_anomalies: int = DEFAULT_MAX_ANOMALIES,
) -> MetaReport:
    changes = compare(before, after, min_games)
    return MetaReport(
        before,
        after,
        min_games,
        changes,
        movers(changes, before, after, top),
        detect_anomalies(
            changes,
            alpha=alpha,
            min_wr_delta=min_wr_delta,
            min_pr_delta=min_pr_delta,
            limit=max_anomalies,
        ),
    )

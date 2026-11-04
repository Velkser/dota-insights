"""Statistical analysis over match samples: hypothesis tests, correlations, match search and a
safe structured query builder for the OpenDota explorer.

Sources:

- OpenDota ``/explorer`` (read-only Postgres). ``player_matches`` / ``matches`` hold parsed
  league matches only (pro and amateur leagues) and carry per-player stats, purchase logs and
  ``radiant_gold_adv``. ``public_matches`` holds sampled public matches of the last year with
  hero arrays, duration and ``avg_rank_tier``, but nothing per player.

Every SQL string here is assembled from whitelisted identifiers and values that were parsed into
``int``/``float``/``bool`` or checked text and quoted with ``sql_text``. Nothing a caller types
reaches the SQL verbatim.
"""

import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from statistics import correlation, median
from typing import Any, Literal

from pydantic import BaseModel, Field

from dota_insights.analytics.farming import fetch_explorer
from dota_insights.analytics.general import GAME_MODE_IDS, RANKED_LOBBY
from dota_insights.analytics.stats import Interval, _normal_cdf, wilson_interval
from dota_insights.models import GameMode, RankBracket

# ---------------------------------------------------------------- SQL value safety


class UnsafeValueError(ValueError):
    """A value cannot be written into SQL safely."""


_TEXT_RE = re.compile(r"^[\w .,:()'&!?+#/-]{0,64}$", re.UNICODE)
_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def sql_int(value: Any) -> str:
    """An integer literal. Rejects bools, floats with a fraction and anything non-numeric."""
    if isinstance(value, bool):
        raise UnsafeValueError(f"expected an integer, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise UnsafeValueError(f"expected an integer, got {value!r}")
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,19}", value.strip()):
        value = int(value.strip())
    if not isinstance(value, int):
        raise UnsafeValueError(f"expected an integer, got {value!r}")
    if abs(value) >= 2**63:
        raise UnsafeValueError(f"integer {value} is out of range")
    return str(value)


def sql_float(value: Any) -> str:
    """A finite numeric literal."""
    if isinstance(value, bool):
        raise UnsafeValueError(f"expected a number, got {value!r}")
    if isinstance(value, int):
        return sql_int(value)
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise UnsafeValueError(f"expected a number, got {value!r}") from exc
    if not isinstance(value, float) or not math.isfinite(value):
        raise UnsafeValueError(f"expected a finite number, got {value!r}")
    return repr(value)


def sql_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().upper()
    raise UnsafeValueError(f"expected true or false, got {value!r}")


def sql_text(value: Any) -> str:
    """A quoted string literal for short names (team names, patch names).

    Only letters, digits, spaces and a few punctuation marks are allowed, so backslashes,
    semicolons, comment markers and control characters never reach the SQL; a single quote is
    doubled, which is the standard SQL escape.
    """
    if not isinstance(value, str) or not _TEXT_RE.fullmatch(value) or "--" in value:
        raise UnsafeValueError(
            f"text value {value!r} must be at most 64 letters, digits, spaces or .,:()'&!?+#/-"
        )
    return "'" + value.replace("'", "''") + "'"


def sql_key(value: str) -> str:
    """A quoted internal name such as an item key ("black_king_bar")."""
    if not _KEY_RE.fullmatch(value):
        raise UnsafeValueError(f"{value!r} is not an internal name")
    return f"'{value}'"


def sql_int_array(values: Sequence[Any]) -> str:
    return "ARRAY[" + ",".join(sql_int(v) for v in values) + "]::int[]"


# ---------------------------------------------------------------- explorer access

EXPLORER_TIMEOUT_HINT = (
    "The OpenDota explorer timed out or rejected the query. Narrow it: a newer patch, fewer "
    "conditions, a smaller recent_match_window or a lower limit."
)


def patch_bounds_sql(patch: str) -> str:
    return (
        "SELECT min(match_id) AS lo, max(match_id) AS hi FROM match_patch "
        f"WHERE patch = {sql_text(patch)}"
    )


async def patch_match_ids(patch: str) -> tuple[int, int] | None:
    """Lowest and highest league match id on ``patch``; used as a window for public matches.

    Match ids are one global sequence, so the league ids of a patch bracket its pub ids too.
    """
    rows = await fetch_explorer(patch_bounds_sql(patch))
    if not rows or rows[0].get("lo") is None:
        return None
    return int(rows[0]["lo"]), int(rows[0]["hi"])


def won(row: Mapping[str, Any]) -> bool:
    return (row["player_slot"] < 128) == bool(row["radiant_win"])


def match_period(rows: Sequence[Mapping[str, Any]]) -> tuple[date, date] | None:
    times = [r["start_time"] for r in rows if r.get("start_time")]
    if not times:
        return None
    return _day(min(times)), _day(max(times))


def newest_match(rows: Sequence[Mapping[str, Any]]) -> datetime | None:
    times = [r["start_time"] for r in rows if r.get("start_time")]
    return datetime.fromtimestamp(max(times), UTC) if times else None


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


# ---------------------------------------------------------------- player rows

# Per-player numbers available for parsed league matches: name -> (SQL expression, meaning).
PLAYER_METRICS: dict[str, tuple[str, str]] = {
    "gold_per_min": ("pm.gold_per_min", "Gold per minute over the match."),
    "xp_per_min": ("pm.xp_per_min", "Experience per minute over the match."),
    "kills": ("pm.kills", "Kills."),
    "deaths": ("pm.deaths", "Deaths."),
    "assists": ("pm.assists", "Assists."),
    "last_hits": ("pm.last_hits", "Last hits."),
    "denies": ("pm.denies", "Denies."),
    "hero_damage": ("pm.hero_damage", "Damage dealt to enemy heroes."),
    "tower_damage": ("pm.tower_damage", "Damage dealt to buildings."),
    "hero_healing": ("pm.hero_healing", "Healing done to heroes."),
    "net_worth": ("pm.net_worth", "Net worth at the end of the match."),
    "obs_placed": ("pm.obs_placed", "Observer wards placed."),
    "sen_placed": ("pm.sen_placed", "Sentry wards placed."),
    "camps_stacked": ("pm.camps_stacked", "Neutral camps stacked."),
    "rune_pickups": ("pm.rune_pickups", "Runes picked up."),
    "stuns": ("pm.stuns", "Seconds of stun dealt."),
    "teamfight_participation": (
        "pm.teamfight_participation",
        "Share of the team's teamfights the player took part in, 0..1.",
    ),
    "level": ("pm.level", "Hero level at the end of the match."),
    "lane_role": ("pm.lane_role", "OpenDota lane: 1 safe, 2 mid, 3 off, 4 jungle."),
    "duration_min": ("m.duration / 60.0", "Match duration in minutes."),
}

METRIC_ALIASES: dict[str, str] = {
    "gpm": "gold_per_min",
    "xpm": "xp_per_min",
    "lh": "last_hits",
    "wards": "obs_placed",
    "observers": "obs_placed",
    "observer_wards": "obs_placed",
    "sentries": "sen_placed",
    "sentry_wards": "sen_placed",
    "stacks": "camps_stacked",
    "networth": "net_worth",
    "nw": "net_worth",
    "damage": "hero_damage",
    "healing": "hero_healing",
    "duration": "duration_min",
    "runes": "rune_pickups",
    "teamfight": "teamfight_participation",
}


def metric_name(key: str) -> str | None:
    text = key.strip().lower().replace(" ", "_").replace("-", "_")
    if text in PLAYER_METRICS:
        return text
    return METRIC_ALIASES.get(text)


PLAYER_ROW_LIMIT = 500
ALL_HEROES_ROW_LIMIT = 3000


def player_rows_sql(
    hero_id: int | None, patch: str, item_keys: Sequence[str] = (), limit: int | None = None
) -> str:
    """Newest parsed league player rows on ``patch``, one per player per match.

    With ``hero_id`` the rows are that hero's; without it, every player of the newest matches.
    Each ``item_keys`` entry adds ``t_<key>``: the first purchase time of that item in seconds
    (null if never bought), read from ``purchase_log``.
    """
    if limit is None:
        limit = PLAYER_ROW_LIMIT if hero_id is not None else ALL_HEROES_ROW_LIMIT
    columns = [
        "pm.match_id",
        "pm.player_slot",
        "pm.hero_id",
        "m.radiant_win",
        "m.start_time",
        "m.duration",
    ]
    columns += [f"{expr} AS {name}" for name, (expr, _) in PLAYER_METRICS.items()]
    for key in sorted(set(item_keys)):
        columns.append(
            "(SELECT min((e->>'time')::int) FROM unnest(pm.purchase_log) e "
            f"WHERE e->>'key' = {sql_key(key)}) AS t_{key}"
        )
    where = [f"mp.patch = {sql_text(patch)}", "pm.purchase_log IS NOT NULL"]
    if hero_id is not None:
        where.insert(0, f"pm.hero_id = {sql_int(hero_id)}")
    return (
        f"SELECT {', '.join(columns)} "
        "FROM player_matches pm JOIN matches m USING (match_id) "
        "JOIN match_patch mp USING (match_id) "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY pm.match_id DESC LIMIT {sql_int(limit)}"
    )


# ---------------------------------------------------------------- conditions

Op = Literal["<", "<=", ">", ">=", "==", "!=", "bought", "not_bought"]
SIDE_FIELD = "side"
ITEM_FIELD = "item_minute"
CONDITION_FIELDS = (*PLAYER_METRICS, SIDE_FIELD, ITEM_FIELD)

_COMPARE: dict[str, Callable[[float, float], bool]] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


class Condition(BaseModel):
    """One test on a player row. All conditions of a group must hold."""

    field: str = Field(
        description=(
            "What to test. A per-player metric (gold_per_min, xp_per_min, kills, deaths, "
            "assists, last_hits, denies, hero_damage, tower_damage, hero_healing, net_worth, "
            "obs_placed, sen_placed, camps_stacked, rune_pickups, stuns, "
            "teamfight_participation, level, lane_role, duration_min), 'side' (value "
            "'radiant' or 'dire'), or 'item_minute' (minute the hero first bought `item`)."
        ),
        examples=["item_minute", "gold_per_min", "duration_min"],
    )
    op: Op = Field(
        description=(
            "Comparison. For item_minute also 'bought' / 'not_bought' (no value needed); "
            "a comparison on item_minute is false when the item was never bought."
        )
    )
    value: float | str | None = Field(
        default=None, description="Number to compare with (or 'radiant'/'dire' for side)."
    )
    item: str | None = Field(
        default=None,
        description='Item for item_minute, as a person would type it: "bkb", "Black King Bar".',
    )


@dataclass(frozen=True)
class ResolvedCondition:
    field: str
    op: str
    value: float | str | None
    item_key: str | None = None

    def label(self) -> str:
        subject = f"{self.item_key} minute" if self.item_key else self.field
        if self.op == "bought":
            return f"bought {self.item_key}"
        if self.op == "not_bought":
            return f"did not buy {self.item_key}"
        value = self.value if not isinstance(self.value, float) else f"{self.value:g}"
        return f"{subject} {self.op} {value}"


def resolve_condition(cond: Condition, item_key: str | None) -> ResolvedCondition:
    """Validate a condition. ``item_key`` is the resolved internal item name for item_minute.

    Raises ValueError with a message for the caller.
    """
    field = cond.field.strip().lower()
    if field not in (SIDE_FIELD, ITEM_FIELD):
        field = metric_name(field) or field
    if field not in CONDITION_FIELDS:
        raise ValueError(
            f"Unknown condition field {cond.field!r}; use one of {', '.join(CONDITION_FIELDS)}."
        )
    if cond.op in ("bought", "not_bought"):
        if field != ITEM_FIELD:
            raise ValueError("'bought' and 'not_bought' only apply to item_minute.")
        return ResolvedCondition(field, cond.op, None, item_key)
    if field == ITEM_FIELD and item_key is None:
        raise ValueError("item_minute needs an item.")
    if field == SIDE_FIELD:
        side = str(cond.value).strip().lower()
        if side not in ("radiant", "dire") or cond.op not in ("==", "!="):
            raise ValueError("side takes op '==' or '!=' and value 'radiant' or 'dire'.")
        return ResolvedCondition(field, cond.op, side)
    try:
        number = float(cond.value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{cond.field} {cond.op} needs a numeric value.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{cond.field} {cond.op} needs a finite value.")
    return ResolvedCondition(field, cond.op, number, item_key)


def _value(row: Mapping[str, Any], cond: ResolvedCondition) -> Any:
    if cond.field == ITEM_FIELD:
        t = row.get(f"t_{cond.item_key}")
        return None if t is None else t / 60
    if cond.field == SIDE_FIELD:
        return "radiant" if row["player_slot"] < 128 else "dire"
    return row.get(cond.field)


def holds(row: Mapping[str, Any], cond: ResolvedCondition) -> bool:
    value = _value(row, cond)
    if cond.op == "bought":
        return value is not None
    if cond.op == "not_bought":
        return value is None
    if value is None:
        return False
    if cond.field == SIDE_FIELD:
        return (value == cond.value) == (cond.op == "==")
    return _COMPARE[cond.op](float(value), float(cond.value))  # type: ignore[arg-type]


def select_rows(
    rows: Sequence[Mapping[str, Any]], conditions: Sequence[ResolvedCondition]
) -> list[Mapping[str, Any]]:
    return [r for r in rows if all(holds(r, c) for c in conditions)]


def split_groups(
    rows: Sequence[Mapping[str, Any]],
    group_a: Sequence[ResolvedCondition],
    group_b: Sequence[ResolvedCondition] | None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], int]:
    """Rows of group A and B, and how many rows matched both (dropped from both).

    With ``group_b`` None, B is every row not in A.
    """
    in_a = {id(r) for r in select_rows(rows, group_a)}
    if group_b is None:
        a = [r for r in rows if id(r) in in_a]
        b = [r for r in rows if id(r) not in in_a]
        return a, b, 0
    in_b = {id(r) for r in select_rows(rows, group_b)}
    both = in_a & in_b
    a = [r for r in rows if id(r) in in_a and id(r) not in both]
    b = [r for r in rows if id(r) in in_b and id(r) not in both]
    return a, b, len(both)


# ---------------------------------------------------------------- statistics


def newcombe_diff_ci(s1: int, n1: int, s2: int, n2: int) -> Interval:
    """Newcombe's hybrid score interval for ``p₁ - p₂`` (method 10), from Wilson intervals::

    low  = d - √((p₁ - l₁)² + (u₂ - p₂)²)
    high = d + √((u₁ - p₁)² + (p₂ - l₂)²)
    """
    p1, p2 = s1 / n1, s2 / n2
    l1, u1 = wilson_interval(s1, n1)
    l2, u2 = wilson_interval(s2, n2)
    d = p1 - p2
    return Interval(
        d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
        d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2),
    )


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p-value for the 2x2 table [[a, b], [c, d]].

    Sums the hypergeometric probabilities of every table with the same margins that is no more
    likely than the observed one.
    """
    row1, col1, n = a + b, a + c, a + b + c + d

    def log_p(x: int) -> float:
        return (
            math.lgamma(row1 + 1)
            - math.lgamma(x + 1)
            - math.lgamma(row1 - x + 1)
            + math.lgamma(n - row1 + 1)
            - math.lgamma(col1 - x + 1)
            - math.lgamma(n - row1 - col1 + x + 1)
            - math.lgamma(n + 1)
            + math.lgamma(col1 + 1)
            + math.lgamma(n - col1 + 1)
        )

    lo, hi = max(0, col1 - (n - row1)), min(row1, col1)
    observed = log_p(a)
    total = sum(math.exp(log_p(x)) for x in range(lo, hi + 1) if log_p(x) <= observed + 1e-7)
    return min(1.0, total)


class MannWhitney(BaseModel):
    u: float
    z: float
    p_value: float
    rank_biserial: float


def _ranks(values: Sequence[float]) -> tuple[list[float], float]:
    """Average ranks (1-based) and the tie term Σ(t³ - t)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    ties = 0.0
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        t = j - i + 1
        ties += t**3 - t
        i = j + 1
    return ranks, ties


def mann_whitney(x: Sequence[float], y: Sequence[float]) -> MannWhitney:
    """Two-sided Mann-Whitney U test with tie correction and continuity correction.

    ``U = R₁ - n₁(n₁+1)/2`` with ``R₁`` the rank sum of ``x``;
    ``σ² = n₁n₂/12 · ((n+1) - Σ(t³-t)/(n(n-1)))``; ``z = (U - n₁n₂/2 ∓ 0.5) / σ``.
    Rank-biserial ``r = 2U/(n₁n₂) - 1``: +1 means every x exceeds every y.
    """
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        raise ValueError("both samples must be non-empty")
    ranks, ties = _ranks([*x, *y])
    u = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    n = n1 + n2
    mean = n1 * n2 / 2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return MannWhitney(u=u, z=0.0, p_value=1.0, rank_biserial=0.0)
    diff = u - mean
    z = (diff - math.copysign(0.5, diff)) / math.sqrt(var) if diff else 0.0
    p = min(1.0, 2 * (1 - _normal_cdf(abs(z))))
    return MannWhitney(u=u, z=z, p_value=p, rank_biserial=2 * u / (n1 * n2) - 1)


def bootstrap_median_diff(
    x: Sequence[float], y: Sequence[float], resamples: int = 2000, seed: int = 0
) -> Interval:
    """Percentile bootstrap 95% interval for ``median(x) - median(y)``, resampling each group."""
    rng = random.Random(seed)
    diffs = sorted(
        median(rng.choices(x, k=len(x))) - median(rng.choices(y, k=len(y)))
        for _ in range(resamples)
    )

    def q(p: float) -> float:
        pos = p * (len(diffs) - 1)
        lo, hi = math.floor(pos), math.ceil(pos)
        return diffs[lo] + (diffs[hi] - diffs[lo]) * (pos - lo)

    return Interval(q(0.025), q(0.975))


@dataclass(frozen=True)
class Table2x2:
    """a = exposed & win, b = exposed & loss, c = unexposed & win, d = unexposed & loss."""

    a: int
    b: int
    c: int
    d: int

    @property
    def n(self) -> int:
        return self.a + self.b + self.c + self.d


class MantelHaenszel(BaseModel):
    odds_ratio: float | None
    ci: tuple[float, float] | None
    chi2: float
    p_value: float


def mantel_haenszel(tables: Sequence[Table2x2]) -> MantelHaenszel:
    """Common odds ratio over strata, Robins-Breslow-Greenland CI and the CMH test.

    ``OR = Σ(aᵢdᵢ/nᵢ) / Σ(bᵢcᵢ/nᵢ)``. The CMH statistic with continuity correction is
    ``(|Σ(aᵢ - E[aᵢ])| - 0.5)² / ΣVar(aᵢ)`` with ``E[aᵢ] = (aᵢ+bᵢ)(aᵢ+cᵢ)/nᵢ`` and
    ``Var(aᵢ) = (aᵢ+bᵢ)(cᵢ+dᵢ)(aᵢ+cᵢ)(bᵢ+dᵢ) / (nᵢ²(nᵢ-1))``, compared with χ²(1).
    """
    strata = [t for t in tables if t.n > 1]
    sum_r = sum(t.a * t.d / t.n for t in strata)
    sum_s = sum(t.b * t.c / t.n for t in strata)
    odds = sum_r / sum_s if sum_s else None
    ci = None
    if sum_r and sum_s:
        pr = ps_qr = qs = 0.0
        for t in strata:
            p, q = (t.a + t.d) / t.n, (t.b + t.c) / t.n
            r, s = t.a * t.d / t.n, t.b * t.c / t.n
            pr += p * r
            ps_qr += p * s + q * r
            qs += q * s
        var = pr / (2 * sum_r**2) + ps_qr / (2 * sum_r * sum_s) + qs / (2 * sum_s**2)
        se = math.sqrt(var)
        log_or = math.log(sum_r / sum_s)
        ci = (math.exp(log_or - 1.96 * se), math.exp(log_or + 1.96 * se))
    observed = expected = variance = 0.0
    for t in strata:
        observed += t.a
        expected += (t.a + t.b) * (t.a + t.c) / t.n
        variance += (t.a + t.b) * (t.c + t.d) * (t.a + t.c) * (t.b + t.d) / (t.n**2 * (t.n - 1))
    if variance == 0:
        return MantelHaenszel(odds_ratio=odds, ci=ci, chi2=0.0, p_value=1.0)
    chi2 = max(0.0, abs(observed - expected) - 0.5) ** 2 / variance
    p = min(1.0, 2 * (1 - _normal_cdf(math.sqrt(chi2))))
    return MantelHaenszel(odds_ratio=odds, ci=ci, chi2=chi2, p_value=p)


def point_biserial(values: Sequence[float], wins: Sequence[bool]) -> float | None:
    """Pearson correlation of a metric with a 0/1 win indicator; None if either is constant."""
    if len(values) < 3 or len(set(values)) < 2 or len(set(wins)) < 2:
        return None
    return correlation([float(v) for v in values], [1.0 if w else 0.0 for w in wins])


# ---------------------------------------------------------------- test_hypothesis

ALPHA = 0.05
MIN_GROUP = 20
FISHER_BELOW_EXPECTED = 5


class GroupSummary(BaseModel):
    label: str = Field(description="The conditions that define the group.")
    matches: int
    wins: int
    win_rate: float
    win_rate_ci: tuple[float, float]
    outcome_median: float | None = Field(
        default=None, description="Median of the outcome metric (continuous outcomes only)."
    )
    outcome_mean: float | None = None


class HypothesisResult(BaseModel):
    hero: str | None
    patch: str
    outcome: str = Field(description='"win" or the metric compared between the groups.')
    group_a: GroupSummary
    group_b: GroupSummary
    test: str = Field(description="Statistical test used (see definitions).")
    statistic: float = Field(description="Test statistic: z, χ² or Mann-Whitney z.")
    p_value: float
    effect_size: float = Field(
        description=(
            "Win outcome: win-rate difference A - B. Continuous outcome: median A - median B."
        )
    )
    effect_ci: tuple[float, float] = Field(description="95% interval for effect_size.")
    standardized_effect: float | None = Field(
        default=None,
        description=(
            "Win outcome: Cohen's h. Continuous: rank-biserial correlation (-1..1, + means A "
            "tends higher)."
        ),
    )
    significant: bool = Field(description=f"p_value < {ALPHA}.")
    overlap_dropped: int = Field(description="Matches that met both groups, left out of both.")
    interpretation: str


def compare_groups(
    a: Sequence[Mapping[str, Any]],
    b: Sequence[Mapping[str, Any]],
    outcome: str,
) -> dict[str, Any]:
    """Pick and run the test for ``outcome``; return the test fields of HypothesisResult."""
    if outcome == "win":
        s1, n1 = sum(won(r) for r in a), len(a)
        s2, n2 = sum(won(r) for r in b), len(b)
        p1, p2 = s1 / n1, s2 / n2
        pooled = (s1 + s2) / (n1 + n2)
        expected = min(n1 * pooled, n1 * (1 - pooled), n2 * pooled, n2 * (1 - pooled))
        if expected < FISHER_BELOW_EXPECTED:
            test, stat = "fisher_exact", 0.0
            p = fisher_exact(s1, n1 - s1, s2, n2 - s2)
        else:
            test = "chi_square_2x2"
            se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
            z = (p1 - p2) / se if se else 0.0
            stat = z * z
            p = min(1.0, 2 * (1 - _normal_cdf(abs(z))))
        h = 2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2))
        ci = newcombe_diff_ci(s1, n1, s2, n2)
        return {
            "test": test,
            "statistic": stat,
            "p_value": p,
            "effect_size": p1 - p2,
            "effect_ci": (ci.low, ci.high),
            "standardized_effect": h,
        }
    x = [float(r[outcome]) for r in a if r.get(outcome) is not None]
    y = [float(r[outcome]) for r in b if r.get(outcome) is not None]
    if not x or not y:
        raise ValueError(f"No {outcome} values in one of the groups.")
    mw = mann_whitney(x, y)
    ci = bootstrap_median_diff(x, y)
    return {
        "test": "mann_whitney_u",
        "statistic": mw.z,
        "p_value": mw.p_value,
        "effect_size": median(x) - median(y),
        "effect_ci": (ci.low, ci.high),
        "standardized_effect": mw.rank_biserial,
    }


def summarize_group(rows: Sequence[Mapping[str, Any]], label: str, outcome: str) -> GroupSummary:
    wins = sum(won(r) for r in rows)
    ci = wilson_interval(wins, len(rows))
    values = [float(r[outcome]) for r in rows if outcome != "win" and r.get(outcome) is not None]
    return GroupSummary(
        label=label,
        matches=len(rows),
        wins=wins,
        win_rate=wins / len(rows) if rows else 0.0,
        win_rate_ci=(ci.low, ci.high),
        outcome_median=median(values) if values else None,
        outcome_mean=sum(values) / len(values) if values else None,
    )


def interpret(outcome: str, effect: float, p: float, ci: tuple[float, float]) -> str:
    what = "win rate" if outcome == "win" else f"median {outcome}"
    if outcome == "win":
        size = f"{effect * 100:+.1f} percentage points (95% CI {ci[0] * 100:+.1f} to "
        size += f"{ci[1] * 100:+.1f})"
    else:
        size = f"{effect:+.4g} (95% CI {ci[0]:+.4g} to {ci[1]:+.4g})"
    if p < ALPHA:
        return (
            f"Group A's {what} differs from group B's by {size}; p = {p:.3g} is below "
            f"{ALPHA}, so a difference this large is unlikely to be chance alone. It is an "
            "association, not proof that the condition causes it."
        )
    return (
        f"Group A's {what} differs from group B's by {size}; p = {p:.3g}, so the data does "
        "not show a reliable difference (which is not proof that there is none)."
    )


# ---------------------------------------------------------------- analyze_correlation

# Duration strata in minutes: [0, 25), [25, 35), [35, 45), [45, ∞).
CORRELATION_EDGES = (0, 25, 35, 45)
MIN_STRATUM = 10


class DurationStratum(BaseModel):
    label: str = Field(description='Match-duration range in minutes, e.g. "25-35" or "45+".')
    games: int
    split_at: float = Field(description="Median of the metric in this stratum.")
    high_games: int = Field(description="Games with the metric above split_at.")
    high_win_rate: float | None
    low_games: int = Field(description="Games with the metric at or below split_at.")
    low_win_rate: float | None
    correlation: float | None = Field(description="Point-biserial r of metric and win.")
    used: bool = Field(description="Whether the stratum entered the pooled estimate.")


class CorrelationResult(BaseModel):
    hero: str | None
    patch: str
    metric: str
    games: int
    metric_median: float
    unadjusted_correlation: float | None = Field(
        description="Point-biserial r of metric and win over all games, ignoring duration."
    )
    unadjusted_odds_ratio: float | None = Field(
        description="Odds of winning above vs at-or-below each stratum's median, pooled crudely."
    )
    adjusted_odds_ratio: float | None = Field(
        description="Mantel-Haenszel odds ratio, high vs low half, controlling for duration."
    )
    adjusted_odds_ratio_ci: tuple[float, float] | None
    p_value: float = Field(description="Cochran-Mantel-Haenszel test p-value.")
    strata: list[DurationStratum]
    direction: str = Field(description='"positive", "negative" or "none" (p >= 0.05).')
    interpretation: str


def _stratum_label(i: int) -> str:
    start = CORRELATION_EDGES[i]
    if i + 1 < len(CORRELATION_EDGES):
        end = CORRELATION_EDGES[i + 1]
        return f"<{end}" if start == 0 else f"{start}-{end}"
    return f"{start}+"


def _stratum(minutes: float) -> int:
    index = 0
    for i, edge in enumerate(CORRELATION_EDGES):
        if minutes >= edge:
            index = i
    return index


def correlate_with_win(
    rows: Sequence[Mapping[str, Any]], metric: str
) -> tuple[list[DurationStratum], list[Table2x2], list[float], list[bool]]:
    """Median-split 2x2 tables per duration stratum (high half = exposed)."""
    by_stratum: dict[int, list[tuple[float, bool]]] = {}
    values: list[float] = []
    wins: list[bool] = []
    for row in rows:
        if row.get(metric) is None:
            continue
        v, w = float(row[metric]), won(row)
        values.append(v)
        wins.append(w)
        by_stratum.setdefault(_stratum(row["duration"] / 60), []).append((v, w))
    strata: list[DurationStratum] = []
    tables: list[Table2x2] = []
    for i in range(len(CORRELATION_EDGES)):
        pairs = by_stratum.get(i, [])
        if not pairs:
            continue
        split = median(v for v, _ in pairs)
        high = [w for v, w in pairs if v > split]
        low = [w for v, w in pairs if v <= split]
        used = len(pairs) >= MIN_STRATUM and bool(high) and bool(low)
        if used:
            tables.append(Table2x2(sum(high), len(high) - sum(high), sum(low), len(low) - sum(low)))
        strata.append(
            DurationStratum(
                label=_stratum_label(i),
                games=len(pairs),
                split_at=split,
                high_games=len(high),
                high_win_rate=sum(high) / len(high) if high else None,
                low_games=len(low),
                low_win_rate=sum(low) / len(low) if low else None,
                correlation=point_biserial([v for v, _ in pairs], [w for _, w in pairs]),
                used=used,
            )
        )
    return strata, tables, values, wins


def crude_odds_ratio(tables: Sequence[Table2x2]) -> float | None:
    a = sum(t.a for t in tables)
    b = sum(t.b for t in tables)
    c = sum(t.c for t in tables)
    d = sum(t.d for t in tables)
    return (a * d) / (b * c) if b * c else None


# ---------------------------------------------------------------- find_matches

FIND_MAX_LIMIT = 50
FIND_DEFAULT_LIMIT = 20
# Without a patch, public matches are searched in the newest ids only (~3 days of pubs).
PUB_DEFAULT_WINDOW = 4_000_000

MatchSource = Literal["pub", "league"]


@dataclass(frozen=True)
class MatchSearch:
    """A validated find_matches request; every field is already parsed into ids and numbers."""

    source: MatchSource
    heroes: tuple[int, ...] = ()
    enemy_heroes: tuple[int, ...] = ()
    heroes_win: bool | None = None
    min_duration_s: int | None = None
    max_duration_s: int | None = None
    rank_tiers: tuple[int, int] | None = None
    game_mode: GameMode | None = None
    patch: str | None = None
    patch_ids: tuple[int, int | None] | None = None  # pub match-id window; None high = open
    min_comeback_gold: int | None = None
    before_match_id: int | None = None
    limit: int = FIND_DEFAULT_LIMIT


def rank_tiers(low: RankBracket, high: RankBracket) -> tuple[int, int]:
    """``avg_rank_tier`` bounds for a medal range: tier·10 .. tier·10+9 (Immortal is 80+)."""
    return low.tier * 10, high.tier * 10 + 9


def _team_clause(search: MatchSearch, radiant: str, dire: str, win: str) -> str | None:
    """SQL predicate: heroes on one side, enemy_heroes on the other, optional outcome."""
    if not search.heroes and not search.enemy_heroes:
        return None
    sides = []
    for ours, theirs, our_win in ((radiant, dire, win), (dire, radiant, f"NOT {win}")):
        parts = []
        if search.heroes:
            parts.append(f"{ours} @> {sql_int_array(search.heroes)}")
        if search.enemy_heroes:
            parts.append(f"{theirs} @> {sql_int_array(search.enemy_heroes)}")
        if search.heroes_win is not None and search.heroes:
            parts.append(our_win if search.heroes_win else f"NOT ({our_win})")
        sides.append("(" + " AND ".join(parts) + ")")
    return "(" + " OR ".join(sides) + ")"


def _common_where(search: MatchSearch, prefix: str) -> list[str]:
    where = []
    if search.before_match_id is not None:
        where.append(f"{prefix}match_id < {sql_int(search.before_match_id)}")
    if search.min_duration_s is not None:
        where.append(f"{prefix}duration >= {sql_int(search.min_duration_s)}")
    if search.max_duration_s is not None:
        where.append(f"{prefix}duration <= {sql_int(search.max_duration_s)}")
    if search.game_mode is not None:
        mode, ranked = GAME_MODE_IDS[search.game_mode]
        where.append(f"{prefix}game_mode = {sql_int(mode)}")
        if ranked is not None:
            where.append(f"{prefix}lobby_type {'=' if ranked else '<>'} {RANKED_LOBBY}")
    return where


def find_matches_sql(search: MatchSearch) -> str:
    """Matches fitting ``search``, newest first: ``limit + 1`` rows, to detect a next page."""
    limit = sql_int(min(search.limit, FIND_MAX_LIMIT) + 1)
    if search.source == "pub":
        where = _common_where(search, "")
        if search.patch_ids is not None:
            lo, hi = search.patch_ids
            where.append(f"match_id >= {sql_int(lo)}")
            if hi is not None:
                where.append(f"match_id <= {sql_int(hi)}")
        else:
            where.append(
                f"match_id > (SELECT max(match_id) FROM public_matches) - {PUB_DEFAULT_WINDOW}"
            )
        if search.rank_tiers is not None:
            lo, hi = search.rank_tiers
            where.append(f"avg_rank_tier BETWEEN {sql_int(lo)} AND {sql_int(hi)}")
        team = _team_clause(search, "radiant_team", "dire_team", "radiant_win")
        if team:
            where.append(team)
        return (
            "SELECT match_id, start_time, duration, game_mode, lobby_type, avg_rank_tier, "
            "radiant_team, dire_team, radiant_win FROM public_matches "
            f"WHERE {' AND '.join(where)} ORDER BY match_id DESC LIMIT {limit}"
        )
    deficit = (
        "CASE WHEN m.radiant_win THEN -(SELECT min(x) FROM unnest(m.radiant_gold_adv) x) "
        "ELSE (SELECT max(x) FROM unnest(m.radiant_gold_adv) x) END"
    )
    inner_where = [f"mp.patch = {sql_text(search.patch)}", *_common_where(search, "m.")]
    if search.min_comeback_gold is not None:
        inner_where.append("m.radiant_gold_adv IS NOT NULL")
        inner_where.append(f"{deficit} >= {sql_int(search.min_comeback_gold)}")
    inner = (
        "SELECT m.match_id, m.start_time, m.duration, m.game_mode, m.lobby_type, m.leagueid, "
        "m.radiant_team_name, m.dire_team_name, m.radiant_win, "
        f"{deficit} AS winner_max_deficit, "
        "ARRAY(SELECT p.hero_id FROM player_matches p WHERE p.match_id = m.match_id "
        "AND p.player_slot < 128 ORDER BY p.player_slot) AS radiant_team, "
        "ARRAY(SELECT p.hero_id FROM player_matches p WHERE p.match_id = m.match_id "
        "AND p.player_slot >= 128 ORDER BY p.player_slot) AS dire_team "
        "FROM matches m JOIN match_patch mp USING (match_id) "
        f"WHERE {' AND '.join(inner_where)}"
    )
    team = _team_clause(search, "q.radiant_team", "q.dire_team", "q.radiant_win")
    outer = f" WHERE {team}" if team else ""
    return f"SELECT q.* FROM ({inner}) q{outer} ORDER BY q.match_id DESC LIMIT {limit}"


class FoundMatch(BaseModel):
    match_id: int
    start_time: datetime
    duration_min: float
    radiant_win: bool
    radiant_heroes: list[str]
    dire_heroes: list[str]
    avg_rank_tier: float | None = Field(
        default=None, description="Public matches: average medal tier (e.g. 72 = Ancient 2)."
    )
    radiant_team: str | None = Field(default=None, description="League matches: team name.")
    dire_team: str | None = None
    winner_max_deficit: int | None = Field(
        default=None,
        description="League matches: largest gold deficit the winning team overcame.",
    )
    url: str


def found_match(row: Mapping[str, Any], hero_names: Mapping[int, str]) -> FoundMatch:
    def names(ids: Sequence[int] | None) -> list[str]:
        return [hero_names.get(i, f"hero {i}") for i in ids or []]

    deficit = row.get("winner_max_deficit")
    return FoundMatch(
        match_id=row["match_id"],
        start_time=datetime.fromtimestamp(row["start_time"], UTC),
        duration_min=round(row["duration"] / 60, 1),
        radiant_win=bool(row["radiant_win"]),
        radiant_heroes=names(row.get("radiant_team")),
        dire_heroes=names(row.get("dire_team")),
        avg_rank_tier=row.get("avg_rank_tier"),
        radiant_team=row.get("radiant_team_name"),
        dire_team=row.get("dire_team_name"),
        winner_max_deficit=max(0, int(deficit)) if deficit is not None else None,
        url=f"https://www.opendota.com/matches/{row['match_id']}",
    )


# ---------------------------------------------------------------- explore_query

FieldType = Literal["int", "float", "bool", "text", "int[]"]


@dataclass(frozen=True)
class ExploreField:
    sql: str
    type: FieldType
    description: str


@dataclass(frozen=True)
class ExploreTable:
    from_sql: str
    fields: dict[str, ExploreField]
    description: str
    league: bool  # joins match_patch and filters by patch


_PUB_FIELDS = {
    "match_id": ExploreField("match_id", "int", "Match id (ascending with time)."),
    "start_time": ExploreField("start_time", "int", "Start time, Unix seconds."),
    "duration": ExploreField("duration", "int", "Duration in seconds."),
    "duration_min": ExploreField("(duration / 60)", "int", "Duration in whole minutes."),
    "game_mode": ExploreField("game_mode", "int", "OpenDota game mode id (22 All Pick)."),
    "lobby_type": ExploreField("lobby_type", "int", "Lobby type (7 ranked)."),
    "avg_rank_tier": ExploreField("avg_rank_tier", "float", "Average medal tier, 10..75."),
    "rank_bracket": ExploreField(
        "floor(avg_rank_tier / 10)::int", "int", "Medal from avg tier: 1 Herald .. 7 Divine."
    ),
    "num_rank_tier": ExploreField("num_rank_tier", "int", "Players with a known medal."),
    "cluster": ExploreField("cluster", "int", "Server cluster id."),
    "radiant_win": ExploreField("radiant_win", "bool", "Radiant won."),
    "radiant_team": ExploreField("radiant_team", "int[]", "Radiant hero ids."),
    "dire_team": ExploreField("dire_team", "int[]", "Dire hero ids."),
}

_MATCH_FIELDS = {
    "match_id": ExploreField("m.match_id", "int", "Match id."),
    "start_time": ExploreField("m.start_time", "int", "Start time, Unix seconds."),
    "duration": ExploreField("m.duration", "int", "Duration in seconds."),
    "duration_min": ExploreField("(m.duration / 60)", "int", "Duration in whole minutes."),
    "radiant_win": ExploreField("m.radiant_win", "bool", "Radiant won."),
    "leagueid": ExploreField("m.leagueid", "int", "League id."),
    "game_mode": ExploreField("m.game_mode", "int", "OpenDota game mode id."),
    "radiant_score": ExploreField("m.radiant_score", "int", "Radiant kills."),
    "dire_score": ExploreField("m.dire_score", "int", "Dire kills."),
    "first_blood_time": ExploreField("m.first_blood_time", "int", "First blood, seconds."),
    "radiant_team_id": ExploreField("m.radiant_team_id", "int", "Radiant team id."),
    "dire_team_id": ExploreField("m.dire_team_id", "int", "Dire team id."),
    "radiant_team_name": ExploreField("m.radiant_team_name", "text", "Radiant team name."),
    "dire_team_name": ExploreField("m.dire_team_name", "text", "Dire team name."),
    "series_type": ExploreField("m.series_type", "int", "0 Bo1, 1 Bo3, 2 Bo5."),
    "patch": ExploreField("mp.patch", "text", 'Major patch, e.g. "7.41".'),
}

_PLAYER_FIELDS = {
    **{
        k: v
        for k, v in _MATCH_FIELDS.items()
        if k in ("match_id", "start_time", "duration", "duration_min", "leagueid", "patch")
    },
    "match_id": ExploreField("pm.match_id", "int", "Match id."),
    "hero_id": ExploreField("pm.hero_id", "int", "Hero id."),
    "player_slot": ExploreField("pm.player_slot", "int", "0-4 Radiant, 128-132 Dire."),
    "is_radiant": ExploreField("(pm.player_slot < 128)", "bool", "Player is on Radiant."),
    "win": ExploreField(
        "((pm.player_slot < 128) = m.radiant_win)", "bool", "The player's team won."
    ),
    **{
        name: ExploreField(
            expr,
            "float" if name in ("stuns", "teamfight_participation", "duration_min") else "int",
            meaning,
        )
        for name, (expr, meaning) in PLAYER_METRICS.items()
        if name != "duration_min"
    },
    "lane": ExploreField("pm.lane", "int", "Lane the player started in."),
    "is_roaming": ExploreField("pm.is_roaming", "bool", "Roamed in the laning phase."),
    "party_size": ExploreField("pm.party_size", "int", "Party size."),
    "item_neutral": ExploreField("pm.item_neutral", "int", "Neutral item id."),
}

EXPLORE_TABLES: dict[str, ExploreTable] = {
    "public_matches": ExploreTable(
        "public_matches",
        _PUB_FIELDS,
        "Sampled public matches of the last year: heroes, rank, duration, winner. No per-player "
        "stats.",
        league=False,
    ),
    "matches": ExploreTable(
        "matches m JOIN match_patch mp USING (match_id)",
        _MATCH_FIELDS,
        "Parsed league matches (pro and amateur leagues), one row per match.",
        league=True,
    ),
    "player_matches": ExploreTable(
        "player_matches pm JOIN matches m USING (match_id) JOIN match_patch mp USING (match_id)",
        _PLAYER_FIELDS,
        "Parsed league matches, one row per player.",
        league=True,
    ),
}

FilterOp = Literal[
    "=", "!=", "<", "<=", ">", ">=", "in", "not_in", "between", "contains", "is_null", "not_null"
]
AggFunc = Literal["count", "count_distinct", "avg", "sum", "min", "max", "median", "stddev"]
EXPLORE_MAX_LIMIT = 500
EXPLORE_DEFAULT_LIMIT = 100
EXPLORE_MAX_IN = 50
PUB_EXPLORE_DEFAULT_WINDOW = 100_000
PUB_EXPLORE_MAX_WINDOW = 2_000_000
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class ExploreFilter(BaseModel):
    field: str = Field(description="A field of the chosen table (see the table's field list).")
    op: FilterOp = Field(
        description=(
            "Comparison. in/not_in take a list (at most 50), between takes [low, high], "
            "contains (hero arrays only) takes a list of hero ids that must all be present, "
            "is_null/not_null take no value."
        )
    )
    value: int | float | bool | str | list[int | float | str] | None = None


class ExploreAggregate(BaseModel):
    func: AggFunc = Field(description="Aggregate function.")
    field: str | None = Field(
        default=None, description="Field to aggregate; omit for count (rows)."
    )
    alias: str | None = Field(
        default=None, description="Output column name (lowercase letters, digits, _)."
    )


class ExploreSpec(BaseModel):
    table: Literal["public_matches", "matches", "player_matches"]
    select: list[str] = Field(default_factory=list)
    filters: list[ExploreFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregates: list[ExploreAggregate] = Field(default_factory=list)
    order_by: str | None = None
    descending: bool = True
    limit: int = EXPLORE_DEFAULT_LIMIT
    patch: str | None = None
    recent_match_window: int = PUB_EXPLORE_DEFAULT_WINDOW


class ExploreQueryError(ValueError):
    """The structured query is invalid; the message says what to change."""


def _field(table: ExploreTable, name: str, table_name: str) -> ExploreField:
    f = table.fields.get(name)
    if f is None:
        raise ExploreQueryError(
            f"{table_name} has no field {name!r}; fields: {', '.join(table.fields)}."
        )
    return f


def _literal(f: ExploreField, value: Any) -> str:
    if f.type == "int":
        return sql_int(value)
    if f.type == "float":
        return sql_float(value)
    if f.type == "bool":
        return sql_bool(value)
    if f.type == "text":
        return sql_text(value)
    raise UnsafeValueError("hero arrays only support contains")


def _predicate(f: ExploreFilter, field: ExploreField) -> str:
    op, value = f.op, f.value
    if op in ("is_null", "not_null"):
        return f"{field.sql} IS {'NOT ' if op == 'not_null' else ''}NULL"
    if field.type == "int[]":
        if op != "contains":
            raise ExploreQueryError(f"{f.field} is a hero array; use op 'contains'.")
        ids = value if isinstance(value, list) else [value]
        if not ids or len(ids) > 5:
            raise ExploreQueryError("contains takes 1-5 hero ids.")
        return f"{field.sql} @> {sql_int_array(ids)}"
    if op == "contains":
        raise ExploreQueryError("contains only applies to radiant_team / dire_team.")
    if op in ("in", "not_in"):
        if not isinstance(value, list) or not 1 <= len(value) <= EXPLORE_MAX_IN:
            raise ExploreQueryError(f"{op} takes a list of 1-{EXPLORE_MAX_IN} values.")
        items = ", ".join(_literal(field, v) for v in value)
        return f"{field.sql} {'NOT IN' if op == 'not_in' else 'IN'} ({items})"
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ExploreQueryError("between takes [low, high].")
        return f"{field.sql} BETWEEN {_literal(field, value[0])} AND {_literal(field, value[1])}"
    if isinstance(value, list) or value is None:
        raise ExploreQueryError(f"{op} takes a single value.")
    if field.type == "bool" and op not in ("=", "!="):
        raise ExploreQueryError(f"{f.field} is true/false; use = or !=.")
    sql_op = "<>" if op == "!=" else op
    return f"{field.sql} {sql_op} {_literal(field, value)}"


_AGG_SQL = {
    "count": "count({})",
    "count_distinct": "count(DISTINCT {})",
    "avg": "avg({})::float8",
    "sum": "sum({})",
    "min": "min({})",
    "max": "max({})",
    "median": "percentile_cont(0.5) WITHIN GROUP (ORDER BY {})",
    "stddev": "stddev_samp({})::float8",
}
_NUMERIC_AGGS = {"avg", "sum", "median", "stddev"}


@dataclass(frozen=True)
class BuiltQuery:
    sql: str
    columns: list[str]
    aggregated: bool
    applied: list[str]  # restrictions the builder added on its own


def build_explore_sql(spec: ExploreSpec) -> BuiltQuery:
    """Assemble SQL from a validated ``spec``. Raises ExploreQueryError / UnsafeValueError."""
    table = EXPLORE_TABLES[spec.table]
    if not 1 <= spec.limit <= EXPLORE_MAX_LIMIT:
        raise ExploreQueryError(f"limit must be 1-{EXPLORE_MAX_LIMIT}.")
    aggregated = bool(spec.aggregates or spec.group_by)
    columns: list[str] = []
    select_sql: list[str] = []
    applied: list[str] = []

    for name in spec.group_by:
        f = _field(table, name, spec.table)
        if f.type == "int[]":
            raise ExploreQueryError(f"Cannot group by the hero array {name}.")
    if aggregated:
        extra = [s for s in spec.select if s not in spec.group_by]
        if extra:
            raise ExploreQueryError(
                f"With aggregates, select only group_by fields; move {', '.join(extra)} into "
                "group_by or an aggregate."
            )
        for name in spec.group_by:
            select_sql.append(f"{table.fields[name].sql} AS {name}")
            columns.append(name)
        select_sql.append("count(*) AS n")
        columns.append("n")
        for agg in spec.aggregates:
            alias = agg.alias or (f"{agg.func}_{agg.field}" if agg.field else agg.func)
            if not _ALIAS_RE.fullmatch(alias) or alias in columns:
                raise ExploreQueryError(f"Alias {alias!r} is invalid or repeated.")
            if agg.field is None:
                if agg.func != "count":
                    raise ExploreQueryError(f"{agg.func} needs a field.")
                expr = "count(*)"
            else:
                f = _field(table, agg.field, spec.table)
                if f.type == "int[]" or (agg.func in _NUMERIC_AGGS and f.type in ("text",)):
                    raise ExploreQueryError(f"{agg.func} does not apply to {agg.field}.")
                inner = f"({f.sql})::int" if f.type == "bool" else f.sql
                if f.type == "bool" and agg.func not in _NUMERIC_AGGS | {"count"}:
                    raise ExploreQueryError(f"{agg.func} does not apply to {agg.field}.")
                expr = _AGG_SQL[agg.func].format(inner)
            select_sql.append(f"{expr} AS {alias}")
            columns.append(alias)
    else:
        names = spec.select or ["match_id"]
        for name in names:
            f = _field(table, name, spec.table)
            select_sql.append(f"{f.sql} AS {name}")
            columns.append(name)

    where: list[str] = []
    for flt in spec.filters:
        where.append(_predicate(flt, _field(table, flt.field, spec.table)))
    if table.league:
        if spec.patch is None:
            raise ExploreQueryError("League tables need a patch.")
        where.insert(0, f"mp.patch = {sql_text(spec.patch)}")
    else:
        has_id_bound = any(
            f.field == "match_id" and f.op in (">", ">=", "between", "=", "in")
            for f in spec.filters
        )
        if not has_id_bound:
            window = spec.recent_match_window
            if not 1 <= window <= PUB_EXPLORE_MAX_WINDOW:
                raise ExploreQueryError(f"recent_match_window must be 1-{PUB_EXPLORE_MAX_WINDOW}.")
            where.insert(
                0, f"match_id > (SELECT max(match_id) FROM public_matches) - {sql_int(window)}"
            )
            applied.append(f"Only the newest {window:,} match ids of public_matches are scanned.")

    sql = f"SELECT {', '.join(select_sql)} FROM {table.from_sql}"
    if where:
        sql += f" WHERE {' AND '.join(where)}"
    if aggregated and spec.group_by:
        sql += " GROUP BY " + ", ".join(table.fields[n].sql for n in spec.group_by)
    if spec.order_by is not None:
        if spec.order_by not in columns:
            raise ExploreQueryError(
                f"order_by must be one of the output columns: {', '.join(columns)}."
            )
        sql += f" ORDER BY {spec.order_by} {'DESC' if spec.descending else 'ASC'}"
        sql += " NULLS LAST" if spec.descending else ""
    elif not aggregated:
        order = "pm.match_id" if spec.table == "player_matches" else table.fields["match_id"].sql
        sql += f" ORDER BY {order} DESC"
    sql += f" LIMIT {sql_int(spec.limit)}"
    return BuiltQuery(sql=sql, columns=columns, aggregated=aggregated, applied=applied)

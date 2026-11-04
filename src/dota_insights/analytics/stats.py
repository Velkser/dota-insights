"""Statistical helpers for win/pick rates, matchups and meta-trend scans.

Standard library only. Every function documents the formula it implements so results
can be checked by hand.
"""

import math
import random
from collections.abc import Callable, Sequence
from statistics import NormalDist, median
from typing import NamedTuple


class Interval(NamedTuple):
    """A closed confidence interval ``[low, high]``."""

    low: float
    high: float


class ZTestResult(NamedTuple):
    """Result of a two-proportion z-test."""

    z: float
    p_value: float


class BHResult(NamedTuple):
    """Benjamini-Hochberg output, both lists in the order of the input p-values."""

    adjusted: list[float]
    rejected: list[bool]


def _z_critical(confidence: float) -> float:
    """Two-sided standard normal critical value ``z = Φ⁻¹(1 - (1 - confidence) / 2)``."""
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    return NormalDist().inv_cdf(1 - (1 - confidence) / 2)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF ``Φ(x) = (1 + erf(x / √2)) / 2``."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _check_counts(successes: int, n: int) -> None:
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    if not 0 <= successes <= n:
        raise ValueError(f"successes must be in [0, n], got {successes} of {n}")


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> Interval:
    """Wilson score interval for a binomial proportion (win rate, pick rate).

    With ``p̂ = successes / n`` and ``z`` the two-sided critical value for ``confidence``::

        centre = (p̂ + z² / 2n) / (1 + z² / n)
        margin = z · √(p̂(1 - p̂) / n + z² / 4n²) / (1 + z² / n)
        interval = [centre - margin, centre + margin]

    Unlike the normal (Wald) interval it stays inside ``[0, 1]`` and behaves well for
    small ``n`` and rates near 0 or 1. With ``n = 0`` there is no information, so the
    interval is ``[0, 1]``.
    """
    _check_counts(successes, n)
    z = _z_critical(confidence)
    if n == 0:
        return Interval(0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return Interval(max(0.0, centre - margin), min(1.0, centre + margin))


def bootstrap_ci(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = median,
    confidence: float = 0.95,
    resamples: int = 2000,
    seed: int | None = None,
) -> Interval:
    """Percentile bootstrap confidence interval for ``stat`` of ``values``.

    Draws ``resamples`` samples of size ``len(values)`` with replacement, computes
    ``stat`` on each, sorts the results ``θ*₍₁₎ ≤ … ≤ θ*₍B₎`` and returns the empirical
    ``α/2`` and ``1 - α/2`` quantiles (``α = 1 - confidence``), using linear
    interpolation between order statistics at position ``q · (B - 1)``.

    The same ``seed`` always gives the same interval; ``seed=None`` is non-deterministic.
    """
    if not values:
        raise ValueError("values must not be empty")
    if resamples < 1:
        raise ValueError(f"resamples must be positive, got {resamples}")
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    rng = random.Random(seed)
    data = list(values)
    k = len(data)
    estimates = sorted(stat(rng.choices(data, k=k)) for _ in range(resamples))
    alpha = 1 - confidence
    return Interval(_quantile(estimates, alpha / 2), _quantile(estimates, 1 - alpha / 2))


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linearly interpolated quantile of an already sorted sequence."""
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def smoothed_rate(successes: int, n: int, prior_rate: float, prior_strength: float) -> float:
    """Posterior-mean rate under a Beta prior (empirical-Bayes shrinkage).

    The prior is ``Beta(α, β)`` with ``α = prior_rate · prior_strength`` and
    ``β = (1 - prior_rate) · prior_strength``, i.e. ``prior_strength`` pseudo-games at
    ``prior_rate``. The posterior mean is::

        (successes + prior_rate · prior_strength) / (n + prior_strength)

    Small samples are pulled towards ``prior_rate`` (typically the global/patch average),
    so ranking heroes by this value does not crown a 3-0 hero with a 100% win rate.
    With no games and no prior strength the result is ``prior_rate``.
    """
    _check_counts(successes, n)
    if not 0 <= prior_rate <= 1:
        raise ValueError(f"prior_rate must be in [0, 1], got {prior_rate}")
    if prior_strength < 0:
        raise ValueError(f"prior_strength must be non-negative, got {prior_strength}")
    total = n + prior_strength
    if total == 0:
        return prior_rate
    return (successes + prior_rate * prior_strength) / total


def two_proportion_ztest(s1: int, n1: int, s2: int, n2: int) -> ZTestResult:
    """Pooled two-proportion z-test for ``H₀: p₁ = p₂`` (two-sided).

    With ``p̂ᵢ = sᵢ / nᵢ`` and pooled ``p̂ = (s₁ + s₂) / (n₁ + n₂)``::

        z = (p̂₁ - p̂₂) / √(p̂(1 - p̂)(1/n₁ + 1/n₂))
        p = 2 · (1 - Φ(|z|)),  Φ(x) = (1 + erf(x / √2)) / 2

    ``z > 0`` means the first proportion is higher. If the pooled rate is 0 or 1 the two
    samples are identical, so ``z = 0`` and ``p = 1``.
    """
    _check_counts(s1, n1)
    _check_counts(s2, n2)
    if n1 == 0 or n2 == 0:
        raise ValueError("both samples must be non-empty")
    pooled = (s1 + s2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return ZTestResult(0.0, 1.0)
    z = (s1 / n1 - s2 / n2) / se
    p_value = 2 * (1 - _normal_cdf(abs(z)))
    return ZTestResult(z, min(1.0, p_value))


def benjamini_hochberg(p_values: Sequence[float], alpha: float = 0.05) -> BHResult:
    """Benjamini-Hochberg false-discovery-rate correction.

    For ``m`` p-values sorted ascending ``p₍₁₎ ≤ … ≤ p₍ₘ₎`` the adjusted values are::

        q₍ᵢ₎ = min(1, min_{k ≥ i} p₍ₖ₎ · m / k)

    and hypothesis ``i`` is rejected when ``qᵢ ≤ alpha``. This controls the expected
    share of false discoveries at ``alpha`` when scanning many heroes at once. Results
    are returned in the original input order.
    """
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    for p in p_values:
        if not 0 <= p <= 1:
            raise ValueError(f"p-values must be in [0, 1], got {p}")
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m / rank)
        adjusted[i] = running
    return BHResult(adjusted, [q <= alpha for q in adjusted])


def advantage(pair_winrate: float, base_winrate_a: float, base_winrate_b: float) -> float:
    """Matchup advantage of hero A against hero B, in win-rate points.

    ``pair_winrate`` is A's win rate in games where A faced B; ``base_winrate_a`` and
    ``base_winrate_b`` are each hero's overall win rate. The win rate A would be expected
    to have against B if the matchup itself did not matter is the log5 estimate::

        expected = a(1 - b) / (a(1 - b) + b(1 - a))

    and the advantage is ``pair_winrate - expected``. Positive means A beats B more often
    than the two heroes' general strength predicts; two 50% heroes give ``expected = 0.5``.
    """
    for name, value in (
        ("pair_winrate", pair_winrate),
        ("base_winrate_a", base_winrate_a),
        ("base_winrate_b", base_winrate_b),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be in [0, 1], got {value}")
    a, b = base_winrate_a, base_winrate_b
    denom = a * (1 - b) + b * (1 - a)
    if denom == 0:
        raise ValueError("expected win rate is undefined when both base rates are 0 or both 1")
    return pair_winrate - a * (1 - b) / denom


def is_sufficient(n: int, min_sample: int) -> bool:
    """Whether ``n`` games meet the minimum sample size: ``n ≥ min_sample``."""
    return n >= min_sample

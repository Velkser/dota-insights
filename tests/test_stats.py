from statistics import mean

import pytest

from dota_insights.analytics.stats import (
    advantage,
    benjamini_hochberg,
    bootstrap_ci,
    is_sufficient,
    smoothed_rate,
    two_proportion_ztest,
    wilson_interval,
)

# --- wilson_interval -------------------------------------------------------------------


def test_wilson_half():
    low, high = wilson_interval(50, 100)
    assert low == pytest.approx(0.4038, abs=1e-4)
    assert high == pytest.approx(0.5962, abs=1e-4)


def test_wilson_zero_successes():
    # Reference: 0/10 at 95% -> [0, 0.2775]
    low, high = wilson_interval(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.2775, abs=1e-4)


def test_wilson_all_successes():
    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(0.7225, abs=1e-4)
    assert high == 1.0


def test_wilson_tiny_n():
    # Reference: 1/1 at 95% -> [0.2065, 1]
    low, high = wilson_interval(1, 1)
    assert low == pytest.approx(0.2065, abs=1e-4)
    assert high == 1.0


def test_wilson_n_zero():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_higher_confidence_is_wider():
    narrow = wilson_interval(30, 60, confidence=0.90)
    wide = wilson_interval(30, 60, confidence=0.99)
    assert wide.low < narrow.low and wide.high > narrow.high


@pytest.mark.parametrize("args", [(-1, 10), (11, 10), (0, -1)])
def test_wilson_rejects_bad_counts(args):
    with pytest.raises(ValueError):
        wilson_interval(*args)


def test_wilson_rejects_bad_confidence():
    with pytest.raises(ValueError):
        wilson_interval(5, 10, confidence=1.0)


# --- bootstrap_ci ----------------------------------------------------------------------


def test_bootstrap_seeded_is_reproducible():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert bootstrap_ci(values, seed=42) == bootstrap_ci(values, seed=42)


def test_bootstrap_contains_median_and_stays_in_range():
    values = list(range(1, 101))
    low, high = bootstrap_ci(values, seed=1)
    assert 1 <= low <= 50.5 <= high <= 100


def test_bootstrap_constant_values():
    assert bootstrap_ci([7.0] * 20, seed=0) == (7.0, 7.0)


def test_bootstrap_single_value():
    assert bootstrap_ci([3.0], seed=0) == (3.0, 3.0)


def test_bootstrap_custom_stat():
    values = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
    low, high = bootstrap_ci(values, stat=mean, resamples=500, seed=7)
    assert 10.0 <= low < 15.0 < high <= 20.0


def test_bootstrap_rejects_empty():
    with pytest.raises(ValueError):
        bootstrap_ci([])


# --- smoothed_rate ---------------------------------------------------------------------


def test_smoothed_rate_formula():
    # (3 + 0.5 * 20) / (3 + 20) = 13 / 23
    assert smoothed_rate(3, 3, prior_rate=0.5, prior_strength=20) == pytest.approx(13 / 23)


def test_smoothed_rate_ranks_large_sample_above_tiny_perfect_record():
    tiny = smoothed_rate(3, 3, prior_rate=0.5, prior_strength=50)
    large = smoothed_rate(600, 1000, prior_rate=0.5, prior_strength=50)
    assert large > tiny


def test_smoothed_rate_edge_cases():
    assert smoothed_rate(0, 0, prior_rate=0.48, prior_strength=10) == pytest.approx(0.48)
    assert smoothed_rate(0, 0, prior_rate=0.48, prior_strength=0) == pytest.approx(0.48)
    assert smoothed_rate(7, 10, prior_rate=0.5, prior_strength=0) == pytest.approx(0.7)


def test_smoothed_rate_rejects_bad_prior():
    with pytest.raises(ValueError):
        smoothed_rate(1, 2, prior_rate=1.5, prior_strength=10)
    with pytest.raises(ValueError):
        smoothed_rate(1, 2, prior_rate=0.5, prior_strength=-1)


# --- two_proportion_ztest --------------------------------------------------------------


def test_ztest_reference():
    # 60/100 vs 50/100: pooled 0.55, se = sqrt(0.55*0.45*0.02); matches R
    # prop.test(c(60, 50), c(100, 100), correct=FALSE): X^2 = 2.0202, p = 0.1552
    z, p = two_proportion_ztest(60, 100, 50, 100)
    assert z == pytest.approx(1.4213, abs=1e-4)
    assert z * z == pytest.approx(2.0202, abs=1e-4)
    assert p == pytest.approx(0.1552, abs=1e-4)


def test_ztest_sign_and_symmetry():
    z1, p1 = two_proportion_ztest(40, 100, 55, 100)
    z2, p2 = two_proportion_ztest(55, 100, 40, 100)
    assert z1 < 0 < z2
    assert z1 == pytest.approx(-z2)
    assert p1 == pytest.approx(p2)


def test_ztest_equal_rates():
    assert two_proportion_ztest(30, 60, 50, 100) == pytest.approx((0.0, 1.0))


def test_ztest_degenerate_pooled_rate():
    assert two_proportion_ztest(0, 10, 0, 20) == (0.0, 1.0)
    assert two_proportion_ztest(10, 10, 20, 20) == (0.0, 1.0)


def test_ztest_rejects_empty_sample():
    with pytest.raises(ValueError):
        two_proportion_ztest(0, 0, 5, 10)


# --- benjamini_hochberg ----------------------------------------------------------------


def test_bh_hand_worked():
    # m = 4, sorted: 0.005, 0.01, 0.03, 0.04
    # raw p*m/rank: 0.02, 0.02, 0.04, 0.04 -> already monotone
    adjusted, rejected = benjamini_hochberg([0.01, 0.04, 0.03, 0.005], alpha=0.03)
    assert adjusted == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert rejected == [True, False, False, True]


def test_bh_enforces_monotonicity():
    # raw: 0.04*2/1 = 0.08, 0.041*2/2 = 0.041 -> step-up min gives 0.041 for both
    adjusted, rejected = benjamini_hochberg([0.04, 0.041], alpha=0.05)
    assert adjusted == pytest.approx([0.041, 0.041])
    assert rejected == [True, True]


def test_bh_caps_at_one():
    adjusted, rejected = benjamini_hochberg([0.9, 0.95, 1.0])
    assert all(q <= 1.0 for q in adjusted)
    assert rejected == [False, False, False]


def test_bh_empty():
    assert benjamini_hochberg([]) == ([], [])


def test_bh_rejects_invalid_p():
    with pytest.raises(ValueError):
        benjamini_hochberg([0.1, 1.2])


# --- advantage -------------------------------------------------------------------------


def test_advantage_even_heroes():
    assert advantage(0.55, 0.5, 0.5) == pytest.approx(0.05)


def test_advantage_log5_expected():
    # a = 0.6, b = 0.4: expected = 0.36 / (0.36 + 0.16) = 0.6923...
    assert advantage(0.6923076923, 0.6, 0.4) == pytest.approx(0.0, abs=1e-9)
    assert advantage(0.65, 0.6, 0.4) == pytest.approx(0.65 - 0.36 / 0.52)


def test_advantage_rejects_undefined_expectation():
    with pytest.raises(ValueError):
        advantage(0.5, 1.0, 1.0)


# --- is_sufficient ---------------------------------------------------------------------


def test_is_sufficient():
    assert is_sufficient(100, 100)
    assert is_sufficient(101, 100)
    assert not is_sufficient(99, 100)
    assert is_sufficient(0, 0)

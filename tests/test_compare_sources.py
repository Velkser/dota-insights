"""Cross-source comparison logic (scripts/compare_sources.py, p1-07) on a recorded snapshot.

``fixtures/reference/source_snapshot.json`` is the per-hero counts the live run of 2026-11-04
saved (OpenDota ``/heroStats`` and STRATZ ``winDay``, no database). No API is called here.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_sources.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("compare_sources", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


cs = _load_script()
SNAPSHOT = json.loads(cs.SNAPSHOT.read_text(encoding="utf-8"))


def _counts(rates: dict[int, tuple[int, int]], matches: int = 10_000):
    return cs.SourceCounts(matches=matches, heroes=rates)


def test_spearman():
    assert cs.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert cs.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Ties take their average rank: ranks (1.5, 1.5, 3) vs (1, 2, 3) -> r = 0.866.
    assert cs.spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.8660254)


def test_compare_pair_by_hand():
    # Ten heroes, 1,000 games each. Source A: hero h wins 500 + h; B: 500 - h.
    a = _counts({h: (1000, 500 + h) for h in range(1, 11)})
    b = _counts({h: (1000, 500 - h) for h in range(1, 11)}, matches=20_000)
    pair = cs.compare_pair("opendota", a, "stratz", b)
    assert pair.n == 10
    # Differences 2h/1000 for h = 1..10: mean 11/1000, all positive, so bias = MAE.
    assert pair.win_mae == pytest.approx(0.011)
    assert pair.win_bias == pytest.approx(0.011)
    assert pair.win_spearman == pytest.approx(-1.0)  # A rises with h, B falls
    # Pick rates 1000/10000 = 0.1 vs 1000/20000 = 0.05 for every hero.
    assert pair.pick_mae == pytest.approx(0.05)
    assert pair.top(1)[0].hero == 10
    # The biggest gap, 52.0% vs 49.0% at n = 1000 each: z = 1.342, p = 0.18; nothing survives BH.
    assert pair.significant == []


def test_compare_pair_skips_small_heroes_and_small_overlaps():
    a = _counts({h: (1000, 500) for h in range(1, 12)} | {99: (50, 40)})
    b = _counts({h: (1000, 500) for h in range(1, 12)} | {99: (5000, 2500)})
    pair = cs.compare_pair("opendota", a, "stratz", b)
    assert 99 not in {h.hero for h in pair.heroes}  # 50 games < MIN_GAMES in A
    assert pair.win_mae == 0.0
    few = _counts({h: (1000, 500) for h in range(1, 6)})
    assert cs.compare_pair("opendota", few, "stratz", few) is None


def test_recorded_snapshot_agreement():
    parsed = cs.parse_snapshot(SNAPSHOT)
    assert set(parsed) == set(cs.BRACKETS.values())
    every = parsed["all"]
    assert isinstance(every["db"], str)  # no database in the recorded run
    (pair,) = cs.compare_bracket({k: v for k, v in every.items() if not isinstance(v, str)})
    assert (pair.a, pair.b, pair.n) == ("opendota", "stratz", 127)
    assert pair.win_mae < 0.005 and pair.win_spearman > 0.95
    assert every["opendota"].pool_win_rate == pytest.approx(0.5, abs=1e-3)
    # OpenDota has no Immortal bucket (8_pick is 0), so Immortal has one source only.
    immortal = parsed["Immortal"]
    assert isinstance(immortal["opendota"], str)
    assert cs.compare_bracket({"stratz": immortal["stratz"]}) == []


def test_report_matches_snapshot():
    current = cs.REPORT.read_text(encoding="utf-8")
    assert cs.with_findings(cs.render(SNAPSHOT), current) == current, (
        "docs/research/source-comparison.md does not match the snapshot; rerun "
        "scripts/compare_sources.py --snapshot tests/fixtures/reference/source_snapshot.json"
    )


def test_findings_survive_regeneration():
    current = f"old table\n\n{cs.FINDINGS}\nhand-written\n"
    assert cs.with_findings("new table\n", current) == f"new table\n\n{cs.FINDINGS}\nhand-written\n"
    assert cs.with_findings("new table\n", None) == f"new table\n\n{cs.FINDINGS}\n"

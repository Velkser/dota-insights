"""Fit the live win-probability model shipped in ``analytics/data/live_win_model.json``.

Downloads parsed league matches (per-minute Radiant net-worth advantage, winner and heroes)
from the OpenDota explorer, fits a time-varying logistic regression and writes the
coefficients, hero ratings and a held-out calibration report. Run with::

    uv run python scripts/train_live_win_model.py --cache-dir /tmp/live-win-cache

Model (see ``dota_insights.analytics.live``): logit P(Radiant wins) =
a(t) + b(t) * lead / 1000 + c(t) * draft, with a, b, c piecewise linear in the minute t.
``draft`` sums hero ratings (logit of each hero's smoothed win rate) Radiant minus Dire.

Split: the newest ``--test-share`` of matches (by match id, so by time) is held out for
calibration; the coefficients are fitted on the older ones. Training rows use out-of-fold hero
ratings so the draft coefficient is not fitted on ratings that saw the same match. The shipped
hero ratings come from all training matches.

Uses only the standard library, plus the package for the prediction code itself, so the
numbers it reports are computed with exactly the code that serves them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dota_insights.analytics.live import (  # noqa: E402
    LEAD_SCALE,
    WinModel,
    hat_weights,
    logit,
    sigmoid,
)

EXPLORER = "https://api.opendota.com/api/explorer"
OUT = Path(__file__).resolve().parents[1] / "src/dota_insights/analytics/data/live_win_model.json"
KNOTS = (0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60)
MAX_MINUTE = 70
PRIOR_GAMES = 50  # hero win rates are shrunk toward 50% with this many pseudo-games
FOLDS = 5
RIDGE = 1.0
# The draft signal is weak and late knots see few matches; shrink those terms harder.
RIDGE_DRAFT = 2000.0
RELIABILITY_BINS = 10
TIME_BUCKETS = ((0, 10), (10, 20), (20, 30), (30, 40), (40, None))
PAGE_SQL = (
    "SELECT m.match_id, m.start_time, m.radiant_win, m.radiant_gold_adv, "
    "array(SELECT pm.hero_id FROM player_matches pm WHERE pm.match_id = m.match_id "
    "AND pm.player_slot < 128) AS radiant_heroes, "
    "array(SELECT pm.hero_id FROM player_matches pm WHERE pm.match_id = m.match_id "
    "AND pm.player_slot >= 128) AS dire_heroes "
    "FROM matches m WHERE m.match_id > {after} AND m.match_id <= {until} "
    "AND m.radiant_gold_adv IS NOT NULL AND m.radiant_win IS NOT NULL "
    "ORDER BY m.match_id LIMIT {limit}"
)


# Data


def fetch_page(after: int, until: int, limit: int, cache_dir: Path | None) -> list[dict]:
    cached = cache_dir / f"page_{after}_{until}_{limit}.json" if cache_dir else None
    if cached and cached.exists():
        return json.loads(cached.read_text())
    sql = PAGE_SQL.format(after=after, until=until, limit=limit)
    url = EXPLORER + "?" + urllib.parse.urlencode({"sql": sql})
    request = urllib.request.Request(url, headers={"User-Agent": "dota-insights-model-trainer"})
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)
    if body.get("err"):
        raise RuntimeError(f"explorer failed: {body['err']}")
    rows = body["rows"]
    if cached:
        cached.write_text(json.dumps(rows))
    time.sleep(1.5)  # stay well under the free tier's 60 calls/min
    return rows


def fetch_matches(first: int, last: int, page: int, cache_dir: Path | None) -> list[dict]:
    matches, after = [], first
    while True:
        rows = fetch_page(after, last, page, cache_dir)
        matches += rows
        print(f"  fetched {len(matches)} matches (after {after})", file=sys.stderr)
        if len(rows) < page:
            return matches
        after = rows[-1]["match_id"]


def usable(m: dict) -> bool:
    adv = m.get("radiant_gold_adv") or []
    return (
        len(adv) >= 10
        and len(m.get("radiant_heroes") or []) == 5
        and len(m.get("dire_heroes") or []) == 5
    )


# Hero ratings


def hero_ratings(matches: list[dict]) -> dict[int, float]:
    games: dict[int, int] = defaultdict(int)
    wins: dict[int, int] = defaultdict(int)
    for m in matches:
        for side, won in (("radiant_heroes", True), ("dire_heroes", False)):
            for h in m[side]:
                games[h] += 1
                wins[h] += int(won == m["radiant_win"])
    return {
        h: logit((wins[h] + PRIOR_GAMES * 0.5) / (games[h] + PRIOR_GAMES)) for h in sorted(games)
    }


def draft_of(m: dict, ratings: dict[int, float]) -> float:
    return sum(ratings.get(h, 0.0) for h in m["radiant_heroes"]) - sum(
        ratings.get(h, 0.0) for h in m["dire_heroes"]
    )


# Samples: one per match and minute


def samples(matches: list[dict], drafts: dict[int, float]) -> list[tuple[int, float, float, int]]:
    out = []
    for m in matches:
        adv = m["radiant_gold_adv"]
        y = int(m["radiant_win"])
        d = drafts[m["match_id"]]
        for minute in range(1, min(len(adv), MAX_MINUTE + 1)):
            out.append((minute, adv[minute] / LEAD_SCALE, d, y))
    return out


# Fitting: Newton-Raphson with per-minute sufficient statistics


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(rows: list[tuple[int, float, float, int]], use_draft: bool) -> list[float]:
    """Coefficients laid out as [intercepts..., leads..., drafts...] per knot."""
    k = len(KNOTS)
    feats = 3 if use_draft else 2
    theta = [0.0] * (3 * k)
    weights = {minute: hat_weights(minute, KNOTS) for minute in range(MAX_MINUTE + 1)}
    for iteration in range(25):
        # Per minute: gradient (3) and Hessian (3x3) over features [1, lead, draft].
        grad_m: dict[int, list[float]] = defaultdict(lambda: [0.0] * 3)
        hess_m: dict[int, list[float]] = defaultdict(lambda: [0.0] * 9)
        coef = {}
        for minute, hw in weights.items():
            coef[minute] = [sum(w * theta[j * k + i] for i, w in hw) for j in range(3)]
        for minute, lead, draft, y in rows:
            a, b, c = coef[minute]
            p = sigmoid(a + b * lead + (c * draft if use_draft else 0.0))
            r = y - p
            s = p * (1.0 - p)
            g = grad_m[minute]
            g[0] += r
            g[1] += r * lead
            g[2] += r * draft
            h = hess_m[minute]
            h[0] += s
            h[1] += s * lead
            h[2] += s * draft
            h[4] += s * lead * lead
            h[5] += s * lead * draft
            h[8] += s * draft * draft
        n = 3 * k
        grad = [0.0] * n
        hess = [[0.0] * n for _ in range(n)]
        for minute, g in grad_m.items():
            h = hess_m[minute]
            full = [[h[0], h[1], h[2]], [h[1], h[4], h[5]], [h[2], h[5], h[8]]]
            for i, wi in weights[minute]:
                for f1 in range(feats):
                    grad[f1 * k + i] += wi * g[f1]
                    for j, wj in weights[minute]:
                        for f2 in range(feats):
                            hess[f1 * k + i][f2 * k + j] += wi * wj * full[f1][f2]
        for idx in range(n):
            if idx >= k:  # ridge on lead and draft terms, not on intercepts
                ridge = RIDGE_DRAFT if idx >= 2 * k else RIDGE
                grad[idx] -= ridge * theta[idx]
                hess[idx][idx] += ridge
            if not use_draft and idx >= 2 * k:
                hess[idx][idx] = 1.0
                grad[idx] = 0.0
            hess[idx][idx] += 1e-9
        step = solve(hess, grad)
        theta = [t + s for t, s in zip(theta, step, strict=True)]
        size = max(abs(s) for s in step)
        print(f"  iteration {iteration + 1}: max step {size:.2e}", file=sys.stderr)
        if size < 1e-6:
            break
    return theta


def to_model(theta: list[float], ratings: dict[int, float]) -> WinModel:
    k = len(KNOTS)
    return WinModel(
        knots=tuple(float(x) for x in KNOTS),
        intercept=tuple(theta[:k]),
        lead=tuple(theta[k : 2 * k]),
        draft=tuple(theta[2 * k :]),
        hero_ratings=ratings,
        meta={},
    )


# Evaluation


def predictions(model: WinModel, matches: list[dict]) -> list[tuple[int, float, int]]:
    """(minute, predicted Radiant win probability, outcome) per held-out match minute."""
    out = []
    for m in matches:
        adv = m["radiant_gold_adv"]
        for minute in range(1, min(len(adv), MAX_MINUTE + 1)):
            p = model.predict(minute, adv[minute], m["radiant_heroes"], m["dire_heroes"])
            out.append((minute, p.radiant_win_probability, int(m["radiant_win"])))
    return out


def brier(rows: list[tuple[int, float, int]]) -> float:
    return sum((p - y) ** 2 for _, p, y in rows) / len(rows)


def log_loss(rows: list[tuple[int, float, int]]) -> float:
    eps = 1e-12
    return -sum(
        y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for _, p, y in rows
    ) / len(rows)


def reliability(rows: list[tuple[int, float, int]]) -> list[dict]:
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for _, p, y in rows:
        bins[min(int(p * RELIABILITY_BINS), RELIABILITY_BINS - 1)].append((p, y))
    out = []
    for b in range(RELIABILITY_BINS):
        cell = bins.get(b, [])
        out.append(
            {
                "from": round(b / RELIABILITY_BINS, 2),
                "to": round((b + 1) / RELIABILITY_BINS, 2),
                "samples": len(cell),
                "predicted_mean": round(sum(p for p, _ in cell) / len(cell), 4) if cell else None,
                "observed_rate": round(sum(y for _, y in cell) / len(cell), 4) if cell else None,
            }
        )
    return out


def by_time(rows, lead_only_rows) -> list[dict]:
    out = []
    for lo, hi in TIME_BUCKETS:

        def inside(r, lo=lo, hi=hi):
            return r[0] >= lo and (hi is None or r[0] < hi)

        cell = [r for r in rows if inside(r)]
        base = [r for r in lead_only_rows if inside(r)]
        if not cell:
            continue
        out.append(
            {
                "from_min": lo,
                "to_min": hi,
                "samples": len(cell),
                "brier": round(brier(cell), 4),
                "brier_lead_only": round(brier(base), 4),
                "accuracy": round(sum((p >= 0.5) == bool(y) for _, p, y in cell) / len(cell), 4),
            }
        )
    return out


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--first-match-id", type=int, default=8_300_000_000)
    parser.add_argument("--last-match-id", type=int, default=10_000_000_000)
    parser.add_argument("--page-size", type=int, default=3000)
    parser.add_argument("--test-share", type=float, default=0.2)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)

    print("Downloading matches", file=sys.stderr)
    raw = fetch_matches(args.first_match_id, args.last_match_id, args.page_size, args.cache_dir)
    matches = sorted((m for m in raw if usable(m)), key=lambda m: m["match_id"])
    cut = int(len(matches) * (1 - args.test_share))
    train, test = matches[:cut], matches[cut:]
    print(f"{len(matches)} usable matches: {len(train)} train, {len(test)} test", file=sys.stderr)

    # Out-of-fold ratings for the training rows.
    oof: dict[int, float] = {}
    for fold in range(FOLDS):
        held = [m for i, m in enumerate(train) if i % FOLDS == fold]
        rest = [m for i, m in enumerate(train) if i % FOLDS != fold]
        ratings = hero_ratings(rest)
        for m in held:
            oof[m["match_id"]] = draft_of(m, ratings)
    ratings = hero_ratings(train)
    rows = samples(train, oof)
    print(f"Fitting on {len(rows)} match-minutes", file=sys.stderr)
    model = to_model(fit(rows, use_draft=True), ratings)
    print("Fitting lead-only baseline", file=sys.stderr)
    lead_only = to_model(fit(rows, use_draft=False), {})

    preds = predictions(model, test)
    base_preds = predictions(lead_only, test)
    radiant_rate = sum(m["radiant_win"] for m in train) / len(train)
    constant = [(minute, radiant_rate, y) for minute, _, y in preds]
    draft_only = []
    for m in test:
        p = model.predict(0, 0, m["radiant_heroes"], m["dire_heroes"]).radiant_win_probability
        draft_only.append((0, p, int(m["radiant_win"])))
    pre_game_constant = [(0, radiant_rate, int(m["radiant_win"])) for m in test]

    calibration = {
        "split": "chronological: the newest matches by match id are held out",
        "test_matches": len(test),
        "test_samples": len(preds),
        "brier": round(brier(preds), 4),
        "brier_lead_only": round(brier(base_preds), 4),
        "brier_constant": round(brier(constant), 4),
        "log_loss": round(log_loss(preds), 4),
        "pre_game_brier_draft": round(brier(draft_only), 4),
        "pre_game_brier_constant": round(brier(pre_game_constant), 4),
        "reliability": reliability(preds),
        "by_time": by_time(preds, base_preds),
    }
    k = len(KNOTS)
    output = {
        "version": 1,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "opendota explorer: parsed league matches (matches.radiant_gold_adv)",
        "training": {
            "matches": len(train),
            "samples": len(rows),
            "first_match_id": train[0]["match_id"],
            "last_match_id": train[-1]["match_id"],
            "from_date": day(min(m["start_time"] for m in train)),
            "to_date": day(max(m["start_time"] for m in train)),
            "test_from_date": day(min(m["start_time"] for m in test)),
            "test_to_date": day(max(m["start_time"] for m in test)),
            "radiant_win_rate": round(radiant_rate, 4),
            "hero_prior_games": PRIOR_GAMES,
            "ridge": RIDGE,
            "ridge_draft": RIDGE_DRAFT,
            "max_minute": MAX_MINUTE,
        },
        "knots_min": list(KNOTS),
        "coefficients": {
            "intercept": [round(x, 5) for x in model.intercept],
            "lead_per_1k": [round(x, 5) for x in model.lead],
            "draft": [round(x, 5) for x in model.draft],
        },
        "lead_only_coefficients": {
            "intercept": [round(x, 5) for x in lead_only.intercept],
            "lead_per_1k": [round(x, 5) for x in lead_only.lead],
        },
        "hero_ratings": {str(h): round(r, 4) for h, r in sorted(ratings.items())},
        "calibration": calibration,
    }
    assert len(output["coefficients"]["intercept"]) == k
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"training": output["training"], "calibration": calibration}, indent=1))


if __name__ == "__main__":
    main()

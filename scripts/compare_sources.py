"""Compare hero win and pick rates across OpenDota, STRATZ and our database (tracker task p1-07).

For the current patch and a few rank brackets, the script pulls per-hero games and wins from

- OpenDota ``/heroStats`` (one call; ranked pubs of the last ~7 days, medals 1-8),
- STRATZ ``heroStats.winDay`` (one call per bracket, through ``analytics.heroes.stratz_pool``,
  the same path the hero tools use), and
- the collector's aggregates in Neon (``analytics.heroes._db_pool``), when a database is set,

then compares every pair of sources hero by hero and writes ``docs/research/source-comparison.md``.
Each live run also saves a snapshot of the counts, so the report can be rebuilt offline::

    uv run python scripts/compare_sources.py                    # live: ~7 STRATZ/OpenDota calls
    uv run python scripts/compare_sources.py --snapshot FILE    # rebuild the report from a snapshot

``tests/test_compare_sources.py`` checks the comparison logic on a recorded snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from dota_insights.analytics.stats import benjamini_hochberg, two_proportion_ztest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "research" / "source-comparison.md"
SNAPSHOT = ROOT / "tests" / "fixtures" / "reference" / "source_snapshot.json"

# Filter value -> label. STRATZ takes single medals; OpenDota tiers 1-8 are summed over the range.
BRACKETS: dict[str | None, str] = {
    None: "all",
    "herald-guardian": "Herald-Guardian",
    "crusader-archon": "Crusader-Archon",
    "legend-ancient": "Legend-Ancient",
    "divine": "Divine",
    "immortal": "Immortal",
}
TIERS: dict[str | None, tuple[int, ...]] = {
    None: tuple(range(1, 9)),
    "herald-guardian": (1, 2),
    "crusader-archon": (3, 4),
    "legend-ancient": (5, 6),
    "divine": (7,),
    "immortal": (8,),
}
SOURCES = ("opendota", "stratz", "db")
# Heroes with fewer games than this in either source are left out of a pair's comparison.
MIN_GAMES = 200
TOP = 5


# --- comparison (pure, tested) ---------------------------------------------------------------


@dataclass(frozen=True)
class SourceCounts:
    matches: int
    heroes: dict[int, tuple[int, int]]  # hero -> games, wins
    period: tuple[str, str] | None = None

    def win_rate(self, hero: int) -> float:
        games, wins = self.heroes[hero]
        return wins / games

    def pick_rate(self, hero: int) -> float:
        return self.heroes[hero][0] / self.matches

    @property
    def pool_win_rate(self) -> float:
        games = sum(g for g, _ in self.heroes.values())
        return sum(w for _, w in self.heroes.values()) / games if games else 0.0


@dataclass(frozen=True)
class HeroDiff:
    hero: int
    games_a: int
    games_b: int
    win_rate_a: float
    win_rate_b: float
    pick_rate_a: float
    pick_rate_b: float
    q_value: float

    @property
    def win_diff(self) -> float:
        return self.win_rate_a - self.win_rate_b


@dataclass(frozen=True)
class PairComparison:
    a: str
    b: str
    heroes: list[HeroDiff]
    matches_a: int
    matches_b: int

    @property
    def n(self) -> int:
        return len(self.heroes)

    @property
    def win_mae(self) -> float:
        return sum(abs(h.win_diff) for h in self.heroes) / self.n

    @property
    def win_bias(self) -> float:
        """Mean signed difference a - b: a systematic offset rather than noise."""
        return sum(h.win_diff for h in self.heroes) / self.n

    @property
    def pick_mae(self) -> float:
        return sum(abs(h.pick_rate_a - h.pick_rate_b) for h in self.heroes) / self.n

    @property
    def win_spearman(self) -> float:
        return spearman([h.win_rate_a for h in self.heroes], [h.win_rate_b for h in self.heroes])

    @property
    def pick_spearman(self) -> float:
        return spearman([h.pick_rate_a for h in self.heroes], [h.pick_rate_b for h in self.heroes])

    @property
    def significant(self) -> list[HeroDiff]:
        return [h for h in self.heroes if h.q_value < 0.05]

    def top(self, k: int = TOP) -> list[HeroDiff]:
        return sorted(self.heroes, key=lambda h: abs(h.win_diff), reverse=True)[:k]


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation: Pearson r of the ranks (ties get their average rank)."""
    rx, ry = _ranks(x), _ranks(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (sx * sy) if sx and sy else 0.0


def compare_pair(
    a_name: str, a: SourceCounts, b_name: str, b: SourceCounts, min_games: int = MIN_GAMES
) -> PairComparison | None:
    """Hero-by-hero comparison of two sources; None when fewer than 10 heroes qualify."""
    shared = sorted(
        h
        for h in a.heroes.keys() & b.heroes.keys()
        if a.heroes[h][0] >= min_games and b.heroes[h][0] >= min_games
    )
    if len(shared) < 10:
        return None
    p_values = [
        two_proportion_ztest(a.heroes[h][1], a.heroes[h][0], b.heroes[h][1], b.heroes[h][0]).p_value
        for h in shared
    ]
    q_values = benjamini_hochberg(p_values).adjusted
    heroes = [
        HeroDiff(
            hero=h,
            games_a=a.heroes[h][0],
            games_b=b.heroes[h][0],
            win_rate_a=a.win_rate(h),
            win_rate_b=b.win_rate(h),
            pick_rate_a=a.pick_rate(h),
            pick_rate_b=b.pick_rate(h),
            q_value=q,
        )
        for h, q in zip(shared, q_values, strict=True)
    ]
    return PairComparison(a_name, b_name, heroes, a.matches, b.matches)


def compare_bracket(sources: Mapping[str, SourceCounts]) -> list[PairComparison]:
    out = []
    for a, b in combinations([s for s in SOURCES if s in sources], 2):
        found = compare_pair(a, sources[a], b, sources[b])
        if found is not None:
            out.append(found)
    return out


# --- snapshot I/O ------------------------------------------------------------------------------


def parse_snapshot(data: Mapping[str, Any]) -> dict[str, dict[str, SourceCounts | str]]:
    """bracket label -> source -> counts, or the reason the source had nothing."""
    out: dict[str, dict[str, SourceCounts | str]] = {}
    for label, sources in data["brackets"].items():
        out[label] = {}
        for name, raw in sources.items():
            if "unavailable" in raw:
                out[label][name] = raw["unavailable"]
                continue
            out[label][name] = SourceCounts(
                matches=raw["matches"],
                heroes={int(h): (g, w) for h, (g, w) in raw["heroes"].items()},
                period=tuple(raw["period"]) if raw.get("period") else None,
            )
    return out


def _dump(counts: SourceCounts | str) -> dict[str, Any]:
    if isinstance(counts, str):
        return {"unavailable": counts}
    return {
        "matches": counts.matches,
        "period": list(counts.period) if counts.period else None,
        "heroes": {str(h): list(v) for h, v in sorted(counts.heroes.items())},
    }


# --- report ------------------------------------------------------------------------------------

LABELS = {"opendota": "OpenDota", "stratz": "STRATZ", "db": "DB"}


def _hero_name(hero_id: int) -> str:
    from dota_insights.reference import hero_by_id

    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


def _pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def _pp(x: float) -> str:
    return f"{100 * x:+.1f} pp"


def render(data: Mapping[str, Any]) -> str:
    parsed = parse_snapshot(data)
    lines = [
        "# Hero win and pick rates across sources",
        "",
        f"<!-- generated by scripts/compare_sources.py on {data['fetched_at']}; "
        "rerun it rather than editing -->",
        "",
        f"Snapshot taken {data['fetched_at']} for patch {data['patch']}. Per-hero games and "
        "wins from OpenDota `/heroStats`, STRATZ `heroStats.winDay` (current patch window, "
        "ranked All Pick, the path the hero tools use) and the dota-insights collector "
        "database, compared pair by pair over heroes with at least "
        f"{MIN_GAMES} games in both sources. Win-rate differences are tested per hero with a "
        "two-proportion z-test and Benjamini-Hochberg correction across heroes (q < 0.05).",
        "",
        "## Sample sizes",
        "",
        "| Bracket | " + " | ".join(LABELS[s] for s in SOURCES) + " |",
        "| --- | " + " | ".join("---" for _ in SOURCES) + " |",
    ]
    for label, sources in parsed.items():
        cells = []
        for s in SOURCES:
            got = sources.get(s)
            if got is None:
                cells.append("—")
            elif isinstance(got, str):
                cells.append(f"n/a ({got})")
            else:
                period = f", {got.period[0]} to {got.period[1]}" if got.period else ""
                cells.append(f"{got.matches:,} matches{period}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Agreement by bracket",
        "",
        "MAE = mean absolute difference over the compared heroes; bias = mean signed "
        "difference (first source minus second); ρ = Spearman rank correlation.",
        "",
        "| Bracket | Pair | Heroes | Win-rate MAE | Bias | Win ρ | Pick-rate MAE | Pick ρ | "
        "Pool win rate | Significant |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    pairs_by_bracket: dict[str, list[PairComparison]] = {}
    for label, sources in parsed.items():
        counts = {s: c for s, c in sources.items() if isinstance(c, SourceCounts)}
        pairs = compare_bracket(counts)
        pairs_by_bracket[label] = pairs
        for p in pairs:
            pool = f"{_pct(counts[p.a].pool_win_rate)} / {_pct(counts[p.b].pool_win_rate)}"
            lines.append(
                f"| {label} | {LABELS[p.a]} vs {LABELS[p.b]} | {p.n} | {_pp(p.win_mae)[1:]} | "
                f"{_pp(p.win_bias)} | {p.win_spearman:.2f} | {_pp(p.pick_mae)[1:]} | "
                f"{p.pick_spearman:.2f} | {pool} | {len(p.significant)} of {p.n} |"
            )
        if not pairs:
            lines.append(f"| {label} | — | 0 | fewer than two sources with data | | | | | | |")

    lines += ["", "## Largest disagreements", ""]
    for label, pairs in pairs_by_bracket.items():
        for p in pairs:
            lines += [
                f"**{label}, {LABELS[p.a]} vs {LABELS[p.b]}**",
                "",
                f"| Hero | {LABELS[p.a]} win rate (games) | {LABELS[p.b]} win rate (games) | "
                "Difference | q |",
                "| --- | --- | --- | --- | --- |",
            ]
            for h in p.top():
                lines.append(
                    f"| {_hero_name(h.hero)} | {_pct(h.win_rate_a)} ({h.games_a:,}) | "
                    f"{_pct(h.win_rate_b)} ({h.games_b:,}) | {_pp(h.win_diff)} | "
                    f"{h.q_value:.2g} |"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Everything after this line in the report is hand-written analysis and survives a rerun.
FINDINGS = "<!-- findings: hand-written below, kept when the report is regenerated -->"


def with_findings(generated: str, current: str | None) -> str:
    """``generated`` followed by the hand-written part of ``current`` (or an empty one)."""
    tail = current.split(FINDINGS, 1)[1] if current and FINDINGS in current else "\n"
    return f"{generated}\n{FINDINGS}{tail}"


# --- live fetch --------------------------------------------------------------------------------


async def _fetch(min_sample: int) -> dict[str, Any]:
    from dota_insights import sources as api
    from dota_insights.analytics import heroes
    from dota_insights.db.access import read
    from dota_insights.models import Filters

    raw = await api.get_opendota().hero_stats()
    brackets: dict[str, dict[str, Any]] = {}
    patch = None
    for value, label in BRACKETS.items():
        filters = Filters(rank_bracket=value, game_mode="ranked_all_pick", min_sample=min_sample)
        entry: dict[str, Any] = {}

        tiers = TIERS[value]
        od = {r["id"]: (sum(r.get(f"{t}_pick") or 0 for t in tiers),
                        sum(r.get(f"{t}_win") or 0 for t in tiers)) for r in raw}  # fmt: skip
        matches = round(sum(g for g, _ in od.values()) / 10)
        entry["opendota"] = (
            _dump(SourceCounts(matches, od, None))
            if matches
            else {"unavailable": f"no games in /heroStats tiers {', '.join(map(str, tiers))}"}
        )

        try:
            pool = await heroes.stratz_pool(filters)
            patch = patch or pool.patch
            period = (str(pool.period.from_date), str(pool.period.to_date)) if pool.period else None
            entry["stratz"] = _dump(
                SourceCounts(
                    pool.total_matches,
                    {h: (c.matches, c.wins) for h, c in pool.counts.items()},
                    period,
                )
            )
        except Exception as exc:  # report what failed instead of aborting the whole run
            entry["stratz"] = {"unavailable": f"{type(exc).__name__}"}

        pool = await read(heroes._db_pool, filters)
        if pool is None:
            entry["db"] = {"unavailable": "no database or no fresh aggregates for these filters"}
        else:
            period = (str(pool.period.from_date), str(pool.period.to_date)) if pool.period else None
            entry["db"] = _dump(
                SourceCounts(
                    pool.total_matches,
                    {h: (c.matches, c.wins) for h, c in pool.counts.items()},
                    period,
                )
            )
        brackets[label] = entry
    await api.close_clients()
    return {
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "patch": patch or "current",
        "brackets": brackets,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, help="rebuild the report from this snapshot")
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    if args.snapshot:
        data = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        data = asyncio.run(_fetch(min_sample=1))
        SNAPSHOT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
    args.out.write_text(with_findings(render(data), current), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Hero win and pick rates over time: patch windows, day windows and the changes between them.

STRATZ serves hero win/match counts in weekly buckets (about the last 19 weeks) and monthly
buckets (back to 2022). A window is the sum of the buckets that lie entirely inside it, with
weeks preferred and months filling in the time before the first week. OpenDota's
``/heroStats`` has only a 7-day daily series, which is the fallback for day windows.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dota_insights import reference
from dota_insights.analytics.stats import (
    benjamini_hochberg,
    two_proportion_ztest,
    wilson_interval,
)
from dota_insights.cache import TTL_AGGREGATE, TTL_REFERENCE, cached
from dota_insights.models import Filters, GameMode, RankBracket
from dota_insights.sources import get_opendota, get_stratz
from dota_insights.sources.stratz import StratzError

DEFAULT_GAME_MODE = GameMode.RANKED_ALL_PICK
BH_ALPHA = 0.05

SERIES_QUERY = """
query HeroTrendSeries(
  $brackets: [RankBracket], $positions: [MatchPlayerPositionType], $modes: [GameModeEnumType]
) {
  heroStats {
    winWeek(take: 60, bracketIds: $brackets, positionIds: $positions, gameModeIds: $modes,
            groupBy: HERO_ID) { week heroId matchCount winCount }
    winMonth(take: 60, bracketIds: $brackets, positionIds: $positions, gameModeIds: $modes,
             groupBy: HERO_ID) { month heroId matchCount winCount }
  }
}
"""

GAME_VERSIONS_QUERY = "{ constants { gameVersions { id name asOfDateTime } } }"

PATCH_NOTES_QUERY = """
query PatchNotes($id: Short) {
  constants { patchNotes(languageId: ENGLISH, gameVersionId: $id) { id heroId text } }
}
"""

_STRATZ_MODE = {GameMode.RANKED_ALL_PICK: "ALL_PICK_RANKED"}
_TAG = re.compile(r"<[^>]+>")
_PATCH_NAME = r"\d+\.\d+[a-z]?"
_DAYS = re.compile(r"^(?:last\s*)?(\d+)\s*(?:d|days?)$")
_PATCH_PAIR = re.compile(rf"^({_PATCH_NAME})\s*(?:→|->|-|\.\.|\s+to\s+)\s*({_PATCH_NAME})$")


class TrendInputError(ValueError):
    """A window, patch or filter the tools cannot serve; the message says what is valid."""


# ---------------------------------------------------------------------------- filters


def stratz_variables(filters: Filters) -> dict[str, Any]:
    """STRATZ ``winWeek``/``winMonth`` filter arguments for ``filters`` (single medals)."""
    brackets = None
    if filters.rank_range:
        low, high = filters.rank_range
        brackets = [b.value.upper() for b in RankBracket if low.tier <= b.tier <= high.tier]
    mode = filters.game_mode or DEFAULT_GAME_MODE
    return {
        "brackets": brackets,
        "positions": [f"POSITION_{int(filters.position)}"] if filters.position else None,
        "modes": [_STRATZ_MODE.get(mode, mode.value.upper())],
    }


def effective_filters(filters: Filters | None) -> Filters:
    """``filters`` with the default game mode filled in, as reported back to the caller."""
    filters = filters or Filters()
    if filters.game_mode is None:
        filters = filters.model_copy(update={"game_mode": DEFAULT_GAME_MODE})
    return filters


# ---------------------------------------------------------------------------- series


@dataclass(frozen=True, slots=True)
class Bucket:
    """Win/match counts per hero over ``[start, end)``."""

    start: datetime
    end: datetime
    kind: str  # "week", "month" or "day"
    heroes: dict[int, tuple[int, int]]  # hero id -> (matches, wins)


@dataclass(frozen=True, slots=True)
class Series:
    """Non-overlapping buckets in time order, plus how many picks one match contributes."""

    buckets: tuple[Bucket, ...]
    picks_per_match: int  # 10 heroes per match, or 2 when filtered to one position
    source: str  # "stratz" or "opendota"

    @property
    def end(self) -> datetime | None:
        return self.buckets[-1].end if self.buckets else None


def _ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _next_month(start: datetime) -> datetime:
    return start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)


def _group(rows: list[dict], key: str) -> dict[int, dict[int, tuple[int, int]]]:
    grouped: dict[int, dict[int, tuple[int, int]]] = {}
    for row in rows or ():
        if not row.get("heroId"):
            continue
        grouped.setdefault(row[key], {})[row["heroId"]] = (row["matchCount"], row["winCount"])
    return grouped


def parse_stratz_series(data: dict, picks_per_match: int) -> Series:
    """Weekly buckets, then monthly buckets that end before the first week, in time order."""
    stats = data["heroStats"]
    weeks = [
        Bucket(_ts(ts), _ts(ts) + timedelta(days=7), "week", heroes)
        for ts, heroes in _group(stats.get("winWeek"), "week").items()
    ]
    weeks.sort(key=lambda b: b.start)
    first_week = weeks[0].start if weeks else None
    months = []
    for ts, heroes in _group(stats.get("winMonth"), "month").items():
        start = _ts(ts)
        end = _next_month(start)
        if first_week is None or end <= first_week:
            months.append(Bucket(start, end, "month", heroes))
    months.sort(key=lambda b: b.start)
    return Series(tuple(months + weeks), picks_per_match, "stratz")


@cached(ttl=TTL_AGGREGATE)
async def _stratz_series_raw(
    brackets: tuple[str, ...] | None, positions: tuple[str, ...] | None, modes: tuple[str, ...]
) -> dict:
    variables = {
        "brackets": list(brackets) if brackets else None,
        "positions": list(positions) if positions else None,
        "modes": list(modes),
    }
    return await get_stratz().query(SERIES_QUERY, variables)


async def fetch_series(filters: Filters) -> Series:
    """STRATZ weekly + monthly hero series for ``filters`` (one cached GraphQL call)."""
    v = stratz_variables(filters)
    data = await _stratz_series_raw(
        tuple(v["brackets"]) if v["brackets"] else None,
        tuple(v["positions"]) if v["positions"] else None,
        tuple(v["modes"]),
    )
    return parse_stratz_series(data, 2 if filters.position else 10)


@cached(ttl=TTL_AGGREGATE)
async def _opendota_hero_stats() -> list[dict[str, Any]]:
    return await get_opendota().hero_stats()


def parse_opendota_series(rows: list[dict[str, Any]], today: datetime) -> Series:
    """Daily buckets from ``pub_pick_trend``/``pub_win_trend``, dropping the partial last day.

    OpenDota does not date the 7 values; the last one is today's partial day, so value ``i``
    of ``n`` is taken to be the day ``n - 1 - i`` days before ``today`` (UTC).
    """
    length = max((len(r.get("pub_pick_trend") or ()) for r in rows), default=0)
    day0 = today.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets = []
    for i in range(length - 1):  # the last value is a partial day
        start = day0 - timedelta(days=length - 1 - i)
        heroes = {}
        for r in rows:
            picks, wins = r.get("pub_pick_trend") or (), r.get("pub_win_trend") or ()
            if i < len(picks) and i < len(wins) and picks[i]:
                heroes[r["id"]] = (int(picks[i]), int(wins[i]))
        buckets.append(Bucket(start, start + timedelta(days=1), "day", heroes))
    return Series(tuple(buckets), 10, "opendota")


async def fetch_opendota_series(today: datetime) -> Series:
    return parse_opendota_series(await _opendota_hero_stats(), today)


# ---------------------------------------------------------------------------- windows


@dataclass(frozen=True, slots=True)
class PatchWindow:
    name: str
    start: datetime
    end: datetime | None  # None while the patch is still live
    stratz_id: int | None = None


@cached(ttl=TTL_REFERENCE)
async def _game_versions_raw() -> dict:
    return await get_stratz().query(GAME_VERSIONS_QUERY)


def _chain(entries: list[tuple[str, datetime, int | None]]) -> list[PatchWindow]:
    entries = sorted(entries, key=lambda e: e[1])
    return [
        PatchWindow(name, start, entries[i + 1][1] if i + 1 < len(entries) else None, sid)
        for i, (name, start, sid) in enumerate(entries)
    ]


def major_patches(game_versions: dict | None = None) -> list[PatchWindow]:
    """Major patches from ``reference.patches()``, each running until the next one."""
    ids = {}
    for gv in ((game_versions or {}).get("constants") or {}).get("gameVersions") or ():
        ids[gv["name"]] = gv["id"]
    return _chain([(p.name, p.date, ids.get(p.name)) for p in reference.patches()])


def letter_patches(game_versions: dict) -> list[PatchWindow]:
    """Every STRATZ game version (7.41, 7.41a, ...), each running until the next one."""
    rows = (game_versions.get("constants") or {}).get("gameVersions") or ()
    return _chain([(gv["name"], _ts(gv["asOfDateTime"]), gv["id"]) for gv in rows])


def is_letter_patch(name: str) -> bool:
    return name[-1].isalpha()


def major_of(name: str) -> str:
    """``"7.41f"`` -> ``"7.41"``; a major name is returned unchanged."""
    return name[:-1] if is_letter_patch(name) else name


async def patch_windows(letter: bool) -> list[PatchWindow]:
    """Patch windows in release order. Letter patches need STRATZ; majors work without it."""
    if letter:
        return letter_patches(await _game_versions_raw())
    try:
        versions = await _game_versions_raw()
    except StratzError:  # STRATZ ids only add patch notes; dates come from reference data
        versions = None
    return major_patches(versions)


def find_patch(windows: list[PatchWindow], name: str) -> int:
    """Index of the patch called ``name``; raises TrendInputError listing recent names."""
    for i, w in enumerate(windows):
        if w.name == name:
            return i
    recent = ", ".join(w.name for w in windows[-6:])
    raise TrendInputError(f"unknown patch {name!r}; recent patches: {recent}")


@dataclass(frozen=True, slots=True)
class WindowSpec:
    kind: str  # "patch" or "days"
    before: str | None = None  # patch names; None means "the patch before `after`"
    after: str | None = None  # None means the current patch (or filters.patch)
    days: int | None = None


def parse_window(text: str) -> WindowSpec:
    """Parse ``"patch"``, ``"7.40->7.41"`` (or ``→``, ``-``, ``to``) or ``"14d"``."""
    clean = text.strip().lower()
    if clean in ("patch", "patches", "last patch", "previous patch"):
        return WindowSpec("patch")
    if m := _DAYS.match(clean):
        days = int(m.group(1))
        if days < 1:
            raise TrendInputError("a day window must be at least 1 day")
        return WindowSpec("days", days=days)
    if m := _PATCH_PAIR.match(clean):
        return WindowSpec("patch", before=m.group(1), after=m.group(2))
    raise TrendInputError(
        f"cannot read window {text!r}; use 'patch', '7.40->7.41' or a day count like '28d'"
    )


# ---------------------------------------------------------------------------- aggregation


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Counts summed over the buckets inside one window."""

    label: str
    start: datetime  # first covered instant
    end: datetime  # end of the last covered bucket (exclusive)
    buckets: int
    hero_picks: int  # sum of hero matchCount over every hero
    heroes: dict[int, tuple[int, int]]
    picks_per_match: int

    @property
    def matches(self) -> int:
        """Matches in the window: total hero picks divided by picks per match."""
        return round(self.hero_picks / self.picks_per_match)


def aggregate(
    series: Series, label: str, start: datetime, end: datetime | None
) -> WindowStats | None:
    """Sum the buckets lying entirely inside ``[start, end)``; None if there are none."""
    inside = [b for b in series.buckets if b.start >= start and (end is None or b.end <= end)]
    return _sum(series, label, inside)


def _sum(series: Series, label: str, buckets: list[Bucket]) -> WindowStats | None:
    if not buckets:
        return None
    heroes: dict[int, list[int]] = {}
    for b in buckets:
        for hero_id, (n, w) in b.heroes.items():
            acc = heroes.setdefault(hero_id, [0, 0])
            acc[0] += n
            acc[1] += w
    return WindowStats(
        label=label,
        start=buckets[0].start,
        end=buckets[-1].end,
        buckets=len(buckets),
        hero_picks=sum(n for n, _ in heroes.values()),
        heroes={h: (n, w) for h, (n, w) in heroes.items()},
        picks_per_match=series.picks_per_match,
    )


def day_windows(series: Series, days: int) -> tuple[WindowStats | None, WindowStats | None]:
    """The latest ``k`` buckets against the ``k`` before them, ``k`` covering ``days``.

    Weekly STRATZ buckets give ``k = ceil(days / 7)``; daily OpenDota buckets give
    ``k = min(days, half the series)``. Only buckets of the finest kind are used.
    """
    if not series.buckets:
        return None, None
    kind = series.buckets[-1].kind
    fine = [b for b in series.buckets if b.kind == kind]
    width = 7 if kind == "week" else 1
    k = max(1, math.ceil(days / width))
    if kind == "day":
        k = min(k, len(fine) // 2)
    if k == 0 or len(fine) < 2 * k:
        return None, _sum(series, "after", fine[-k:]) if k else None
    return _sum(series, "before", fine[-2 * k : -k]), _sum(series, "after", fine[-k:])


# ---------------------------------------------------------------------------- rates


@dataclass(frozen=True, slots=True)
class Rate:
    matches: int
    wins: int
    window_matches: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.matches if self.matches else 0.0

    @property
    def pick_rate(self) -> float:
        return self.matches / self.window_matches if self.window_matches else 0.0

    def win_ci(self) -> tuple[float, float]:
        return tuple(wilson_interval(self.wins, self.matches))  # type: ignore[return-value]

    def pick_ci(self) -> tuple[float, float]:
        n = max(self.window_matches, self.matches)
        return tuple(wilson_interval(self.matches, n))  # type: ignore[return-value]


def hero_rate(window: WindowStats, hero_id: int) -> Rate:
    n, w = window.heroes.get(hero_id, (0, 0))
    return Rate(n, w, max(window.matches, n))


@dataclass(slots=True)
class HeroChange:
    hero_id: int
    before: Rate
    after: Rate
    win_p: float
    pick_p: float
    win_q: float = 1.0
    pick_q: float = 1.0

    @property
    def win_delta(self) -> float:
        return self.after.win_rate - self.before.win_rate

    @property
    def pick_delta(self) -> float:
        return self.after.pick_rate - self.before.pick_rate


@dataclass(slots=True)
class Comparison:
    changes: list[HeroChange]
    skipped: list[int] = field(default_factory=list)  # heroes below min_sample in a window


def compare(before: WindowStats, after: WindowStats, min_sample: int) -> Comparison:
    """Per-hero z-tests (win rate and pick rate) with Benjamini-Hochberg across heroes.

    Win rate: wins/matches after vs before. Pick rate: matches/window matches after vs
    before (a hero appears at most once per match). Heroes with fewer than ``min_sample``
    matches in either window are skipped and do not count towards the BH ``m``.
    """
    changes, skipped = [], []
    for hero_id in sorted(set(before.heroes) | set(after.heroes)):
        b, a = hero_rate(before, hero_id), hero_rate(after, hero_id)
        if b.matches < min_sample or a.matches < min_sample:
            skipped.append(hero_id)
            continue
        win = two_proportion_ztest(a.wins, a.matches, b.wins, b.matches)
        pick = two_proportion_ztest(a.matches, a.window_matches, b.matches, b.window_matches)
        changes.append(HeroChange(hero_id, b, a, win.p_value, pick.p_value))
    if changes:
        win_q = benjamini_hochberg([c.win_p for c in changes], BH_ALPHA).adjusted
        pick_q = benjamini_hochberg([c.pick_p for c in changes], BH_ALPHA).adjusted
        for c, wq, pq in zip(changes, win_q, pick_q, strict=True):
            c.win_q, c.pick_q = wq, pq
    return Comparison(changes, skipped)


@dataclass(frozen=True, slots=True)
class Jump:
    from_label: str
    to_label: str
    before: Rate
    after: Rate
    win_q: float
    pick_q: float

    @property
    def win_delta(self) -> float:
        return self.after.win_rate - self.before.win_rate

    @property
    def pick_delta(self) -> float:
        return self.after.pick_rate - self.before.pick_rate


def jumps(points: list[tuple[str, Rate | None]], min_sample: int) -> list[Jump]:
    """Changes between consecutive points that both have ``min_sample`` matches.

    Each jump gets a two-proportion z-test for win rate and for pick rate; the p-values
    are Benjamini-Hochberg adjusted across the jumps (separately per metric). Sorted by
    absolute win-rate change, largest first.
    """
    pairs = [
        (l0, l1, r0, r1)
        for (l0, r0), (l1, r1) in zip(points, points[1:], strict=False)
        if r0 and r1 and r0.matches >= min_sample and r1.matches >= min_sample
    ]
    if not pairs:
        return []
    win_p = [
        two_proportion_ztest(a.wins, a.matches, b.wins, b.matches).p_value for _, _, b, a in pairs
    ]
    pick_p = [
        two_proportion_ztest(a.matches, a.window_matches, b.matches, b.window_matches).p_value
        for _, _, b, a in pairs
    ]
    win_q = benjamini_hochberg(win_p, BH_ALPHA).adjusted
    pick_q = benjamini_hochberg(pick_p, BH_ALPHA).adjusted
    result = [
        Jump(l0, l1, b, a, wq, pq)
        for (l0, l1, b, a), wq, pq in zip(pairs, win_q, pick_q, strict=True)
    ]
    result.sort(key=lambda j: -abs(j.win_delta))
    return result


# ---------------------------------------------------------------------------- patch notes


@cached(ttl=TTL_REFERENCE)
async def _patch_notes_raw(game_version_id: int) -> dict:
    return await get_stratz().query(PATCH_NOTES_QUERY, {"id": game_version_id})


def clean_note(text: str) -> str:
    return " ".join(_TAG.sub("", text).replace('\\"', '"').split())


async def fetch_patch_notes(game_version_id: int) -> dict[int, list[str]]:
    """STRATZ English patch-note lines per hero id for one game version (may be empty)."""
    data = await _patch_notes_raw(game_version_id)
    notes: dict[int, list[str]] = {}
    for row in (data.get("constants") or {}).get("patchNotes") or ():
        if row.get("heroId") and row.get("text"):
            notes.setdefault(row["heroId"], []).append(clean_note(row["text"]))
    return notes


def now() -> datetime:
    return datetime.now(UTC)

"""Meta trends: which heroes rose or fell, a hero's patch history, and a patch's movers."""

from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from dota_insights.analytics import trends as t
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.mcp.errors import retry_hint, source_error
from dota_insights.models import Filters, GameMode
from dota_insights.reference import hero_by_id, resolve_hero
from dota_insights.sources.opendota import OpenDotaError
from dota_insights.sources.stratz import StratzError

SOURCE_NAMES = {"stratz": "STRATZ", "opendota": "OpenDota"}
STRATZ_CAVEAT = "Hero win/match counts come from STRATZ (https://stratz.com)."
BUCKET_CAVEAT = (
    "Windows sum whole STRATZ buckets only: weeks for roughly the last 19 weeks, months "
    "before that. A bucket that straddles a window edge is left out, so 'covered_from' and "
    "'covered_to' can be narrower than the patch dates."
)
POSITION_CAVEAT = "Positions are STRATZ's own position detection, not a self-reported role."
SIGNIFICANCE_CAVEAT = (
    "Significance: two-sided two-proportion z-test per hero, then Benjamini-Hochberg across "
    f"all tested heroes at FDR {t.BH_ALPHA}. With hundreds of thousands of games tiny "
    "changes become significant, so also look at the size of the delta."
)

RATE_DEFS = {
    "win_rate": "wins / matches of the hero in the window.",
    "pick_rate": (
        "matches with the hero / all matches in the window (matches = summed hero picks / 10, "
        "or / 2 when filtered to one position)."
    ),
    "win_rate_ci / pick_rate_ci": "95% Wilson score interval.",
}


class WindowInfo(BaseModel):
    label: str = Field(description="Patch name, or 'before'/'after' for a day window.")
    patch_from: date | None = Field(None, description="Patch release date (UTC), for patches.")
    patch_to: date | None = Field(None, description="Day before the next patch; null if live.")
    covered_from: date = Field(description="First day the counts cover.")
    covered_to: date = Field(description="Last day the counts cover.")
    matches: int = Field(description="Matches in the window (all heroes).")


class RateSnapshot(BaseModel):
    matches: int
    win_rate: float
    win_rate_ci: tuple[float, float]
    pick_rate: float
    pick_rate_ci: tuple[float, float]


class HeroTrend(BaseModel):
    hero_id: int
    hero: str
    before: RateSnapshot
    after: RateSnapshot
    win_rate_delta: float = Field(description="after.win_rate - before.win_rate.")
    pick_rate_delta: float = Field(description="after.pick_rate - before.pick_rate.")
    win_rate_p: float
    win_rate_q: float = Field(description="Benjamini-Hochberg adjusted p-value.")
    pick_rate_p: float
    pick_rate_q: float = Field(description="Benjamini-Hochberg adjusted p-value.")
    significant: bool = Field(description=f"q <= {t.BH_ALPHA} for the sorted metric.")


class MetaTrends(BaseModel):
    before: WindowInfo
    after: WindowInfo
    sort_by: str
    heroes: list[HeroTrend]
    heroes_tested: int
    heroes_significant: int = Field(description="Significant movers in the chosen direction.")
    heroes_skipped: int = Field(description="Heroes below min_sample in either window.")


class PatchPoint(BaseModel):
    patch: str
    patch_from: date
    patch_to: date | None
    covered_from: date | None
    covered_to: date | None
    matches: int
    win_rate: float | None
    win_rate_ci: tuple[float, float] | None
    pick_rate: float | None
    pick_rate_ci: tuple[float, float] | None
    below_min_sample: bool


class PatchJump(BaseModel):
    from_patch: str
    to_patch: str
    win_rate_delta: float
    win_rate_q: float
    pick_rate_delta: float
    pick_rate_q: float
    significant: bool = Field(description=f"Either q <= {t.BH_ALPHA}.")


class HeroPatchHistory(BaseModel):
    hero_id: int
    hero: str
    patches: list[PatchPoint] = Field(description="Oldest first.")
    biggest_jumps: list[PatchJump] = Field(description="Largest |win_rate_delta| first.")


class Mover(BaseModel):
    hero_id: int
    hero: str
    delta: float
    before: float
    after: float
    q_value: float
    matches_after: int
    patch_notes: list[str] = Field(
        default_factory=list, description="STRATZ patch-note lines for this hero, if any."
    )


class PatchSummary(BaseModel):
    patch: WindowInfo
    previous_patch: WindowInfo
    sub_patches: list[dict[str, str]] = Field(
        default_factory=list, description="Letter patches inside a major patch: name, date."
    )
    win_rate_risers: list[Mover]
    win_rate_fallers: list[Mover]
    pick_rate_risers: list[Mover]
    pick_rate_fallers: list[Mover]
    heroes_tested: int
    patch_notes_available: bool


FiltersArg = Annotated[
    Filters | None,
    Field(description="Rank, position and game mode filters. Defaults to ranked all pick."),
]


def _day(dt: datetime) -> date:
    return dt.date()


def _last_day(end: datetime) -> date:
    return (end - timedelta(seconds=1)).date()


def _snapshot(rate: t.Rate) -> RateSnapshot:
    return RateSnapshot(
        matches=rate.matches,
        win_rate=round(rate.win_rate, 4),
        win_rate_ci=_round(rate.win_ci()),
        pick_rate=round(rate.pick_rate, 4),
        pick_rate_ci=_round(rate.pick_ci()),
    )


def _round(ci: tuple[float, float]) -> tuple[float, float]:
    return round(ci[0], 4), round(ci[1], 4)


def _window_info(stats: t.WindowStats, patch: t.PatchWindow | None = None) -> WindowInfo:
    return WindowInfo(
        label=patch.name if patch else stats.label,
        patch_from=_day(patch.start) if patch else None,
        patch_to=_last_day(patch.end) if patch and patch.end else None,
        covered_from=_day(stats.start),
        covered_to=_last_day(stats.end),
        matches=stats.matches,
    )


def _hero_name(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"hero {hero_id}"


def _common_caveats(series: t.Series, filters: Filters) -> list[str]:
    caveats = []
    if series.source == "stratz":
        caveats += [STRATZ_CAVEAT, BUCKET_CAVEAT]
        if filters.position:
            caveats.append(POSITION_CAVEAT)
    if series.end and series.end < t.now() - timedelta(days=8):
        caveats.append(
            f"The newest {SOURCE_NAMES[series.source]} bucket ends {_last_day(series.end)}; "
            "games after that are not counted yet."
        )
    return caveats


def _freshness(series: t.Series) -> datetime | None:
    return min(series.end, t.now()) if series.end else None


def _stratz_error(exc: StratzError, filters: Filters) -> ToolResult:
    envelope = source_error(exc, filters=filters, source=["stratz"])
    envelope.hint = f"{envelope.hint} Patch and rank-filtered trends need STRATZ."
    return envelope


async def _resolve_patch_pair(
    spec: t.WindowSpec, filters: Filters
) -> tuple[t.PatchWindow, t.PatchWindow]:
    after_name = spec.after or filters.patch
    letter = any(n and t.is_letter_patch(n) for n in (spec.before, after_name))
    windows = await t.patch_windows(letter)
    after_i = t.find_patch(windows, after_name) if after_name else len(windows) - 1
    before_i = t.find_patch(windows, spec.before) if spec.before else after_i - 1
    if before_i < 0:
        raise t.TrendInputError(f"no patch before {windows[after_i].name} in the reference data")
    if before_i >= after_i:
        raise t.TrendInputError("the first patch of the window must be older than the second")
    return windows[before_i], windows[after_i]


def _opendota_fallback_allowed(filters: Filters) -> bool:
    return (
        filters.rank_bracket is None
        and filters.position is None
        and filters.game_mode in (None, GameMode.RANKED_ALL_PICK, GameMode.ALL_PICK)
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def find_meta_trends(
        filters: FiltersArg = None,
        window: Annotated[
            str,
            Field(
                description=(
                    "What to compare. 'patch': the previous patch vs the current one (or vs "
                    "filters.patch). 'A->B' (e.g. '7.40->7.41' or '7.41e->7.41f'): two named "
                    "patches. 'Nd' or 'last N days' (e.g. '28d'): the latest N days vs the N "
                    "days before, rounded up to whole weeks."
                )
            ),
        ] = "patch",
        direction: Annotated[
            Literal["rising", "falling", "both"],
            Field(description="Keep heroes whose sorted metric went up, down, or either."),
        ] = "both",
        sort_by: Annotated[
            Literal["pick_rate_delta", "winrate_delta"],
            Field(description="Metric to test, filter and sort by."),
        ] = "winrate_delta",
        min_delta: Annotated[
            float,
            Field(
                ge=0,
                le=1,
                description=(
                    "Smallest absolute change to report, as a fraction (0.005 = 0.5 "
                    "percentage points). Filters out significant but negligible changes."
                ),
            ),
        ] = 0.005,
        limit: Annotated[int, Field(ge=1, le=127, description="Most heroes to return.")] = 10,
    ) -> ToolResult[MetaTrends]:
        """Find heroes whose win rate or pick rate changed between two patches or two periods.

        Each hero is tested with a two-proportion z-test and the p-values are corrected with
        Benjamini-Hochberg across heroes, so only changes unlikely to be noise are returned.
        """
        f = t.effective_filters(filters)
        try:
            spec = t.parse_window(window)
        except t.TrendInputError as exc:
            return error(str(exc), filters=f)

        patches: tuple[t.PatchWindow, t.PatchWindow] | None = None
        try:
            series = await t.fetch_series(f)
            if spec.kind == "days":
                before, after = t.day_windows(series, spec.days)
            else:
                patches = await _resolve_patch_pair(spec, f)
                before = t.aggregate(series, patches[0].name, patches[0].start, patches[0].end)
                after = t.aggregate(series, patches[1].name, patches[1].start, patches[1].end)
        except t.TrendInputError as exc:
            return error(str(exc), filters=f)
        except StratzError as exc:
            if spec.kind != "days" or not _opendota_fallback_allowed(f):
                return _stratz_error(exc, f)
            try:
                series = await t.fetch_opendota_series(t.now())
            except OpenDotaError as od_exc:
                return error(
                    f"STRATZ and OpenDota both failed: {exc}; {od_exc}",
                    hint=retry_hint(od_exc),
                    filters=f,
                    source=["stratz", "opendota"],
                )
            f = f.model_copy(update={"game_mode": None})
            before, after = t.day_windows(series, spec.days)

        source = [series.source]
        caveats = _common_caveats(series, f)
        if series.source == "opendota":
            caveats.append(
                "STRATZ was unavailable, so this uses OpenDota's 7-day public-match series "
                "(all public modes and ranks): at most 3 days vs the 3 days before."
            )
        if before is None or after is None:
            return insufficient_data(
                "No hero data covers one of the two windows.",
                "Use a shorter day window, or patches from 2025 onward.",
                filters=f,
                caveats=caveats,
                source=source,
            )
        sample = before.matches + after.matches
        if after.matches < f.min_sample or before.matches < f.min_sample:
            return insufficient_data(
                f"Only {before.matches} and {after.matches} matches in the two windows.",
                "Widen rank_bracket, drop position, or pick longer windows.",
                filters=f,
                sample_size=sample,
                caveats=caveats,
                source=source,
            )

        comparison = t.compare(before, after, f.min_sample)
        by_win = sort_by == "winrate_delta"
        picked = []
        for c in comparison.changes:
            delta = c.win_delta if by_win else c.pick_delta
            q = c.win_q if by_win else c.pick_q
            if q > t.BH_ALPHA or abs(delta) < min_delta:
                continue
            if (direction == "rising" and delta <= 0) or (direction == "falling" and delta >= 0):
                continue
            picked.append((delta, q, c))
        picked.sort(key=lambda x: -abs(x[0]))
        heroes = [
            HeroTrend(
                hero_id=c.hero_id,
                hero=_hero_name(c.hero_id),
                before=_snapshot(c.before),
                after=_snapshot(c.after),
                win_rate_delta=round(c.win_delta, 4),
                pick_rate_delta=round(c.pick_delta, 4),
                win_rate_p=c.win_p,
                win_rate_q=c.win_q,
                pick_rate_p=c.pick_p,
                pick_rate_q=c.pick_q,
                significant=True,
            )
            for _, _, c in picked[:limit]
        ]
        if comparison.skipped:
            caveats.append(
                f"{len(comparison.skipped)} heroes had fewer than {f.min_sample} matches in a "
                "window and were not tested."
            )
        caveats.append(SIGNIFICANCE_CAVEAT)
        if not heroes:
            caveats.append(
                "No hero passed the significance and min_delta filters: the meta did not "
                "measurably move in this direction."
            )
        metric = "win_rate" if by_win else "pick_rate"
        confidence = {}
        for h in heroes:
            confidence[f"{h.hero}.before.{metric}"] = getattr(h.before, f"{metric}_ci")
            confidence[f"{h.hero}.after.{metric}"] = getattr(h.after, f"{metric}_ci")
        result = MetaTrends(
            before=_window_info(before, patches[0] if patches else None),
            after=_window_info(after, patches[1] if patches else None),
            sort_by=sort_by,
            heroes=heroes,
            heroes_tested=len(comparison.changes),
            heroes_significant=len(picked),
            heroes_skipped=len(comparison.skipped),
        )
        return ok(
            result,
            filters=f,
            sample_size=sample,
            period=Period(from_date=_day(before.start), to_date=_last_day(after.end)),
            definitions={
                **RATE_DEFS,
                "win_rate_delta / pick_rate_delta": "after minus before, as a fraction.",
                "win_rate_q / pick_rate_q": "Benjamini-Hochberg adjusted p-value across heroes.",
                "sample_size": "Matches in both windows together.",
            },
            confidence=confidence,
            caveats=caveats,
            source=source,
            data_freshness=_freshness(series),
        )

    @mcp.tool
    async def get_hero_patch_history(
        hero: Annotated[str, Field(description="Hero name or nickname, e.g. 'Anti-Mage', 'am'.")],
        filters: FiltersArg = None,
        patches: Annotated[
            int, Field(ge=2, le=12, description="How many patches back, ending at filters.patch.")
        ] = 5,
        letter_patches: Annotated[
            bool,
            Field(description="Use letter patches (7.41a, 7.41b, ...) instead of major ones."),
        ] = False,
    ) -> ToolResult[HeroPatchHistory]:
        """A hero's win rate and pick rate per patch, with intervals and the biggest jumps."""
        f = t.effective_filters(filters)
        resolution = resolve_hero(hero)
        target = resolution.best
        if target is None:
            names = ", ".join(c.record.display_name for c in resolution.candidates[:5])
            return error(
                f"hero {hero!r} is {'ambiguous' if names else 'unknown'}"
                + (f": {names}" if names else ""),
                hint="Use the full hero name.",
                filters=f,
            )
        try:
            windows = await t.patch_windows(letter_patches)
            end_i = t.find_patch(windows, f.patch) if f.patch else len(windows) - 1
            series = await t.fetch_series(f)
        except t.TrendInputError as exc:
            return error(str(exc), filters=f)
        except StratzError as exc:
            return _stratz_error(exc, f)

        selected = windows[max(0, end_i - patches + 1) : end_i + 1]
        points, stats_list = [], []
        for w in selected:
            stats = t.aggregate(series, w.name, w.start, w.end)
            rate = t.hero_rate(stats, target.id) if stats else None
            stats_list.append((w, stats, rate))
            points.append(
                PatchPoint(
                    patch=w.name,
                    patch_from=_day(w.start),
                    patch_to=_last_day(w.end) if w.end else None,
                    covered_from=_day(stats.start) if stats else None,
                    covered_to=_last_day(stats.end) if stats else None,
                    matches=rate.matches if rate else 0,
                    win_rate=round(rate.win_rate, 4) if rate and rate.matches else None,
                    win_rate_ci=_round(rate.win_ci()) if rate and rate.matches else None,
                    pick_rate=round(rate.pick_rate, 4) if rate else None,
                    pick_rate_ci=_round(rate.pick_ci()) if rate and rate.window_matches else None,
                    below_min_sample=not rate or rate.matches < f.min_sample,
                )
            )

        caveats = _common_caveats(series, f)
        missing = [p.patch for p in points if p.covered_from is None]
        if missing:
            caveats.append(f"No STRATZ data covers {', '.join(missing)}.")
        sample = sum(p.matches for p in points)
        if sample < f.min_sample:
            return insufficient_data(
                f"{target.display_name} has only {sample} matches over these patches.",
                "Widen rank_bracket, drop position, or lower min_sample.",
                filters=f,
                sample_size=sample,
                caveats=caveats,
                source=["stratz"],
            )

        jumps = [
            PatchJump(
                from_patch=j.from_label,
                to_patch=j.to_label,
                win_rate_delta=round(j.win_delta, 4),
                win_rate_q=j.win_q,
                pick_rate_delta=round(j.pick_delta, 4),
                pick_rate_q=j.pick_q,
                significant=min(j.win_q, j.pick_q) <= t.BH_ALPHA,
            )
            for j in t.jumps([(w.name, r) for w, _, r in stats_list], f.min_sample)
        ]
        caveats.append(
            "Jumps compare consecutive patches with enough matches; q-values are "
            "Benjamini-Hochberg adjusted across those jumps."
        )
        if any(p.below_min_sample for p in points if p.covered_from):
            caveats.append(f"Patches flagged below_min_sample have under {f.min_sample} matches.")

        confidence = {}
        for p in points:
            if p.win_rate_ci and not p.below_min_sample:
                confidence[f"{p.patch}.win_rate"] = p.win_rate_ci
            if p.pick_rate_ci and not p.below_min_sample:
                confidence[f"{p.patch}.pick_rate"] = p.pick_rate_ci
        covered = [s for _, s, _ in stats_list if s]
        return ok(
            HeroPatchHistory(
                hero_id=target.id, hero=target.display_name, patches=points, biggest_jumps=jumps
            ),
            filters=f,
            sample_size=sample,
            period=Period(from_date=_day(covered[0].start), to_date=_last_day(covered[-1].end)),
            definitions={
                **RATE_DEFS,
                "sample_size": f"{target.display_name}'s matches summed over the patches.",
                "biggest_jumps": "Change from one patch to the next, after minus before.",
            },
            confidence=confidence,
            caveats=caveats,
            source=["stratz"],
            data_freshness=_freshness(series),
        )

    @mcp.tool
    async def get_patch_summary(
        patch: Annotated[
            str | None,
            Field(description="Patch such as '7.41' or '7.41f'. Omit for the latest major patch."),
        ] = None,
        filters: FiltersArg = None,
        limit: Annotated[int, Field(ge=1, le=20, description="Movers per list.")] = 5,
    ) -> ToolResult[PatchSummary]:
        """What changed in a patch: release dates and the heroes whose win or pick rate moved
        most compared with the previous patch, with STRATZ patch notes when it has them."""
        f = t.effective_filters(filters)
        name = patch or f.patch
        try:
            spec = t.WindowSpec("patch", after=name)
            previous, current = await _resolve_patch_pair(spec, f)
            f = f.model_copy(update={"patch": current.name})
            series = await t.fetch_series(f)
            sub_patches = []
            if not t.is_letter_patch(current.name):
                try:
                    letters = await t.patch_windows(letter=True)
                except StratzError:
                    letters = []
                sub_patches = [
                    {"name": w.name, "date": _day(w.start).isoformat()}
                    for w in letters
                    if t.major_of(w.name) == current.name
                ]
        except t.TrendInputError as exc:
            return error(str(exc), filters=f)
        except StratzError as exc:
            return _stratz_error(exc, f)

        before = t.aggregate(series, previous.name, previous.start, previous.end)
        after = t.aggregate(series, current.name, current.start, current.end)
        caveats = _common_caveats(series, f)
        if before is None or after is None:
            return insufficient_data(
                f"No STRATZ data covers {previous.name if before is None else current.name}.",
                "Pick a patch from 2025 onward.",
                filters=f,
                caveats=caveats,
                source=["stratz"],
            )
        sample = before.matches + after.matches
        if before.matches < f.min_sample or after.matches < f.min_sample:
            return insufficient_data(
                f"Only {before.matches} and {after.matches} matches in the two patches.",
                "Widen rank_bracket or drop position.",
                filters=f,
                sample_size=sample,
                caveats=caveats,
                source=["stratz"],
            )

        notes: dict[int, list[str]] = {}
        if current.stratz_id is not None:
            try:
                notes = await t.fetch_patch_notes(current.stratz_id)
            except StratzError:
                notes = {}
        if notes:
            caveats.append(
                "patch_notes are STRATZ's English patch-note lines for the hero (up to 6), "
                "shown beside the stat change; they do not prove the change caused it."
            )
        else:
            caveats.append(
                f"STRATZ has no patch notes for {current.name}, so none are shown; see the "
                "official Dota 2 patch notes for what changed."
            )

        comparison = t.compare(before, after, f.min_sample)

        def movers(metric: str, rising: bool) -> list[Mover]:
            rows = []
            for c in comparison.changes:
                delta = c.win_delta if metric == "win" else c.pick_delta
                q = c.win_q if metric == "win" else c.pick_q
                if q > t.BH_ALPHA or (delta <= 0 if rising else delta >= 0):
                    continue
                rate_b = c.before.win_rate if metric == "win" else c.before.pick_rate
                rate_a = c.after.win_rate if metric == "win" else c.after.pick_rate
                rows.append(
                    Mover(
                        hero_id=c.hero_id,
                        hero=_hero_name(c.hero_id),
                        delta=round(delta, 4),
                        before=round(rate_b, 4),
                        after=round(rate_a, 4),
                        q_value=q,
                        matches_after=c.after.matches,
                        patch_notes=notes.get(c.hero_id, [])[:6],
                    )
                )
            rows.sort(key=lambda m: -abs(m.delta))
            return rows[:limit]

        caveats.append(SIGNIFICANCE_CAVEAT + " Only significant movers are listed.")
        if comparison.skipped:
            caveats.append(
                f"{len(comparison.skipped)} heroes had fewer than {f.min_sample} matches in a "
                "patch and were not tested."
            )
        return ok(
            PatchSummary(
                patch=_window_info(after, current),
                previous_patch=_window_info(before, previous),
                sub_patches=sub_patches,
                win_rate_risers=movers("win", True),
                win_rate_fallers=movers("win", False),
                pick_rate_risers=movers("pick", True),
                pick_rate_fallers=movers("pick", False),
                heroes_tested=len(comparison.changes),
                patch_notes_available=bool(notes),
            ),
            filters=f,
            sample_size=sample,
            period=Period(from_date=_day(before.start), to_date=_last_day(after.end)),
            definitions={
                **RATE_DEFS,
                "delta": "Rate in this patch minus rate in the previous patch, as a fraction.",
                "q_value": "Benjamini-Hochberg adjusted p-value across heroes.",
                "patch dates": "Major patch dates from reference data; letter patches from STRATZ.",
                "sample_size": "Matches in both patches together.",
            },
            caveats=caveats,
            source=["stratz"],
            data_freshness=_freshness(series),
        )

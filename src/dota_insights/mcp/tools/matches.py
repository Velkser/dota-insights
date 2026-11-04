"""Single-match tools: overview, timeline, turning points, teamfights, vision and laning."""

from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import matches as am
from dota_insights.mcp.envelope import Period, ToolResult, error, insufficient_data, ok
from dota_insights.mcp.errors import source_error
from dota_insights.sources.opendota import OpenDotaError

MatchId = Annotated[
    int,
    Field(
        gt=0,
        description=(
            "Dota 2 match id, the number in the match URL on OpenDota, STRATZ or Dotabuff "
            "(e.g. 9073999945)."
        ),
    ),
]

_SIDES = (
    "Teams are 'radiant' and 'dire'; player_slot 0-4 is Radiant, 128-132 is Dire. "
    "Times are game clock (mm:ss, negative before the horn) with *_s in seconds."
)
_GOLD_ADV = (
    "OpenDota radiant_gold_adv: Radiant's total gold minus Dire's at each minute "
    "(positive = Radiant ahead). xp_adv is the same for experience."
)
_PARSE_CAVEAT = (
    "Replay-derived data (timelines, events, teamfights, wards, lanes) exists only for matches "
    "OpenDota has parsed; about 8% of public matches are parsed without a request."
)


def _period(raw: dict[str, Any]) -> Period | None:
    start, end = am.match_time(raw)
    if start is None or end is None:
        return None
    return Period(from_date=start.date(), to_date=end.date())


def _unparsed(match: am.MatchData, what: str) -> ToolResult:
    """insufficient_data for a parsed-only tool, listing what the unparsed match does show."""
    raw = match.raw
    b = am.basics(raw)
    if match.parse_requested:
        reason = (
            f"Match {b['match_id']} is not parsed yet, so {what} is not available. "
            "A replay parse was requested from OpenDota."
        )
        hint = (
            "Retry in about a minute; a fresh match can take several minutes until its replay "
            "is downloadable. analyze_match already works on the unparsed data."
        )
    else:
        reason = (
            f"Match {b['match_id']} is not parsed, so {what} is not available, and the parse "
            f"request failed ({match.parse_error})."
        )
        hint = "Retry in a few minutes. analyze_match already works on the unparsed data."
    _, end = am.match_time(raw)
    return insufficient_data(
        reason,
        hint,
        sample_size=1,
        period=_period(raw),
        caveats=[
            f"Available without parsing: {b['winner']} won in {b['duration']}, score "
            f"{b['score']['radiant']}-{b['score']['dire']} (Radiant-Dire), first blood at "
            f"{b['first_blood_time']}.",
            _PARSE_CAVEAT,
        ],
        source=["opendota"],
        data_freshness=end,
    )


async def _run(
    match_id: int,
    build: Callable[[am.MatchData], ToolResult],
    *,
    parsed_only: str | None,
) -> ToolResult:
    try:
        match = await am.load_match(match_id)
    except am.MatchNotFoundError:
        return error(
            f"OpenDota has no match {match_id}.",
            hint="Check the id; matches appear on OpenDota about 5 minutes after they end.",
            source=["opendota"],
        )
    except OpenDotaError as exc:
        return source_error(exc, source=["opendota"])
    if parsed_only and not match.parsed:
        return _unparsed(match, parsed_only)
    return build(match)


def _ok(raw: dict[str, Any], result: Any, definitions: dict[str, str], caveats: list[str]):
    _, end = am.match_time(raw)
    return ok(
        result,
        sample_size=1,
        period=_period(raw),
        definitions={"sides": _SIDES, **definitions},
        caveats=caveats,
        source=["opendota"],
        data_freshness=end,
    )


def _analyze(match: am.MatchData) -> ToolResult:
    raw = match.raw
    weights = ", ".join(f"{w:.2f} x {k}" for k, w in am.CONTRIBUTION_WEIGHTS.items())
    caveats = [
        "One match: these numbers describe this game only, not a hero's or player's level.",
        "The contribution score is a heuristic ranking of visible stats; it does not see "
        "vision, stacking, space created or stuns.",
    ]
    if not match.parsed:
        state = (
            "a replay parse was requested; retry in about a minute"
            if match.parse_requested
            else f"the parse request failed ({match.parse_error})"
        )
        caveats.insert(
            0,
            f"Match is not parsed ({state}), so gold_swing (net-worth swing and comeback) and "
            "stuns are null. Everything else comes from the Steam API data.",
        )
    return _ok(
        raw,
        am.match_summary(raw),
        {
            "kda": "(kills + assists) / max(1, deaths).",
            "gpm / xpm": "Gold and experience per minute over the whole match (OpenDota).",
            "net_worth": "Gold plus item value at the end of the match.",
            "contribution score": (
                f"Weighted sum of the player's share of their own team's totals: {weights}. "
                "kill_participation = (kills + assists) / team kills. Range 0-1; the top 3 "
                "overall and the top player per team are listed."
            ),
            "gold_swing": (
                f"From {_GOLD_ADV} max_*_lead is each side's largest lead and the minute it "
                "occurred; swing_range = max Radiant lead + max Dire lead; winner_max_deficit "
                "is the largest lead the losing side ever held."
            ),
            "comeback": (
                f"true when winner_max_deficit >= {am.COMEBACK_THRESHOLD} gold, i.e. the winner "
                "overcame at least that deficit."
            ),
        },
        caveats,
    )


def _timeline(include_consumables: bool) -> Callable[[am.MatchData], ToolResult]:
    def build(match: am.MatchData) -> ToolResult:
        return _ok(
            match.raw,
            am.timeline_summary(match.raw, include_consumables=include_consumables),
            {
                "advantage_by_minute": _GOLD_ADV,
                "events": (
                    "first_blood, tower, barracks, ancient (lost_by = side that lost the "
                    "building, destroyed_by = hero or 'creeps'), roshan and tormentor (side "
                    "that killed it), aegis pickups (also aegis_stolen / aegis_denied), buyback."
                ),
                "item_purchases": (
                    "Each player's purchase log by game time"
                    + ("." if include_consumables else ", without consumables and wards.")
                ),
            },
            [
                "Item purchases include components; a finished item appears when its last "
                "component or recipe was bought.",
                _PARSE_CAVEAT,
            ],
        )

    return build


def _turning_points(window: int, top: int) -> Callable[[am.MatchData], ToolResult]:
    def build(match: am.MatchData) -> ToolResult:
        points = am.turning_points(match.raw, window=window, top=top)
        return _ok(
            match.raw,
            {**am.basics(match.raw), "window_minutes": window, "turning_points": points},
            {
                "gold_adv_change_radiant": (
                    f"radiant_gold_adv[to_minute] - radiant_gold_adv[from_minute] over a "
                    f"{window}-minute window; positive = the swing favoured Radiant. {_GOLD_ADV}"
                ),
                "selection": (
                    f"The {top} windows with the largest absolute change, keeping only windows "
                    "that do not overlap a larger one; listed in time order."
                ),
                "teamfights / objectives": (
                    "Teamfights overlapping the window and buildings, Roshan, Tormentor, aegis "
                    "and buybacks inside it. gold_swing_radiant of a fight = Radiant's total "
                    "gold_delta in the fight minus Dire's."
                ),
            },
            [
                "Gold advantage moves with farm as well as fights; a swing with no teamfight "
                "or objective attached was likely farm, pickoffs or a split push.",
                _PARSE_CAVEAT,
            ],
        )

    return build


def _teamfights(match: am.MatchData) -> ToolResult:
    return _ok(
        match.raw,
        am.teamfight_summary(match.raw),
        {
            "teamfight": "OpenDota's parser fight detection (a cluster of hero deaths).",
            "participants": "Players who dealt damage, healed or died in the fight.",
            "damage / healing": "Hero damage dealt and healing done during the fight.",
            "gold_delta / xp_delta": "Gold and experience the player gained during the fight.",
            "gold_swing_radiant": "Sum of Radiant gold_delta minus sum of Dire gold_delta.",
            "winner": "Side with the larger total gold_delta in the fight (null on a tie).",
            "match_stuns": "Seconds of stun the player applied over the whole match.",
        },
        [
            "OpenDota does not record stuns per teamfight, only per match (match_stuns).",
            _PARSE_CAVEAT,
        ],
    )


def _vision(match: am.MatchData) -> ToolResult:
    return _ok(
        match.raw,
        am.vision_summary(match.raw),
        {
            "placed": "Wards the player planted (obs_log / sen_log).",
            "destroyed_by_enemy": (
                "The player's wards that left the map killed by anything other than a hero of "
                "their own team (enemy heroes, creeps)."
            ),
            "avg_lifetime_s": (
                "Mean seconds from placement until the ward left the map, over wards that left "
                "before the game ended; full duration is about 360 s for observers and 420 s "
                "for sentries."
            ),
            "dewards": "Enemy observer and sentry wards the player killed.",
            "vs_match_average": "Player's value minus the mean over all ten players.",
        },
        [
            "Ward counts reward supports by design; compare players of the same role.",
            _PARSE_CAVEAT,
        ],
    )


def _laning(match: am.MatchData) -> ToolResult:
    minute = am.LANING_MINUTE
    return _ok(
        match.raw,
        am.laning_summary(match.raw, minute),
        {
            f"last_hits_{minute} / denies_{minute} / gold_{minute} / xp_{minute}": (
                f"Cumulative values at {minute}:00 (lh_t, dn_t, gold_t, xp_t)."
            ),
            "lane / lane_role": (
                "Map lane the parser assigned (top, middle, bottom) and role in it "
                "(safe, mid, off, jungle)."
            ),
            "lane outcome": (
                f"Compares gold + XP at {minute}:00 of each side's non-roaming heroes in the "
                f"lane; radiant_margin = (Radiant - Dire) / larger total. A side wins the lane "
                f"when the margin is at least {am.LANE_WIN_MARGIN:.0%}, otherwise 'even'."
            ),
            "early_kills": f"Hero kills before {minute}:00.",
            "rotation": (
                "true when the killer's assigned lane differs from the victim's, a proxy for a "
                "gank or rotation (kill positions are not used)."
            ),
            "lane_efficiency_pct": "OpenDota: lane gold earned vs. a perfect lane, in percent.",
        },
        [
            "Lane assignment comes from the parser's hero positions in the first minutes and "
            "can be wrong for heroes that swapped lanes.",
            _PARSE_CAVEAT,
        ],
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def analyze_match(match_id: MatchId) -> ToolResult:
        """Overview of one match: result, duration, draft, every player's KDA, GPM, XPM, net
        worth, damage and healing, the biggest contributors by a documented score, the largest
        gold leads of each side and whether the winner came back from a deficit. Works on
        unparsed matches (then the gold swing is null and a parse is requested)."""
        return await _run(match_id, _analyze, parsed_only=None)

    @mcp.tool
    async def get_match_timeline(
        match_id: MatchId,
        include_consumables: Annotated[
            bool,
            Field(description="Include tangos, salves, TPs, wards, smokes and dust in purchases."),
        ] = False,
    ) -> ToolResult:
        """Per-minute gold and XP advantage, key events (first blood, towers, barracks, Roshan,
        aegis, Tormentor, buybacks) and each player's item purchases by time. Needs a parsed
        match; for an unparsed one a parse is requested and you should retry in a minute."""
        return await _run(match_id, _timeline(include_consumables), parsed_only="the timeline")

    @mcp.tool
    async def find_turning_points(
        match_id: MatchId,
        window_minutes: Annotated[
            int, Field(ge=1, le=10, description="Length of the window a swing is measured over.")
        ] = am.TURNING_POINT_WINDOW,
        top: Annotated[int, Field(ge=1, le=10, description="How many swings to return.")] = 5,
    ) -> ToolResult:
        """The largest gold-advantage swings of a match over short windows, each linked to the
        teamfights and objectives (towers, barracks, Roshan, Tormentor, buybacks) around it.
        Use to explain where a game was won or thrown. Needs a parsed match."""
        return await _run(
            match_id, _turning_points(window_minutes, top), parsed_only="the gold timeline"
        )

    @mcp.tool
    async def analyze_teamfights(match_id: MatchId) -> ToolResult:
        """Every teamfight of a match: time, participants, deaths per side, gold and XP swing,
        which side won it, and each participant's damage, healing, kills, deaths and buybacks;
        plus each player's stun time over the match. Needs a parsed match."""
        return await _run(match_id, _teamfights, parsed_only="teamfight data")

    @mcp.tool
    async def analyze_vision(match_id: MatchId) -> ToolResult:
        """Observer and sentry wards per player and team: placed, destroyed by the enemy,
        average lifetime, dewards, and each player's difference from the match average.
        Needs a parsed match."""
        return await _run(match_id, _vision, parsed_only="ward data")

    @mcp.tool
    async def analyze_laning_phase(match_id: MatchId) -> ToolResult:
        """Laning phase of a match: last hits, denies, gold and XP at 10:00 per player, lane
        assignments, who won each lane, and kills before 10:00 with a rotation flag. Needs a
        parsed match."""
        return await _run(match_id, _laning, parsed_only="lane data")

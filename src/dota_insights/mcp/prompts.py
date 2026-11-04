"""MCP prompts: ready-made requests that steer the client to the right tools in order.

Each prompt renders a user message naming the tools to call (in backticks, so tests can check
they exist) and how to report the envelopes they return.
"""

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

REPORTING = (
    "Every tool returns an envelope. Read `status` first: on insufficient_data follow the "
    "`hint` to widen the filters and call again; on error tell me the `message` and whether "
    "the `hint` says to retry. Quote sample sizes, confidence intervals and caveats with the "
    "numbers, and do not present low-sample or correlational results as facts."
)

PlayerArg = Annotated[
    str,
    Field(
        description=(
            "Dota account ID, SteamID64, or a Steam, OpenDota, STRATZ or Dotabuff profile URL."
        )
    ),
]


def analyze_last_match(player: PlayerArg) -> str:
    """Break down a player's most recent match and what to improve."""
    return f"""Analyze my last Dota 2 match. My account: {player}.

1. Call `get_player_profile` and take the first game in `last_20.games` (the most recent);
   note its match_id, hero and result.
2. Call `analyze_match` with that match_id for the draft, KDA, farm and items of every player.
3. Call `analyze_laning_phase` and `find_turning_points` for the same match; if the match is
   parsed, add `analyze_teamfights` and `analyze_vision`.
4. Compare my GPM, XPM and last hits with `get_hero_benchmarks` for my hero.
5. Summarise: why the game was won or lost, my two or three biggest mistakes, and one concrete
   thing to do differently next game.

{REPORTING}"""


def pick_hero_for_style(
    style: Annotated[
        str,
        Field(
            description=(
                'How you like to play, e.g. "aggressive early ganks", "late-game farming carry", '
                '"teamfight initiator", "save-heavy support".'
            )
        ),
    ],
    position: Annotated[
        str | None,
        Field(description="Preferred position 1-5 (carry, mid, offlane, soft or hard support)."),
    ] = None,
    rank_bracket: Annotated[
        str | None,
        Field(description='Your rank, e.g. "archon" or "divine+". Omit for all ranks.'),
    ] = None,
    player: Annotated[
        str | None,
        Field(description="Optional account ID or profile URL to factor in your hero pool."),
    ] = None,
) -> str:
    """Suggest heroes that fit a play style, position and rank, backed by current stats."""
    scope = ", ".join(
        part
        for part in (
            f"position {position}" if position else "",
            f"rank {rank_bracket}" if rank_bracket else "",
        )
        if part
    )
    pool = (
        f"\n4. Call `get_player_hero_pool` for {player} and prefer heroes I already win with, "
        "or explain why a new hero is worth learning."
        if player
        else ""
    )
    filters = f" Filters: {scope}." if scope else ""
    last = 5 if player else 4
    return f"""Pick a Dota 2 hero for my play style: {style}.{filters}

1. Use `lookup_reference` to shortlist heroes whose roles and abilities fit the style.
2. Call `get_tier_list` with the same position and rank filters, and keep shortlisted heroes
   that rank well; use `find_hidden_gems` for strong low-pick options.
3. For the top three, call `get_hero_overview` (win rate, matchups, intervals) and
   `get_hero_builds` (a starting build).{pool}
{last}. Recommend one hero first, with two alternatives, why each fits the style, and a
   build.

{REPORTING}"""


def patch_meta_overview(
    patch: Annotated[
        str | None,
        Field(description='Patch, e.g. "7.39c". Omit for the current patch.'),
    ] = None,
    rank_bracket: Annotated[
        str | None,
        Field(description='Rank bracket, e.g. "immortal" or "legend+". Omit for all ranks.'),
    ] = None,
) -> str:
    """Overview of the meta in a patch: what changed, top heroes, rising picks, pro trends."""
    target = f"patch {patch}" if patch else "the current patch"
    rank = f" in {rank_bracket}" if rank_bracket else ""
    return f"""Give me an overview of the Dota 2 meta in {target}{rank}.

1. Call `get_patch_summary` for what changed and which heroes moved.
2. Call `find_meta_trends` for heroes rising and falling in win and pick rate.
3. Call `get_tier_list` per position (1 to 5) for the strongest heroes now.
4. Call `get_pro_matches` or `get_tournament_stats` for the most picked and banned pro heroes,
   and `get_item_overview` for any item the trends point to.
5. Summarise in a short brief: three biggest winners and losers, the best hero per position,
   and where pub and pro meta disagree.

{REPORTING}"""


def check_dota_myth(
    claim: Annotated[
        str,
        Field(
            description=(
                'The claim to test, e.g. "Radiant wins more", "BKB before 20 minutes always '
                'wins", "Pudge is a noob stomper in low ranks".'
            )
        ),
    ],
) -> str:
    """Test a common Dota belief against match data and give a verdict."""
    return f"""Check this Dota 2 claim against real match data: "{claim}".

1. Restate the claim as something measurable (which metric, hero, item, rank or patch).
2. Pick the tools that measure it, for example: `get_general_stats` (Radiant vs Dire, match
   length), `analyze_item` or `compare_items` (item timing), `get_hero_overview` or
   `compare_heroes` (hero strength by rank), `get_hero_power_curve` (early vs late heroes),
   `get_hero_matchups` (counters), `get_hero_patch_history` (was it true in older patches).
3. Run them with filters that match the claim, and a broader baseline to compare against.
4. Give a verdict: true, false, partly true or not testable with this data, with the numbers,
   sample sizes and confidence intervals. Remind me that win rates are correlational.

{REPORTING}"""


PROMPTS = (analyze_last_match, pick_hero_for_style, patch_meta_overview, check_dota_myth)


def register_prompts(mcp: FastMCP) -> None:
    """Register every prompt in ``PROMPTS`` on ``mcp``."""
    for fn in PROMPTS:
        mcp.prompt(fn)

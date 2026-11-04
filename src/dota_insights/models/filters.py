"""Shared filter vocabulary for every analytics tool.

The enums and the `Filters` model are part of the public MCP contract: their JSON schema
is shown to AI callers, so descriptions are written for them.
"""

from enum import IntEnum, StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

DEFAULT_MIN_SAMPLE = 100


class Position(IntEnum):
    """Farm priority role, 1 (most farm) to 5 (least farm)."""

    CARRY = 1
    MID = 2
    OFFLANE = 3
    SOFT_SUPPORT = 4
    HARD_SUPPORT = 5

    @classmethod
    def parse(cls, value: Any) -> Any:
        """Accept 1-5, "1"-"5", "pos1"-"pos5" or a role name such as "soft_support"."""
        if isinstance(value, str):
            text = value.strip().lower().replace(" ", "_").replace("-", "_")
            for prefix in ("position_", "position", "pos_", "pos"):
                if text.startswith(prefix):
                    text = text.removeprefix(prefix)
                    break
            if text.isdigit():
                return int(text)
            try:
                return cls[text.upper()]
            except KeyError:
                return value
        return value


class RankBracket(StrEnum):
    """Matchmaking medal, ordered from lowest to highest."""

    HERALD = "herald"
    GUARDIAN = "guardian"
    CRUSADER = "crusader"
    ARCHON = "archon"
    LEGEND = "legend"
    ANCIENT = "ancient"
    DIVINE = "divine"
    IMMORTAL = "immortal"

    @property
    def tier(self) -> int:
        """1 for Herald up to 8 for Immortal."""
        return list(RankBracket).index(self) + 1


class GameMode(StrEnum):
    """Dota 2 game mode. `ranked_all_pick` is ranked matchmaking; `all_pick` is unranked."""

    RANKED_ALL_PICK = "ranked_all_pick"
    ALL_PICK = "all_pick"
    TURBO = "turbo"
    CAPTAINS_MODE = "captains_mode"
    SINGLE_DRAFT = "single_draft"
    RANDOM_DRAFT = "random_draft"
    ALL_RANDOM = "all_random"
    ABILITY_DRAFT = "ability_draft"


class Outcome(StrEnum):
    """Which matches to include by result for the subject (hero, player, team)."""

    WIN = "win"
    LOSS = "loss"
    ALL = "all"


_BRACKET_NAMES = "|".join(b.value for b in RankBracket)
RANK_RANGE_PATTERN = rf"^({_BRACKET_NAMES})(\+|-({_BRACKET_NAMES}))?$"


def parse_rank_range(value: str) -> tuple[RankBracket, RankBracket]:
    """Turn "divine", "divine+" or "archon-legend" into an inclusive (low, high) pair."""
    text = value.strip().lower()
    if text.endswith("+"):
        return RankBracket(text[:-1]), RankBracket.IMMORTAL
    low, sep, high = text.partition("-")
    lo = RankBracket(low)
    hi = RankBracket(high) if sep else lo
    if hi.tier < lo.tier:
        raise ValueError(f"rank range {value!r} goes from high to low; write it low-high")
    return lo, hi


def _normalize_rank_range(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip().lower().replace(" ", "")
    lo, hi = parse_rank_range(text)
    if lo == hi:
        return lo.value
    if hi is RankBracket.IMMORTAL and text.endswith("+"):
        return f"{lo.value}+"
    return f"{lo.value}-{hi.value}"


RankRange = Annotated[
    str,
    BeforeValidator(_normalize_rank_range),
    Field(pattern=RANK_RANGE_PATTERN),
]


class Filters(BaseModel):
    """Which matches a statistic is computed over. Omitted fields mean "no restriction"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    patch: str | None = Field(
        default=None,
        description=(
            'Game patch such as "7.37" or "7.37d". Omit to use the current patch. '
            "Results from different patches are not comparable; widen to an older patch "
            "only if the user asks or the current patch has too few matches."
        ),
        examples=["7.37d"],
    )
    rank_bracket: RankRange | None = Field(
        default=None,
        description=(
            'Medal bracket, lowercase. One bracket ("legend"), that bracket and above '
            '("divine+"), or an inclusive range ("archon-legend"). Brackets from low to '
            "high: herald, guardian, crusader, archon, legend, ancient, divine, immortal. "
            "Omit for all brackets."
        ),
        examples=["divine+", "archon-legend", "herald"],
    )
    position: Annotated[Position | None, BeforeValidator(Position.parse)] = Field(
        default=None,
        description=(
            "Role by farm priority: 1 carry, 2 mid, 3 offlane, 4 soft support, "
            "5 hard support. Omit for all positions."
        ),
    )
    game_mode: GameMode | None = Field(
        default=None,
        description=(
            "Game mode. Use ranked_all_pick for competitive matchmaking questions. Omit to "
            "use the tool's default mode, which the result's filters field reports."
        ),
    )
    min_sample: int = Field(
        default=DEFAULT_MIN_SAMPLE,
        ge=1,
        description=(
            "Fewest matches a result must be based on. Below this the tool returns "
            'status "insufficient_data" instead of a number. Lowering it gives noisier '
            "answers; prefer widening patch, rank_bracket or position first."
        ),
    )

    @property
    def rank_range(self) -> tuple[RankBracket, RankBracket] | None:
        """The rank filter as an inclusive (low, high) pair, or None for all brackets."""
        return parse_rank_range(self.rank_bracket) if self.rank_bracket else None

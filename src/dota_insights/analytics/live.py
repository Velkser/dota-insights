"""Live win probability from net-worth lead, game time and draft.

The model is a logistic regression whose three coefficients (intercept, net-worth lead, draft)
vary with game time: each is a piecewise-linear function of the minute, defined by its value
at a few knots (0, 5, 10, ... minutes) and interpolated in between. So the logit is

    a(t) + b(t) * radiant_lead / 1000 + c(t) * draft

where ``draft`` is the sum of the Radiant heroes' ratings minus the sum of the Dire heroes'
ratings, and a hero's rating is the logit of its smoothed win rate in the training matches.
The coefficients, hero ratings and calibration report are fitted by
``scripts/train_live_win_model.py`` and shipped in ``data/live_win_model.json``.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

LEAD_SCALE = 1000.0
MODEL_FILE = "live_win_model.json"


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def hat_weights(minute: float, knots: Sequence[float]) -> list[tuple[int, float]]:
    """(knot index, weight) pairs for linear interpolation at ``minute``.

    Minutes before the first or after the last knot use that knot's value.
    """
    if minute <= knots[0]:
        return [(0, 1.0)]
    if minute >= knots[-1]:
        return [(len(knots) - 1, 1.0)]
    for i in range(len(knots) - 1):
        lo, hi = knots[i], knots[i + 1]
        if lo <= minute <= hi:
            w = (minute - lo) / (hi - lo)
            return [(i, 1.0 - w), (i + 1, w)]
    raise AssertionError("unreachable")


def draft_score(radiant: Sequence[int], dire: Sequence[int], ratings: dict[int, float]) -> float:
    """Sum of Radiant hero ratings minus sum of Dire hero ratings (unknown heroes count 0)."""
    return sum(ratings.get(h, 0.0) for h in radiant) - sum(ratings.get(h, 0.0) for h in dire)


@dataclass(frozen=True, slots=True)
class Prediction:
    radiant_win_probability: float
    intercept_term: float
    lead_term: float
    draft_term: float
    draft_score: float | None
    lead_coefficient: float = 0.0


@dataclass(frozen=True, slots=True)
class WinModel:
    knots: tuple[float, ...]
    intercept: tuple[float, ...]
    lead: tuple[float, ...]
    draft: tuple[float, ...]
    hero_ratings: dict[int, float]
    meta: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WinModel":
        coef = raw["coefficients"]
        return cls(
            knots=tuple(float(k) for k in raw["knots_min"]),
            intercept=tuple(coef["intercept"]),
            lead=tuple(coef["lead_per_1k"]),
            draft=tuple(coef["draft"]),
            hero_ratings={int(k): float(v) for k, v in raw.get("hero_ratings", {}).items()},
            meta={k: v for k, v in raw.items() if k not in ("coefficients", "hero_ratings")},
        )

    def coefficients_at(self, minute: float) -> tuple[float, float, float]:
        a = b = c = 0.0
        for i, w in hat_weights(minute, self.knots):
            a += w * self.intercept[i]
            b += w * self.lead[i]
            c += w * self.draft[i]
        return a, b, c

    def predict(
        self,
        minute: float,
        radiant_lead: float,
        radiant_heroes: Sequence[int] | None = None,
        dire_heroes: Sequence[int] | None = None,
    ) -> Prediction:
        a, b, c = self.coefficients_at(max(minute, 0.0))
        draft = None
        if radiant_heroes and dire_heroes:
            draft = draft_score(radiant_heroes, dire_heroes, self.hero_ratings)
        lead_term = b * radiant_lead / LEAD_SCALE
        draft_term = c * draft if draft is not None else 0.0
        return Prediction(
            radiant_win_probability=sigmoid(a + lead_term + draft_term),
            intercept_term=a,
            lead_term=lead_term,
            draft_term=draft_term,
            draft_score=draft,
            lead_coefficient=b,
        )

    def lead_for_even(self, minute: float, draft_term: float = 0.0) -> float | None:
        """Radiant net-worth lead at which the game is a coin flip, given the draft term."""
        a, b, _ = self.coefficients_at(max(minute, 0.0))
        if abs(b) < 1e-9:
            return None
        return -(a + draft_term) / b * LEAD_SCALE

    def reliability_bin(self, probability: float) -> dict[str, Any] | None:
        """The held-out calibration bin whose predicted range contains ``probability``."""
        for row in self.meta.get("calibration", {}).get("reliability", []):
            if row["from"] <= probability < row["to"] or (row["to"] >= 1.0 and probability >= 1):
                return row
        return None


@cache
def load_model() -> WinModel:
    path = resources.files(__package__).joinpath("data", MODEL_FILE)
    return WinModel.from_dict(json.loads(path.read_text(encoding="utf-8")))


# Live games (OpenDota /live)


@dataclass(frozen=True, slots=True)
class LiveState:
    match_id: int
    game_time_s: int
    radiant_lead: int | None
    radiant_heroes: list[int]
    dire_heroes: list[int]
    radiant_team: str | None
    dire_team: str | None
    radiant_score: int | None
    dire_score: int | None
    league_id: int | None
    delay_s: int | None
    last_update_time: int | None


def live_state(game: dict[str, Any]) -> LiveState:
    """Model inputs from one OpenDota ``/live`` row (``team`` 0 is Radiant, 1 Dire)."""
    players = game.get("players") or []
    return LiveState(
        match_id=int(game["match_id"]),
        game_time_s=int(game.get("game_time") or 0),
        radiant_lead=game.get("radiant_lead"),
        radiant_heroes=[p["hero_id"] for p in players if p.get("team") == 0 and p.get("hero_id")],
        dire_heroes=[p["hero_id"] for p in players if p.get("team") == 1 and p.get("hero_id")],
        radiant_team=game.get("team_name_radiant") or None,
        dire_team=game.get("team_name_dire") or None,
        radiant_score=game.get("radiant_score"),
        dire_score=game.get("dire_score"),
        league_id=game.get("league_id") or None,
        delay_s=game.get("delay"),
        last_update_time=game.get("last_update_time"),
    )


def find_live(games: Sequence[dict[str, Any]], match_id: int) -> dict[str, Any] | None:
    for g in games:
        if str(g.get("match_id")) == str(match_id):
            return g
    return None

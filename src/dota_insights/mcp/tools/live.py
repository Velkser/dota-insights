"""Live tools: win probability of a live or described match."""

from datetime import UTC, date, datetime
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from dota_insights.analytics import live, pro
from dota_insights.cache import TTL_LIVE
from dota_insights.mcp import args
from dota_insights.mcp.envelope import Period, ToolResult, error, ok
from dota_insights.reference import resolve_hero
from dota_insights.sources.opendota import OpenDotaError

HeroList = Annotated[
    list[str] | None,
    Field(
        description=f"Up to 5 heroes of one side; overrides the live draft. {args.HEROES}",
        max_length=5,
    ),
]


def _resolve(names: list[str]) -> tuple[list[int], ToolResult | None]:
    ids = []
    for name in names:
        resolution = resolve_hero(name)
        if resolution.best is None:
            if not resolution.found:
                return [], error(f"No hero matches {name!r}.", hint="Check the spelling.")
            options = ", ".join(c.record.display_name for c in resolution.candidates[:5])
            return [], error(f"{name!r} could match several heroes: {options}.", hint="Name one.")
        ids.append(resolution.best.id)
    return ids, None


def _clock(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def predict_live_win(
        match_id: Annotated[
            int | None,
            Field(
                description=(
                    "A match from get_live_matches. Its net-worth lead, game time and heroes are "
                    "read from the live feed; explicit arguments below override them."
                )
            ),
        ] = None,
        radiant_networth_lead: Annotated[
            int | None,
            Field(description="Radiant net worth minus Dire net worth, in gold (negative: Dire)."),
        ] = None,
        game_time_s: Annotated[
            int | None, Field(ge=-120, le=7200, description="In-game clock in seconds.")
        ] = None,
        radiant_heroes: HeroList = None,
        dire_heroes: HeroList = None,
    ) -> ToolResult[dict]:
        """Win probability for each side of a live game (or a described game state) from the
        net-worth lead, the game time and the draft, with the model's calibration.

        Pass match_id for a game listed by get_live_matches, or radiant_networth_lead and
        game_time_s (plus optional heroes) to ask "what if"."""  # fmt: skip
        model = live.load_model()
        state = None
        freshness: datetime | None = None
        caveats: list[str] = []
        source: list = []
        if match_id is not None:
            try:
                games = await pro.fetch_live()
            except OpenDotaError as exc:
                return error(f"OpenDota /live failed: {exc}", hint="Retry in a minute.")
            game = live.find_live(games, match_id)
            if game is None or game.get("deactivate_time"):
                return error(
                    f"Match {match_id} is not among OpenDota's live games.",
                    hint=(
                        "Pick a match_id from get_live_matches, or pass radiant_networth_lead "
                        "and game_time_s yourself."
                    ),
                    source=["opendota"],
                )
            state = live.live_state(game)
            source.append("opendota")
            if state.last_update_time:
                freshness = pro.utc(state.last_update_time)
            caveats.append(
                f"Live feed lags the game by {state.delay_s or 'an unknown number of'} s and is "
                f"cached here for {TTL_LIVE} s; the real game may have moved on."
            )

        lead = radiant_networth_lead
        if lead is None and state is not None:
            lead = state.radiant_lead
        seconds = game_time_s if game_time_s is not None else (state.game_time_s if state else None)
        if lead is None or seconds is None:
            return error(
                "Need a net-worth lead and a game time.",
                hint=(
                    "Pass match_id of a live game, or both radiant_networth_lead and game_time_s."
                ),
                source=source,
            )
        rad, bad = _resolve(radiant_heroes) if radiant_heroes else ([], None)
        if bad is not None:
            return bad
        dire, bad = _resolve(dire_heroes) if dire_heroes else ([], None)
        if bad is not None:
            return bad
        if not rad and state is not None:
            rad = state.radiant_heroes
        if not dire and state is not None:
            dire = state.dire_heroes
        if set(rad) & set(dire):
            return error("A hero cannot be on both sides.")

        minute = max(seconds, 0) / 60
        prediction = model.predict(minute, lead, rad or None, dire or None)
        p = prediction.radiant_win_probability
        meta = model.meta
        training, calibration = meta["training"], meta["calibration"]
        if seconds < 0:
            caveats.append("The horn has not sounded yet; the game time is treated as 0:00.")
        if minute > training["max_minute"]:
            caveats.append(
                f"Past minute {training['max_minute']} the model uses its last fitted values; "
                "few training matches last that long."
            )
        if not (rad and dire):
            caveats.append("No complete draft on one side, so the draft term is left out.")
        elif len(rad) < 5 or len(dire) < 5:
            caveats.append("Draft is incomplete; missing heroes count as average.")
        caveats += [
            "Trained on parsed league matches (pro and amateur leagues) from OpenDota; pub games "
            "and very high-level pro games can swing differently.",
            "Uses net worth, time and heroes only: no buildings, Aegis, buybacks, item timings "
            "or hero levels. Treat it as a baseline, not a forecast of the next fight.",
            "Draft term uses hero win rates from the training period, which spans several "
            "patches; on held-out games it barely improves on net worth alone.",
            "No confidence interval: this is a model estimate, not an observed rate. "
            "calibration_bin shows how often held-out games given a similar probability "
            "were actually won.",
        ]

        calibration_bin = model.reliability_bin(p)
        even = model.lead_for_even(minute, prediction.draft_term)
        result = {
            "radiant_win_probability": round(p, 4),
            "dire_win_probability": round(1 - p, 4),
            "favourite": "radiant" if p > 0.5 else "dire" if p < 0.5 else "even",
            "inputs": {
                "match_id": match_id,
                "radiant_team": state.radiant_team if state else None,
                "dire_team": state.dire_team if state else None,
                "game_time": _clock(seconds),
                "game_time_s": seconds,
                "radiant_networth_lead": lead,
                "radiant_heroes": [pro.hero_name(h) for h in rad],
                "dire_heroes": [pro.hero_name(h) for h in dire],
                "radiant_score": state.radiant_score if state else None,
                "dire_score": state.dire_score if state else None,
            },
            "logit_terms": {
                "intercept": round(prediction.intercept_term, 4),
                "networth_lead": round(prediction.lead_term, 4),
                "draft": round(prediction.draft_term, 4),
            },
            "draft_score": round(prediction.draft_score, 4)
            if prediction.draft_score is not None
            else None,
            "even_game_lead": round(even) if even is not None else None,
            "probability_change_per_1k_lead": round(
                live.sigmoid(
                    prediction.intercept_term
                    + prediction.lead_term
                    + prediction.draft_term
                    + prediction.lead_coefficient
                )
                - p,
                4,
            ),
            "model": {
                "type": "logistic regression, coefficients piecewise linear in game time",
                "training_matches": training["matches"],
                "held_out_matches": calibration["test_matches"],
                "brier": calibration["brier"],
                "brier_networth_only": calibration["brier_lead_only"],
                "brier_constant": calibration["brier_constant"],
                "calibration_bin": calibration_bin,
            },
        }
        return ok(
            result,
            sample_size=training["matches"],
            period=Period(
                from_date=date.fromisoformat(training["from_date"]),
                to_date=date.fromisoformat(training["to_date"]),
            ),
            definitions={
                "radiant_win_probability": (
                    "sigmoid(a(t) + b(t) * lead / 1000 + c(t) * draft_score); a, b, c are "
                    "fitted per knot minute (0, 5, ..., 40, 50, 60) and interpolated linearly."
                ),
                "draft_score": (
                    "Sum over Radiant heroes minus sum over Dire heroes of logit(hero win rate), "
                    f"win rates shrunk toward 50% with {training['hero_prior_games']} "
                    "pseudo-games."
                ),
                "logit_terms": "Each term's contribution to the log-odds of a Radiant win.",
                "even_game_lead": "Radiant net-worth lead at which the model says 50%.",
                "probability_change_per_1k_lead": (
                    "How much the Radiant probability rises with 1,000 more Radiant net worth."
                ),
                "brier": (
                    "Mean squared error of the probability on held-out match-minutes "
                    "(0 perfect, 0.25 a coin flip). brier_constant always predicts the "
                    "training Radiant win rate."
                ),
                "calibration_bin": (
                    "Held-out match-minutes whose prediction fell in the same 10% band: mean "
                    "prediction vs share actually won."
                ),
                "sample_size": "League matches the model was trained on.",
            },
            caveats=caveats,
            source=source or ["opendota"],
            data_freshness=freshness or datetime.fromisoformat(meta["trained_at"]).astimezone(UTC),
        )

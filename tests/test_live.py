from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from dota_insights import sources
from dota_insights.analytics import live
from dota_insights.cache import clear_all
from dota_insights.mcp.server import mcp
from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

# OpenDota /live trimmed to the fields the model reads: two league games, one pub game and one
# that has ended (deactivate_time set).
FIXTURES = Path(__file__).parent / "fixtures" / "live"
LEAGUE_GAME = 9076183783  # CyberHero vs ..., 41:03, Dire ahead by 36,926
ENDED_GAME = 9076108067


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def requests():
    clear_all()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/live":
            return httpx.Response(200, content=(FIXTURES / "opendota_live.json").read_bytes())
        return httpx.Response(404, json={"error": "no fixture"})

    transport = httpx.MockTransport(handler)
    sources.use_clients(
        opendota=OpenDotaClient(
            transport=transport, sleep=_no_sleep, max_retries=0, requests_per_minute=1e6
        ),
        stratz=StratzClient(
            token="t", transport=transport, sleep=_no_sleep, requests_per_minute=1e6
        ),
    )
    yield seen
    clear_all()


async def call(args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool("predict_live_win", args, raise_on_error=False)
    return result.structured_content


def test_hat_weights_interpolate_and_clamp():
    knots = (0, 5, 10)
    assert live.hat_weights(-1, knots) == [(0, 1.0)]
    assert live.hat_weights(7.5, knots) == [(1, 0.5), (2, 0.5)]
    assert live.hat_weights(5, knots) == [(0, 0.0), (1, 1.0)]
    assert live.hat_weights(99, knots) == [(2, 1.0)]


def test_shipped_model_is_complete_and_calibrated():
    model = live.load_model()
    k = len(model.knots)
    assert len(model.intercept) == len(model.lead) == len(model.draft) == k
    assert len(model.hero_ratings) > 100
    cal = model.meta["calibration"]
    assert cal["brier"] < cal["brier_constant"]
    assert cal["test_matches"] > 1000
    for row in cal["reliability"]:
        assert row["samples"] > 1000
        assert abs(row["predicted_mean"] - row["observed_rate"]) < 0.05
    # A lead matters at every point of the game, and the same lead matters less later on.
    assert all(b > 0 for b in model.lead)
    assert model.lead[2] > model.lead[6] > model.lead[-1]


def test_prediction_is_monotone_in_lead_and_mirrors():
    model = live.load_model()
    probs = [model.predict(25, lead).radiant_win_probability for lead in range(-10000, 10001, 2500)]
    assert probs == sorted(probs)
    ahead = model.predict(25, 5000, [1, 2], [3, 4]).radiant_win_probability
    swapped = model.predict(25, -5000, [3, 4], [1, 2]).radiant_win_probability
    # Mirrored except for the Radiant-side intercept.
    a = model.coefficients_at(25)[0]
    assert live.logit(ahead) + live.logit(swapped) == pytest.approx(2 * a)
    even = model.lead_for_even(25)
    assert model.predict(25, even).radiant_win_probability == pytest.approx(0.5)


def test_live_state_reads_sides_from_team():
    game = {
        "match_id": "7",
        "game_time": 600,
        "radiant_lead": 1200,
        "players": [
            {"team": 0, "hero_id": 1},
            {"team": 1, "hero_id": 2},
            {"team": 0, "hero_id": 0},
        ],
    }
    state = live.live_state(game)
    assert (state.radiant_heroes, state.dire_heroes, state.radiant_lead) == ([1], [2], 1200)


async def test_explicit_inputs_need_no_source(requests):
    body = await call(
        {
            "radiant_networth_lead": 5000,
            "game_time_s": 1500,
            "radiant_heroes": ["axe", "lich", "viper", "pa", "cm"],
            "dire_heroes": ["lina", "invoker", "tide", "sven", "lion"],
        }
    )
    assert requests == []
    assert body["status"] == "ok"
    res = body["result"]
    assert 0.6 < res["radiant_win_probability"] < 0.95
    assert res["radiant_win_probability"] + res["dire_win_probability"] == pytest.approx(1)
    assert res["favourite"] == "radiant"
    assert res["inputs"]["game_time"] == "25:00"
    assert res["inputs"]["radiant_heroes"][3] == "Phantom Assassin"
    assert res["draft_score"] is not None
    terms = res["logit_terms"]
    assert live.sigmoid(sum(terms.values())) == pytest.approx(res["radiant_win_probability"], 1e-3)
    assert res["model"]["calibration_bin"]["from"] <= res["radiant_win_probability"]
    assert body["sample_size"] == res["model"]["training_matches"]
    assert body["period"]["from_date"] <= body["period"]["to_date"]
    assert {"radiant_win_probability", "draft_score", "brier"} <= set(body["definitions"])
    assert body["data_freshness"]


async def test_live_match_uses_feed_values(requests):
    body = await call({"match_id": LEAGUE_GAME})
    assert requests == ["/api/live"]
    assert body["status"] == "ok"
    res = body["result"]
    assert res["inputs"]["radiant_networth_lead"] == -36926
    assert res["inputs"]["game_time"] == "41:03"
    assert res["inputs"]["radiant_team"] == "CyberHero"
    assert len(res["inputs"]["radiant_heroes"]) == len(res["inputs"]["dire_heroes"]) == 5
    assert res["dire_win_probability"] > 0.95
    assert any("lags" in c for c in body["caveats"])
    assert body["data_freshness"].startswith("2026-")


async def test_explicit_values_override_live_feed(requests):
    body = await call({"match_id": LEAGUE_GAME, "radiant_networth_lead": 0, "game_time_s": 60})
    res = body["result"]
    assert res["inputs"]["radiant_networth_lead"] == 0
    assert 0.4 < res["radiant_win_probability"] < 0.6


async def test_ended_or_unknown_match_is_an_error(requests):
    for match_id in (ENDED_GAME, 1):
        body = await call({"match_id": match_id})
        assert body["status"] == "error"
        assert "get_live_matches" in body["hint"]


async def test_missing_inputs_and_bad_heroes(requests):
    body = await call({"radiant_networth_lead": 1000})
    assert body["status"] == "error"
    assert "game time" in body["message"]
    body = await call({"radiant_networth_lead": 0, "game_time_s": 0, "radiant_heroes": ["zzzz"]})
    assert body["status"] == "error"
    body = await call(
        {
            "radiant_networth_lead": 0,
            "game_time_s": 0,
            "radiant_heroes": ["axe"],
            "dire_heroes": ["axe"],
        }
    )
    assert "both sides" in body["message"]


async def test_no_draft_and_pre_horn_are_flagged(requests):
    body = await call({"radiant_networth_lead": 0, "game_time_s": -30})
    assert body["status"] == "ok"
    assert body["result"]["draft_score"] is None
    assert any("horn" in c for c in body["caveats"])
    assert any("draft term is left out" in c for c in body["caveats"])

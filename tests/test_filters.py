import json

import pytest
from pydantic import ValidationError

from dota_insights.models import (
    DEFAULT_MIN_SAMPLE,
    Filters,
    GameMode,
    Outcome,
    Position,
    RankBracket,
    parse_rank_range,
)


def test_position_values():
    assert [p.value for p in Position] == [1, 2, 3, 4, 5]
    assert Position.SOFT_SUPPORT == 4


def test_rank_bracket_order():
    assert RankBracket.HERALD.tier == 1
    assert RankBracket.IMMORTAL.tier == 8
    assert len(RankBracket) == 8


def test_simple_enums():
    assert GameMode("ranked_all_pick") is GameMode.RANKED_ALL_PICK
    assert {o.value for o in Outcome} == {"win", "loss", "all"}


def test_defaults():
    f = Filters()
    assert f.patch is None and f.rank_bracket is None and f.position is None
    assert f.min_sample == DEFAULT_MIN_SAMPLE
    assert f.rank_range is None


@pytest.mark.parametrize(
    ("raw", "stored", "expected"),
    [
        ("divine", "divine", (RankBracket.DIVINE, RankBracket.DIVINE)),
        ("Divine+", "divine+", (RankBracket.DIVINE, RankBracket.IMMORTAL)),
        ("archon - legend", "archon-legend", (RankBracket.ARCHON, RankBracket.LEGEND)),
        ("legend-legend", "legend", (RankBracket.LEGEND, RankBracket.LEGEND)),
    ],
)
def test_rank_ranges(raw, stored, expected):
    f = Filters(rank_bracket=raw)
    assert f.rank_bracket == stored
    assert f.rank_range == expected
    assert parse_rank_range(stored) == expected


@pytest.mark.parametrize("raw", ["mythic", "legend-archon", "divine++", ""])
def test_bad_rank_ranges(raw):
    with pytest.raises(ValidationError):
        Filters(rank_bracket=raw)


@pytest.mark.parametrize("raw", [1, "1", "pos1", "pos 1", "position_1", "carry", "CARRY"])
def test_position_parsing(raw):
    assert Filters(position=raw).position is Position.CARRY


@pytest.mark.parametrize("raw", [0, 6, "pos6", "jungler"])
def test_bad_positions(raw):
    with pytest.raises(ValidationError):
        Filters(position=raw)


def test_min_sample_must_be_positive():
    with pytest.raises(ValidationError):
        Filters(min_sample=0)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        Filters(hero="pudge")


def test_round_trip():
    f = Filters(
        patch="7.37d",
        rank_bracket="divine+",
        position=Position.MID,
        game_mode=GameMode.TURBO,
        min_sample=50,
    )
    dumped = f.model_dump(mode="json")
    assert dumped == {
        "patch": "7.37d",
        "rank_bracket": "divine+",
        "position": 2,
        "game_mode": "turbo",
        "min_sample": 50,
    }
    assert Filters.model_validate_json(json.dumps(dumped)) == f


def test_json_schema():
    schema = Filters.model_json_schema()
    json.dumps(schema)
    props = schema["properties"]
    assert set(props) == {"patch", "rank_bracket", "position", "game_mode", "min_sample"}
    assert all(p.get("description") for p in props.values())
    assert schema["$defs"]["Position"]["enum"] == [1, 2, 3, 4, 5]
    assert "ranked_all_pick" in schema["$defs"]["GameMode"]["enum"]

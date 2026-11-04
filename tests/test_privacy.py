"""Player privacy (p4-10): private profiles, the opt-out list, and masking in match tools."""

import copy
from datetime import UTC, datetime

import httpx
import pytest
from fakes import FakeKV
from fastmcp import Client
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from test_matches import PARSED, PARSED_ID
from test_players import GORGC, PRIVATE, YATORO, api, call  # noqa: F401  (fixture)

from dota_insights import privacy, sources
from dota_insights.cache import clear_all
from dota_insights.db.access import use_database
from dota_insights.db.models import Base, PlayerOptOut
from dota_insights.kv import reset_kv, use_kv
from dota_insights.mcp.server import mcp
from dota_insights.sources.opendota import OpenDotaClient

HIDDEN = 987654321


@pytest.fixture
def opt_outs(tmp_path):
    """A database whose opt-out list holds HIDDEN and Yatoro."""
    engine = create_engine(f"sqlite:///{tmp_path / 'privacy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        for account in (HIDDEN, YATORO):
            session.add(PlayerOptOut(account_id=account, created_at=datetime.now(UTC)))
    privacy.forget_cache()
    with use_database(engine):
        yield engine
    privacy.forget_cache()
    engine.dispose()


async def test_opted_out_player_is_refused_without_any_source_call(api, opt_outs):  # noqa: F811
    out = await call("get_player_profile", player=str(YATORO))
    assert out["status"] == "error" and "asked not to be shown" in out["message"]
    assert out["hint"]
    out = await call("compare_players", player_a=str(GORGC), player_b=str(YATORO))
    assert out["status"] == "error"
    assert api["opendota"] == []
    # Others still work.
    assert (await call("get_player_profile", player=str(GORGC)))["status"] == "ok"


async def test_private_profile_is_a_clear_error(api, opt_outs):  # noqa: F811
    out = await call("get_player_profile", player=str(PRIVATE))
    assert out["status"] == "error" and "private" in out["message"]


async def test_peers_mask_opted_out_players(api, tmp_path):  # noqa: F811
    out = await call("get_player_peers", player=str(GORGC), min_games=5, top=5)
    first = out["result"]["teammates"][0]
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(PlayerOptOut(account_id=first["account_id"], created_at=datetime.now(UTC)))
    privacy.forget_cache()
    clear_all()
    try:
        with use_database(engine):
            out = await call("get_player_peers", player=str(GORGC), min_games=5, top=5)
    finally:
        privacy.forget_cache()
    masked = out["result"]["teammates"][0]
    assert masked["account_id"] is None and masked["persona_name"] is None
    assert masked["matches"] == first["matches"]


@pytest.fixture
def match_api():
    raw = copy.deepcopy(PARSED)
    raw["players"][0].update(account_id=HIDDEN, personaname="Hidden One")
    raw["players"][1].update(account_id=222, personaname="Visible")
    raw["players"][2].update(account_id=privacy.ANONYMOUS_ACCOUNT)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/matches/{PARSED_ID}":
            return httpx.Response(200, json=raw)
        return httpx.Response(404, json={"error": "Not Found"})

    clear_all()
    sources.use_clients(
        opendota=OpenDotaClient(api_key=None, transport=httpx.MockTransport(handle), max_retries=0)
    )
    yield raw
    sources.use_clients()
    clear_all()


async def test_match_tools_mask_opted_out_and_anonymous_players(match_api, opt_outs):
    async with Client(mcp) as client:
        result = await client.call_tool("analyze_match", {"match_id": PARSED_ID})
    players = result.structured_content["result"]["players"]
    assert [p["account_id"] for p in players[:3]] == [None, 222, None]
    assert str(HIDDEN) not in str(result.structured_content)
    assert "Hidden One" not in str(result.structured_content)
    # The cached raw match is not modified.
    assert match_api["players"][0]["account_id"] == HIDDEN


def test_mask_players_copies():
    players = [{"account_id": 5, "personaname": "x"}, {"account_id": None}, {"account_id": 6}]
    masked = privacy.mask_players(players, frozenset({5}))
    assert masked[0] == {"account_id": None, "personaname": None}
    assert masked[1:] == players[1:] and players[0]["account_id"] == 5


async def test_opt_out_list_is_shared_through_redis_and_survives_db_failure(opt_outs):
    kv = FakeKV()
    use_kv(kv)
    try:
        assert await privacy.opted_out() == {HIDDEN, YATORO}
        assert privacy.SHARED_KEY in kv.data
        privacy.forget_cache()
        with use_database(None):  # another instance, no DB: Redis has the list
            assert await privacy.is_opted_out(HIDDEN)
        # Redis gone, database broken: the last list seen is kept.
        reset_kv()
        use_kv(None)
        broken = create_engine("sqlite:////nonexistent/dir/x.db")
        privacy._cache = (0.0, frozenset({HIDDEN}))
        with use_database(broken):
            assert await privacy.opted_out() == {HIDDEN}
    finally:
        reset_kv()


async def test_without_a_database_nobody_is_opted_out():
    privacy.forget_cache()
    assert await privacy.opted_out() == frozenset()


def test_privacy_cli(tmp_path, monkeypatch, capsys):
    from dota_insights.db import session as db_session
    from dota_insights.usage import cli

    engine = create_engine(f"sqlite:///{tmp_path / 'cli.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "get_sessionmaker", lambda: factory)
    kv = FakeKV()
    use_kv(kv)
    try:
        assert cli.main(["privacy", "opt-out", "76561197960265735", "--note", "email"]) == 0
        assert cli.main(["privacy", "opt-out", "https://www.opendota.com/players/42"]) == 0
        assert cli.main(["privacy", "opt-out", "42"]) == 0  # already there
        assert kv.data[privacy.SHARED_KEY] == "[7, 42]"
        assert cli.main(["privacy", "opt-in", "7"]) == 0
        assert cli.main(["privacy", "list"]) == 0
        assert cli.main(["privacy", "opt-out", "not-a-player"]) == 2
    finally:
        reset_kv()
    with factory() as session:
        assert list(session.scalars(select(PlayerOptOut.account_id))) == [42]
    assert kv.data[privacy.SHARED_KEY] == "[42]"
    assert "42" in capsys.readouterr().out

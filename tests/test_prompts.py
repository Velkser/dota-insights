import re

import pytest
from fastmcp import Client

from dota_insights.mcp.server import mcp

ARGS = {
    "analyze_last_match": {"player": "56939869"},
    "pick_hero_for_style": {
        "style": "aggressive early ganks",
        "position": "4",
        "rank_bracket": "archon",
        "player": "56939869",
    },
    "patch_meta_overview": {"patch": "7.39c", "rank_bracket": "immortal"},
    "check_dota_myth": {"claim": "Radiant wins more"},
}


async def test_prompts_are_registered():
    names = {p.name for p in await mcp.list_prompts()}
    assert names == set(ARGS)


@pytest.mark.parametrize("name", sorted(ARGS))
async def test_prompt_renders_and_names_only_real_tools(name):
    tools = {t.name for t in await mcp.list_tools()}
    async with Client(mcp) as client:
        result = await client.get_prompt(name, ARGS[name])
    text = result.messages[0].content.text
    for value in ARGS[name].values():
        assert value in text
    mentioned = {m for m in re.findall(r"`([a-z_]+)`", text) if "_" in m}
    named_tools = mentioned - {"last_20", "match_id", "insufficient_data"}
    assert named_tools, "a prompt should steer the client to tools"
    assert named_tools <= tools, named_tools - tools
    assert "hint" in text


async def test_prompt_optional_arguments_can_be_omitted():
    async with Client(mcp) as client:
        result = await client.get_prompt("pick_hero_for_style", {"style": "farming carry"})
    text = result.messages[0].content.text
    assert "Filters:" not in text
    assert "get_player_hero_pool" not in text
    assert "\n4. Recommend" in text

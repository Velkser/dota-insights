"""The tool catalog: registered tools grouped by domain, with a one-line purpose and an example.

``scripts/gen_tool_docs.py`` renders it into the README and the landing page (``site.py``)
renders it as HTML, so neither can drift from the tools the server registers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastmcp import FastMCP

from dota_insights.mcp.tools import discover_domains

# One question a person might ask that the tool answers. Tools missing here get "—".
EXAMPLES: dict[str, str] = {
    "get_hero_builds": "What should I build on Juggernaut this patch?",
    "get_ability_stats": "Which talents do Invoker players take at 10 and 15?",
    "get_neutral_items": "Which tier 3 neutral item is best on Sniper?",
    "get_hero_benchmarks": "Is 600 GPM good for an Anti-Mage?",
    "get_hero_power_curve": "Does Spectre win more in long games?",
    "get_farming_stats": "How much gold does a Medusa have at 20 minutes?",
    "get_gold_xp_curves": "Who out-farms whom between Alchemist and Naga Siren?",
    "get_hero_lane_stats": "How often does Viper win its lane?",
    "get_general_stats": "Does Radiant win more than Dire?",
    "get_records": "What is the highest GPM ever recorded on Alchemist?",
    "get_rank_distribution": "What share of players are Divine or higher?",
    "get_hero_overview": "How strong is Phantom Assassin right now in Ancient?",
    "compare_heroes": "Pudge or Lion as a 4 in Legend?",
    "get_tier_list": "Which carries are the strongest this patch?",
    "find_hidden_gems": "Which rarely picked heroes win a lot right now?",
    "get_item_overview": "Who buys Radiance and when?",
    "analyze_item": "Is an early BKB worth it on Faceless Void?",
    "compare_items": "Battle Fury or Maelstrom first on Troll Warlord?",
    "get_item_payback": "How long does Hand of Midas take to pay for itself?",
    "analyze_match": "What happened in match 8012345678?",
    "get_match_timeline": "When did the gold lead flip in my last game?",
    "find_turning_points": "Which fight decided match 8012345678?",
    "analyze_teamfights": "Which teamfights did we lose and why?",
    "analyze_vision": "Who warded the most and how many wards were dewarded?",
    "analyze_laning_phase": "Who won each lane in match 8012345678?",
    "get_hero_matchups": "What counters Huskar?",
    "find_hero_combos": "Which support pairs best with Chaos Knight?",
    "suggest_pick": "We have Axe and Lion against Sniper and Drow; what should I pick mid?",
    "analyze_draft": "Which side has the better draft?",
    "server_info": "Which data sources is this server using?",
    "get_player_profile": "What is the medal and recent form of account 56939869?",
    "get_player_stats": "What are my average KDA and GPM over the last 100 games?",
    "get_player_hero_pool": "Which of my heroes win more than average?",
    "find_player_weaknesses": "Where am I below my rank's average?",
    "get_player_trends": "Am I improving over my last 200 matches?",
    "get_player_peers": "Who do I win the most with?",
    "compare_players": "How do I compare with my friend on the heroes we both play?",
    "get_pro_matches": "What were Team Spirit's latest results?",
    "get_team_stats": "Which heroes does Gaimin Gladiators draft most?",
    "get_head_to_head": "What is the record between OG and Tundra?",
    "get_tournament_stats": "Which heroes were contested most at The International?",
    "get_pro_player": "What does Yatoro play and how does he do?",
    "get_pro_builds": "How do pros build Morphling compared with pubs?",
    "get_live_matches": "Which pro games are live right now?",
    "lookup_reference": "What does Faceless Void's Chronosphere do?",
    "find_meta_trends": "Which heroes rose the most after the last patch?",
    "get_hero_patch_history": "How has Pudge's win rate changed over the patches?",
    "get_patch_summary": "What changed for heroes in 7.39?",
    "analyze_death": "How much does dying at 25 minutes cost, and when is buyback worth it?",
    "get_farm_distribution": "How much of the team's net worth should the carry have?",
    "get_comeback_stats": "How often do teams win from 10k behind at 25 minutes?",
    "get_objective_impact": "How much does killing the first Roshan raise the win rate?",
    "test_hypothesis": "Do Phantom Assassins with BKB before 20 minutes win more?",
    "analyze_correlation": "Does stacking more camps actually help supports win?",
    "find_matches": "Show me matches won after being 20k gold behind.",
    "explore_query": "Average GPM by lane role in league matches this patch?",
    "render_chart": "Draw Anti-Mage's gold curve as a chart.",
    "predict_live_win": "Who is likely to win a game with a 5k lead at 30 minutes?",
}


@dataclass(frozen=True)
class ToolEntry:
    name: str
    purpose: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    example: str | None


@dataclass(frozen=True)
class Domain:
    name: str
    tools: tuple[ToolEntry, ...]


def first_sentence(text: str) -> str:
    paragraph = " ".join(text.strip().split("\n\n")[0].split())
    found = re.match(r".+?[.!?](?=\s|$)", paragraph)
    return found.group(0) if found else paragraph


async def domain_tools() -> list[tuple[str, list]]:
    """Each domain module's name and its tools as FastMCP registers them, sorted by name."""
    domains = []
    for module in discover_domains():
        server = FastMCP(module.__name__)
        module.register(server)
        tools = sorted(await server.list_tools(), key=lambda t: t.name)
        if tools:
            domains.append((module.__name__.rsplit(".", 1)[-1], tools))
    return domains


async def catalog() -> tuple[Domain, ...]:
    """The tool catalog, grouped by domain."""
    domains = []
    for name, tools in await domain_tools():
        entries = []
        for tool in tools:
            props = list(tool.parameters.get("properties", {}))
            required = tool.parameters.get("required", [])
            entries.append(
                ToolEntry(
                    name=tool.name,
                    purpose=first_sentence(tool.description or ""),
                    required=tuple(required),
                    optional=tuple(p for p in props if p not in required),
                    example=EXAMPLES.get(tool.name),
                )
            )
        domains.append(Domain(name=name, tools=tuple(entries)))
    return tuple(domains)


_cached: tuple[Domain, ...] | None = None


async def cached_catalog() -> tuple[Domain, ...]:
    """``catalog()``, built once per process (the tools do not change while it runs)."""
    global _cached
    if _cached is None:
        _cached = await catalog()
    return _cached

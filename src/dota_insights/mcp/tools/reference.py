"""Reference lookups (heroes, items, abilities) and the static MCP resources built on them."""

import difflib
from enum import StrEnum
from functools import cache
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from pydantic import BaseModel, Field

from dota_insights.mcp.envelope import ToolResult, error, ok
from dota_insights.reference import (
    Ability,
    Hero,
    Item,
    abilities,
    ability_by_name,
    hero_by_id,
    heroes,
    item_by_id,
    items,
    patches,
    resolve_hero,
    resolve_item,
)
from dota_insights.reference.aliases import normalize
from dota_insights.reference.resolver import AMBIGUITY_MARGIN, MAX_CANDIDATES, MIN_SCORE

ATTRIBUTES = {"str": "Strength", "agi": "Agility", "int": "Intelligence", "all": "Universal"}
TALENT_PREFIX = "special_bonus_"

METRIC_DEFINITIONS: dict[str, str] = {
    "win_rate": (
        "Wins / matches for the subject (hero, item, build, side) under the stated filters. "
        "Reported with a 95% Wilson score interval; differences inside the interval are noise."
    ),
    "pick_rate": (
        "Matches where the hero was picked / all matches under the filters. Ten heroes are "
        "picked per match, so pick rates of all heroes sum to 1,000%."
    ),
    "ban_rate": "Matches where the hero was banned / all matches under the filters.",
    "advantage": (
        "Matchup or synergy advantage of hero A with or against hero B, in win-rate points: "
        "A's win rate in the pairing minus the log5 expectation from both heroes' overall "
        "win rates, a(1-b) / (a(1-b) + b(1-a)). Positive means A does better than the "
        "heroes' general strength predicts."
    ),
    "gpm": "Gold per minute: the player's total earned gold / match duration in minutes.",
    "xpm": "Experience per minute: the player's total experience / match duration in minutes.",
    "benchmark_percentile": (
        "Where a value sits among players of the same hero (OpenDota /benchmarks): the "
        "Xth percentile means X% of recorded games of that hero had a lower value. "
        "Benchmarks cover GPM, XPM, kills/min, last hits/min, hero damage/min, hero "
        "healing/min and tower damage."
    ),
    "lane_outcome": (
        "Result of the laning phase for a hero's lane (win, draw or loss) as the source "
        "classifies it; tools that report it state the source and its criterion."
    ),
    "sample_size": "Number of matches a figure is based on; below min_sample no figure is given.",
    "confidence_interval": (
        "95% interval for a rate or median: Wilson score interval for rates, percentile "
        "bootstrap for medians. A wide interval means the figure is uncertain."
    ),
}


class Kind(StrEnum):
    HERO = "hero"
    ITEM = "item"
    ABILITY = "ability"
    ANY = "any"


class Reference(BaseModel):
    kind: Literal["hero", "item", "ability"]
    record: dict[str, Any] = Field(description="The record's fields (see each kind's keys).")
    data_patch: str = Field(description="Latest patch in the reference snapshot.")


def _data_patch() -> str:
    return patches()[-1].name


def _ability_name(name: str) -> str:
    ability = ability_by_name().get(name)
    return ability.display_name if ability else name


def hero_record(hero: Hero) -> dict[str, Any]:
    return {
        "id": hero.id,
        "name": hero.display_name,
        "internal_name": hero.name,
        "primary_attribute": ATTRIBUTES.get(hero.primary_attr, hero.primary_attr),
        "attack_type": hero.attack_type,
        "roles": list(hero.roles),
        "abilities": [
            {"name": _ability_name(name), "internal_name": name} for name in hero.abilities
        ],
        "talents": [
            {"level": t.level, "name": _ability_name(t.name), "internal_name": t.name}
            for t in hero.talents
        ],
        "facets": [
            {"id": f.id, "name": f.title or f.name, "internal_name": f.name}
            for f in hero.facets
            if not f.deprecated
        ],
    }


def _item_display(name: str) -> str:
    for item in items():
        if item.name == name:
            return item.display_name or name
    return name


def item_record(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.display_name,
        "internal_name": item.name,
        "cost": item.cost,
        "quality": item.quality,
        "components": [{"name": _item_display(c), "internal_name": c} for c in item.components],
        "is_recipe": item.is_recipe,
    }


def ability_record(ability: Ability) -> dict[str, Any]:
    owner = hero_by_id().get(ability.hero_id) if ability.hero_id is not None else None
    return {
        "name": ability.display_name,
        "internal_name": ability.name,
        "hero": owner.display_name if owner else None,
        "hero_id": ability.hero_id,
        "behavior": list(ability.behavior),
        "damage_type": ability.damage_type,
        "pierces_spell_immunity": ability.pierces_immunity,
    }


@cache
def _ability_index() -> dict[str, list[Ability]]:
    """Normalized display and internal names -> abilities, hero abilities first."""
    index: dict[str, list[Ability]] = {}
    ranked = sorted(abilities(), key=lambda a: a.hero_id is None)
    for ability in ranked:
        if ability.name.startswith(TALENT_PREFIX):
            continue
        for key in {normalize(ability.display_name), normalize(ability.name)}:
            if key:
                index.setdefault(key, []).append(ability)
    return index


def search_abilities(query: str) -> list[tuple[Ability, float]]:
    """Abilities matching ``query``, best first, as (ability, score 0..1)."""
    key = normalize(query)
    if not key:
        return []
    index = _ability_index()
    scored: dict[str, tuple[Ability, float]] = {}

    def offer(found: list[Ability], score: float) -> None:
        for ability in found:
            if score > scored.get(ability.name, (ability, 0.0))[1]:
                scored[ability.name] = (ability, score)

    if key in index:
        # Neutral-creep and unused copies share names; keep the hero's version when it exists.
        found = index[key]
        owned = [a for a in found if a.hero_id is not None]
        offer(owned or found[:1], 1.0)
    else:
        for name in index:
            if len(key) >= 3 and name.startswith(key):
                offer(index[name][:1], 0.7 + 0.2 * len(key) / len(name))
        for name in difflib.get_close_matches(key, list(index), n=10, cutoff=0.75):
            ratio = difflib.SequenceMatcher(None, key, name).ratio()
            offer(index[name][:1], ratio * 0.9)
    ranked = sorted(scored.values(), key=lambda pair: -pair[1])
    return [(a, round(s, 3)) for a, s in ranked if s >= MIN_SCORE][:MAX_CANDIDATES]


def _candidates(query: str, kind: Kind) -> list[tuple[str, Any, float]]:
    found: list[tuple[str, Any, float]] = []
    if kind in (Kind.HERO, Kind.ANY):
        found += [("hero", c.record, c.score) for c in resolve_hero(query).candidates]
    if kind in (Kind.ITEM, Kind.ANY):
        found += [("item", c.record, c.score) for c in resolve_item(query).candidates]
    if kind in (Kind.ABILITY, Kind.ANY):
        found += [("ability", a, s) for a, s in search_abilities(query)]
    return sorted(found, key=lambda c: -c[2])


RECORD_BUILDERS = {"hero": hero_record, "item": item_record, "ability": ability_record}


def _label(kind: str, record: Any) -> str:
    if kind == "ability" and record.hero_id in hero_by_id():
        return f"{record.display_name} ({hero_by_id()[record.hero_id].display_name} ability)"
    return f"{record.display_name} ({kind})"


def lookup(query: str, kind: Kind) -> ToolResult[Reference]:
    found = _candidates(query, kind)
    if not found:
        return error(
            f"Nothing in the reference data matches {query!r} (kind {kind.value}).",
            hint="Check the spelling, or use kind 'any'.",
        )
    best_kind, best, score = found[0]
    rivals = [c for c in found[1:] if c[2] >= score - AMBIGUITY_MARGIN]
    if rivals:
        names = "; ".join(_label(k, r) for k, r, _ in found[:MAX_CANDIDATES])
        return error(
            f"{query!r} is ambiguous. Candidates: {names}.",
            hint="Ask the user which one they mean, then look it up by its full name.",
        )
    return ok(
        Reference(
            kind=best_kind, record=RECORD_BUILDERS[best_kind](best), data_patch=_data_patch()
        ),
        caveats=[
            "Reference data is a snapshot of OpenDota's game constants (dotaconstants) "
            f"taken at patch {_data_patch()}; costs and kits can change in later patches.",
        ],
        source=["opendota"],
    )


def _hero_from_key(key: str) -> Hero:
    if key.isdigit() and int(key) in hero_by_id():
        return hero_by_id()[int(key)]
    best = resolve_hero(key).best
    if best is None:
        raise ResourceError(f"No hero matches {key!r}. Read heroes://list for valid ids.")
    return best


def _item_from_key(key: str) -> Item:
    if key.isdigit() and int(key) in item_by_id():
        return item_by_id()[int(key)]
    best = resolve_item(key).best
    if best is None:
        raise ResourceError(f"No item matches {key!r}. Read items://list for valid ids.")
    return best


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def lookup_reference(
        query: Annotated[
            str,
            Field(
                description=(
                    "A hero, item or ability name as a person would type it, in English or "
                    'Russian, including slang ("PA", "Фантомка", "bkb", "blink").'
                )
            ),
        ],
        kind: Annotated[
            Kind,
            Field(description="What to look for. 'any' searches heroes, items and abilities."),
        ] = Kind.ANY,
    ) -> ToolResult[Reference]:
        """Look up a hero, item or ability: names, attributes and roles, abilities and
        talents, item cost and components, ability behaviour and damage type, and the
        patch the reference data is from. Ambiguous queries return the candidates."""
        return lookup(query, kind)

    @mcp.resource(
        "heroes://list",
        mime_type="application/json",
        description="Every hero: id, name, primary attribute, attack type, roles.",
    )
    def hero_list() -> list[dict[str, Any]]:
        return [
            {
                "id": h.id,
                "name": h.display_name,
                "internal_name": h.name,
                "primary_attribute": ATTRIBUTES.get(h.primary_attr, h.primary_attr),
                "attack_type": h.attack_type,
                "roles": list(h.roles),
            }
            for h in heroes()
        ]

    @mcp.resource(
        "heroes://{hero}",
        mime_type="application/json",
        description="One hero by id or name: attributes, roles, abilities, talents, facets.",
    )
    def hero_detail(hero: str) -> dict[str, Any]:
        return hero_record(_hero_from_key(hero)) | {"data_patch": _data_patch()}

    @mcp.resource(
        "items://list",
        mime_type="application/json",
        description="Every purchasable item (no recipes): id, name, cost, quality.",
    )
    def item_list() -> list[dict[str, Any]]:
        return [
            {
                "id": i.id,
                "name": i.display_name,
                "internal_name": i.name,
                "cost": i.cost,
                "quality": i.quality,
            }
            for i in items()
            if i.display_name and not i.is_recipe
        ]

    @mcp.resource(
        "items://{item}",
        mime_type="application/json",
        description="One item by id or name: cost, quality, components.",
    )
    def item_detail(item: str) -> dict[str, Any]:
        return item_record(_item_from_key(item)) | {"data_patch": _data_patch()}

    @mcp.resource(
        "patches://list",
        mime_type="application/json",
        description="Game patches in release order with release dates (UTC).",
    )
    def patch_list() -> list[dict[str, Any]]:
        return [{"id": p.id, "name": p.name, "date": p.date.isoformat()} for p in patches()]

    @mcp.resource(
        "metrics://definitions",
        mime_type="application/json",
        description="How the metrics reported across tools are defined.",
    )
    def metric_definitions() -> dict[str, str]:
        return METRIC_DEFINITIONS

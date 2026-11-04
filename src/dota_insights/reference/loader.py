"""Typed access to the OpenDota constants snapshots shipped in ``reference/data``.

Snapshots are produced by ``scripts/update_reference.py``. Every loader is cached, so the
JSON is parsed at most once per process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from importlib import resources
from typing import Any

HERO_PREFIX = "npc_dota_hero_"


@dataclass(frozen=True, slots=True)
class Talent:
    name: str
    level: int


@dataclass(frozen=True, slots=True)
class Facet:
    id: int
    name: str
    title: str | None
    deprecated: bool


@dataclass(frozen=True, slots=True)
class Hero:
    id: int
    name: str  # internal name, e.g. "npc_dota_hero_antimage"
    display_name: str
    primary_attr: str  # "str" | "agi" | "int" | "all"
    attack_type: str  # "Melee" | "Ranged"
    roles: tuple[str, ...]
    abilities: tuple[str, ...] = ()
    talents: tuple[Talent, ...] = ()
    facets: tuple[Facet, ...] = ()

    @property
    def short_name(self) -> str:
        """Internal name without the ``npc_dota_hero_`` prefix, e.g. ``"antimage"``."""
        return self.name.removeprefix(HERO_PREFIX)


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    name: str  # internal name, e.g. "black_king_bar"
    display_name: str | None
    cost: int | None
    quality: str | None
    components: tuple[str, ...]

    @property
    def is_recipe(self) -> bool:
        return self.name.startswith("recipe_")


@dataclass(frozen=True, slots=True)
class Ability:
    name: str
    display_name: str
    behavior: tuple[str, ...]
    damage_type: str | None
    pierces_immunity: str | None  # raw OpenDota value: "Yes" / "No" / None
    hero_id: int | None  # owning hero, when the ability is in a hero's kit


@dataclass(frozen=True, slots=True)
class Patch:
    id: int
    name: str
    date: datetime


def _read(name: str) -> Any:
    path = resources.files(__package__).joinpath("data", f"{name}.json")
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def heroes() -> tuple[Hero, ...]:
    """All heroes, ordered by id."""
    kits = _read("hero_abilities")
    result = []
    for raw in _read("heroes"):
        kit = kits.get(raw["name"], {})
        result.append(
            Hero(
                id=raw["id"],
                name=raw["name"],
                display_name=raw["localized_name"],
                primary_attr=raw["primary_attr"],
                attack_type=raw["attack_type"],
                roles=tuple(raw["roles"]),
                abilities=tuple(kit.get("abilities", ())),
                talents=tuple(Talent(t["name"], t["level"]) for t in kit.get("talents", ())),
                facets=tuple(
                    Facet(f["id"], f["name"], f["title"], f["deprecated"])
                    for f in kit.get("facets", ())
                ),
            )
        )
    return tuple(result)


@cache
def items() -> tuple[Item, ...]:
    """All items OpenDota knows, including recipes and event items, ordered by id."""
    return tuple(
        Item(
            id=raw["id"],
            name=raw["name"],
            display_name=raw["dname"],
            cost=raw["cost"],
            quality=raw["qual"],
            components=tuple(raw["components"]),
        )
        for raw in _read("items")
    )


@cache
def abilities() -> tuple[Ability, ...]:
    """Abilities and talents that have a display name, ordered by internal name."""
    owner = {}
    for hero in heroes():
        for name in (*hero.abilities, *(t.name for t in hero.talents)):
            owner.setdefault(name, hero.id)
    return tuple(
        Ability(
            name=raw["name"],
            display_name=raw["dname"],
            behavior=tuple(raw["behavior"]),
            damage_type=raw["dmg_type"],
            pierces_immunity=raw["bkbpierce"],
            hero_id=owner.get(raw["name"]),
        )
        for raw in _read("abilities")
    )


@cache
def patches() -> tuple[Patch, ...]:
    """Game patches in release order."""
    return tuple(
        Patch(
            id=raw["id"],
            name=raw["name"],
            date=datetime.fromisoformat(raw["date"]),
        )
        for raw in _read("patches")
    )


@cache
def item_ids() -> dict[int, str]:
    """OpenDota's item id -> internal name table (covers some ids absent from ``items()``)."""
    return {int(key): name for key, name in _read("item_ids").items()}


@cache
def hero_by_id() -> dict[int, Hero]:
    return {hero.id: hero for hero in heroes()}


@cache
def item_by_id() -> dict[int, Item]:
    return {item.id: item for item in items()}


@cache
def ability_by_name() -> dict[str, Ability]:
    return {ability.name: ability for ability in abilities()}


def patch_at(when: datetime) -> Patch | None:
    """The patch that was live at ``when`` (timezone-aware), or None if before the first."""
    current = None
    for patch in patches():
        if patch.date > when:
            break
        current = patch
    return current

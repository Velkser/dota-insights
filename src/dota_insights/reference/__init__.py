"""Dota 2 reference data (heroes, items, abilities, patches) and name resolution."""

from .loader import (
    Ability,
    Facet,
    Hero,
    Item,
    Patch,
    Talent,
    abilities,
    ability_by_name,
    hero_by_id,
    heroes,
    item_by_id,
    item_ids,
    items,
    patch_at,
    patches,
)
from .resolver import Candidate, Resolution, resolve_hero, resolve_item

__all__ = [
    "Ability",
    "Candidate",
    "Facet",
    "Hero",
    "Item",
    "Patch",
    "Resolution",
    "Talent",
    "abilities",
    "ability_by_name",
    "hero_by_id",
    "heroes",
    "item_by_id",
    "item_ids",
    "items",
    "patch_at",
    "patches",
    "resolve_hero",
    "resolve_item",
]

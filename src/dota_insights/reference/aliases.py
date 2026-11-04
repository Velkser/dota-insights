"""Alias tables for heroes and items.

Each hero or item gets aliases from three tiers, highest priority first:

1. ``NAME`` - display name and internal name, generated from the snapshots, plus the usual
   Russian name from ``data/names_ru.json``.
2. ``SLANG`` - hand-written English short forms and slang below, plus the Russian aliases
   from ``data/names_ru.json``.
3. ``INITIALS`` - first letters of a multi-word display name ("Queen of Pain" -> "qop"),
   generated.

When the same normalized alias appears in several tiers, the highest tier wins, so a
generated initialism never overrides slang ("ES" is Earthshaker, not Earth Spirit). Within
one tier an alias may point at several records; resolving it then reports the ambiguity.

Slang and Russian names are keyed by internal name (hero short name without
``npc_dota_hero_``, or item name) and only list forms in common use; the test suite checks
every key exists in the data and that no Russian name or alias is ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import IntEnum
from functools import cache

from .loader import Hero, Item, heroes, items, ru_names

HERO_SLANG: dict[str, tuple[str, ...]] = {
    "antimage": ("am",),
    "bloodseeker": ("bs", "blood"),
    "crystal_maiden": ("cm",),
    "drow_ranger": ("drow", "traxex"),
    "earthshaker": ("es", "shaker"),
    "juggernaut": ("jugg", "jug"),
    "mirana": ("potm",),
    "morphling": ("morph",),
    "nevermore": ("sf",),
    "phantom_lancer": ("pl",),
    "sand_king": ("sk",),
    "storm_spirit": ("storm",),
    "vengefulspirit": ("vs", "venge"),
    "windrunner": ("wr", "windrunner"),
    "zuus": ("zeus",),
    "shadow_shaman": ("shaman", "rhasta"),
    "tidehunter": ("tide",),
    "witch_doctor": ("wd",),
    "necrolyte": ("necro", "necrolyte"),
    "beastmaster": ("bm",),
    "queenofpain": ("qop",),
    "venomancer": ("veno",),
    "faceless_void": ("void", "fv"),
    "skeleton_king": ("wk", "skeleton king"),
    "death_prophet": ("dp",),
    "phantom_assassin": ("pa",),
    "templar_assassin": ("ta",),
    "dragon_knight": ("dk",),
    "rattletrap": ("clock",),
    "leshrac": ("lesh",),
    "furion": ("np", "furion"),
    "life_stealer": ("ls", "naix"),
    "dark_seer": ("ds",),
    "omniknight": ("omni",),
    "enchantress": ("ench",),
    "night_stalker": ("ns",),
    "broodmother": ("brood",),
    "bounty_hunter": ("bh", "bounty"),
    "jakiro": ("thd",),
    "batrider": ("bat",),
    "spectre": ("spec",),
    "ancient_apparition": ("aa",),
    "doom_bringer": ("doom",),
    "spirit_breaker": ("sb",),
    "gyrocopter": ("gyro",),
    "alchemist": ("alch",),
    "invoker": ("inv", "invo"),
    "obsidian_destroyer": ("od", "outworld destroyer"),
    "brewmaster": ("brew",),
    "shadow_demon": ("sd",),
    "lone_druid": ("ld",),
    "chaos_knight": ("ck",),
    "treant": ("treant",),
    "ogre_magi": ("ogre",),
    "nyx_assassin": ("nyx",),
    "naga_siren": ("naga",),
    "keeper_of_the_light": ("kotl",),
    "wisp": ("wisp",),
    "medusa": ("dusa",),
    "troll_warlord": ("troll",),
    "centaur": ("cent", "centaur"),
    "magnataur": ("magnus", "mag"),
    "shredder": ("timber",),
    "bristleback": ("bb",),
    "skywrath_mage": ("sky",),
    "abaddon": ("aba",),
    "elder_titan": ("et", "titan"),
    "legion_commander": ("lc", "legion"),
    "ember_spirit": ("ember",),
    "abyssal_underlord": ("underlord", "pit lord"),
    "terrorblade": ("tb",),
    "winter_wyvern": ("ww", "wyvern"),
    "arc_warden": ("arc",),
    "monkey_king": ("mk",),
    "dark_willow": ("willow",),
    "pangolier": ("pango",),
    "grimstroke": ("grim",),
    "hoodwink": ("hood",),
    "void_spirit": ("vs",),
    "snapfire": ("snap",),
    "ringmaster": ("ringmaster",),
    "dawnbreaker": ("dawn",),
    "primal_beast": ("pb",),
}

ITEM_SLANG: dict[str, tuple[str, ...]] = {
    "black_king_bar": ("bkb",),
    "blink": ("blink", "dagger"),
    "ultimate_scepter": ("aghs", "aghanim", "scepter"),
    "aghanims_shard": ("shard", "aghs shard"),
    "sphere": ("linken", "linkens"),
    "bfury": ("bf", "bfury"),
    "manta": ("manta",),
    "desolator": ("deso",),
    "butterfly": ("fly",),
    "skadi": ("skadi",),
    "monkey_king_bar": ("mkb",),
    "abyssal_blade": ("abyssal",),
    "basher": ("basher",),
    "heart": ("heart", "tarrasque"),
    "assault": ("ac", "assault"),
    "shivas_guard": ("shiva", "shivas"),
    "refresher": ("refresher",),
    "sheepstick": ("hex", "sheep", "vyse"),
    "orchid": ("orchid",),
    "silver_edge": ("silver",),
    "invis_sword": ("sb", "lothar", "shadow blade"),
    "travel_boots": ("bot", "travels"),
    "power_treads": ("treads",),
    "phase_boots": ("phase",),
    "arcane_boots": ("arcanes",),
    "tranquil_boots": ("tranquils",),
    "hand_of_midas": ("midas",),
    "force_staff": ("force",),
    "glimmer_cape": ("glimmer",),
    "ghost": ("ghost",),
    "cyclone": ("euls", "eul"),
    "mekansm": ("mek",),
    "guardian_greaves": ("greaves",),
    "pipe": ("pipe",),
    "vladmir": ("vlads", "vlad"),
    "aether_lens": ("lens",),
    "rapier": ("rapier",),
    "diffusal_blade": ("diffusal", "diffu"),
    "mjollnir": ("mjol",),
    "greater_crit": ("daedalus", "crit"),
    "octarine_core": ("octarine",),
    "sange_and_yasha": ("sny", "s&y"),
    "kaya_and_sange": ("kns", "k&s"),
    "yasha_and_kaya": ("ynk", "y&k"),
    "echo_sabre": ("echo",),
    "armlet": ("armlet",),
    "ward_observer": ("obs", "observer"),
    "ward_sentry": ("sentry", "sentries"),
    "tpscroll": ("tp",),
    "smoke_of_deceit": ("smoke",),
    "blade_mail": ("bm",),
    "lotus_orb": ("lotus",),
    "aeon_disk": ("aeon",),
    "spirit_vessel": ("vessel",),
    "urn_of_shadows": ("urn",),
    "heavens_halberd": ("halberd",),
    "crimson_guard": ("crimson",),
    "hurricane_pike": ("pike",),
    "ethereal_blade": ("eblade",),
    "meteor_hammer": ("meteor",),
    "solar_crest": ("solar",),
    "wind_waker": ("waker",),
    "moon_shard": ("moon",),
    "magic_wand": ("wand",),
}


class Tier(IntEnum):
    INITIALS = 1
    SLANG = 2
    NAME = 3


# QWERTY <-> ЙЦУКЕН key positions, used to undo text typed in the wrong keyboard layout.
_EN_KEYS = "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
_RU_KEYS = "ёйцукенгшщзхъфывапролджэячсмитьбю"
EN_TO_RU = str.maketrans(_EN_KEYS, _RU_KEYS)
RU_TO_EN = str.maketrans(_RU_KEYS, _EN_KEYS)

_NON_ALNUM = re.compile(r"[\W_]+")
_WORD_SPLIT = re.compile(r"[\s\-_]+")
_SKIP_WORDS = {"of", "the", "and"}


def normalize(text: str) -> str:
    """Case-fold, unify ё/е and drop everything that is not a letter or digit."""
    return _NON_ALNUM.sub("", text.casefold().replace("ё", "е"))


def words(text: str) -> list[str]:
    """Normalized words of a display name, without filler words."""
    parts = (normalize(p) for p in _WORD_SPLIT.split(text))
    return [p for p in parts if p and p not in _SKIP_WORDS]


def initials(text: str) -> str | None:
    """First letters of every word ("Keeper of the Light" -> "kotl"); None for one word."""
    parts = [normalize(p) for p in _WORD_SPLIT.split(text)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    return "".join(p[0] for p in parts)


AliasIndex = dict[str, frozenset[int]]


def _build(entries: Iterable[tuple[str, int, Tier]]) -> AliasIndex:
    best: dict[str, tuple[Tier, set[int]]] = {}
    for alias, record_id, tier in entries:
        key = normalize(alias)
        if not key:
            continue
        current = best.get(key)
        if current is None or tier > current[0]:
            best[key] = (tier, {record_id})
        elif tier == current[0]:
            current[1].add(record_id)
    return {key: frozenset(ids) for key, (_, ids) in best.items()}


def _hero_entries(hero: Hero) -> Iterable[tuple[str, int, Tier]]:
    yield hero.display_name, hero.id, Tier.NAME
    yield hero.name, hero.id, Tier.NAME
    yield hero.short_name, hero.id, Tier.NAME
    for alias in HERO_SLANG.get(hero.short_name, ()):
        yield alias, hero.id, Tier.SLANG
    if ru := ru_names()["heroes"].get(hero.short_name):
        yield ru.name, hero.id, Tier.NAME
        for alias in ru.aliases:
            yield alias, hero.id, Tier.SLANG
    if abbreviation := initials(hero.display_name):
        yield abbreviation, hero.id, Tier.INITIALS


def _item_entries(item: Item, display_owner: dict[str, int]) -> Iterable[tuple[str, int, Tier]]:
    yield item.name, item.id, Tier.NAME
    if item.display_name is None:
        return
    # Leveled items (Dagon 1-5, Necronomicon 1-3) share a display name; it names level 1.
    if display_owner[normalize(item.display_name)] == item.id:
        yield item.display_name, item.id, Tier.NAME
    for alias in ITEM_SLANG.get(item.name, ()):
        yield alias, item.id, Tier.SLANG
    if ru := ru_names()["items"].get(item.name):
        yield ru.name, item.id, Tier.NAME
        for alias in ru.aliases:
            yield alias, item.id, Tier.SLANG
    if not item.is_recipe and (abbreviation := initials(item.display_name)):
        yield abbreviation, item.id, Tier.INITIALS


@cache
def hero_aliases() -> AliasIndex:
    """Normalized alias -> ids of the heroes it may refer to."""
    return _build(entry for hero in heroes() for entry in _hero_entries(hero))


@cache
def item_aliases() -> AliasIndex:
    """Normalized alias -> ids of the items it may refer to."""
    display_owner: dict[str, int] = {}
    for item in items():
        if item.display_name is None:
            continue
        key = normalize(item.display_name)
        owner = display_owner.get(key)
        # Prefer the item whose internal name spells the display name, else the lowest id.
        if owner is None or normalize(item.name) == key:
            display_owner[key] = item.id
    return _build(entry for item in items() for entry in _item_entries(item, display_owner))

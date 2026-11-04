"""Alias tables for heroes and items.

Each hero or item gets aliases from three tiers, highest priority first:

1. ``NAME`` - display name and internal name, generated from the snapshots.
2. ``SLANG`` - hand-written short forms and community slang (English and Russian) below.
3. ``INITIALS`` - first letters of a multi-word display name ("Queen of Pain" -> "qop"),
   generated.

When the same normalized alias appears in several tiers, the highest tier wins, so a
generated initialism never overrides slang ("ES" is Earthshaker, not Earth Spirit). Within
one tier an alias may point at several records; resolving it then reports the ambiguity.

Slang is keyed by internal name (hero short name without ``npc_dota_hero_``, or item name)
and only lists forms in common use; the test suite checks every key exists in the data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import IntEnum
from functools import cache

from .loader import Hero, Item, heroes, items

HERO_SLANG: dict[str, tuple[str, ...]] = {
    "antimage": ("am", "антимаг", "ам", "магина"),
    "axe": ("акс",),
    "bane": ("бейн",),
    "bloodseeker": ("bs", "blood", "бс", "бладсикер", "сикер"),
    "crystal_maiden": ("cm", "цм", "кристалка", "кристал мейден"),
    "drow_ranger": ("drow", "traxex", "дров", "дровка"),
    "earthshaker": ("es", "shaker", "ес", "шейкер", "эртшейкер"),
    "juggernaut": ("jugg", "jug", "джаггернаут", "джагернаут", "джагер", "джаг"),
    "mirana": ("potm", "мирана"),
    "morphling": ("morph", "морф", "морфлинг"),
    "nevermore": ("sf", "сф", "шадоу финд", "невермор"),
    "phantom_lancer": ("pl", "пл", "лансер", "фантом лансер"),
    "puck": ("пак",),
    "pudge": ("пудж",),
    "razor": ("разор",),
    "sand_king": ("sk", "ск", "сенд кинг", "сэнд кинг"),
    "storm_spirit": ("storm", "шторм", "шторм спирит"),
    "sven": ("свен",),
    "tiny": ("тини", "тайни"),
    "vengefulspirit": ("vs", "venge", "венга", "венж"),
    "windrunner": ("wr", "windrunner", "вр", "виндраннер", "винда"),
    "zuus": ("zeus", "зевс"),
    "kunkka": ("кунка", "адмирал"),
    "lina": ("лина",),
    "lion": ("лион", "лайон"),
    "shadow_shaman": ("shaman", "rhasta", "шаман", "раста"),
    "slardar": ("слардар",),
    "tidehunter": ("tide", "тайд", "тайдхантер"),
    "witch_doctor": ("wd", "вд", "вичдоктор"),
    "lich": ("лич",),
    "riki": ("рики",),
    "enigma": ("энигма",),
    "tinker": ("тинкер",),
    "sniper": ("снайпер",),
    "necrolyte": ("necro", "necrolyte", "некр", "некрофос"),
    "warlock": ("варлок",),
    "beastmaster": ("bm", "бм", "бистмастер"),
    "queenofpain": ("qop", "акаша", "квопа"),
    "venomancer": ("veno", "веник", "веномансер"),
    "faceless_void": ("void", "fv", "войд", "фв", "фейслес войд"),
    "skeleton_king": ("wk", "skeleton king", "вк", "скелет", "леорик"),
    "death_prophet": ("dp", "дп"),
    "phantom_assassin": ("pa", "па", "фантомка", "мортред"),
    "pugna": ("пугна",),
    "templar_assassin": ("ta", "та", "темпларка", "ланая"),
    "viper": ("вайпер",),
    "luna": ("луна",),
    "dragon_knight": ("dk", "дк", "давион"),
    "dazzle": ("дазл",),
    "rattletrap": ("clock", "клок", "клокверк"),
    "leshrac": ("lesh", "леш", "лешрак"),
    "furion": ("np", "furion", "нп", "фурион"),
    "life_stealer": ("ls", "naix", "лс", "гуля", "найкс"),
    "dark_seer": ("ds", "дс", "дарк сир"),
    "clinkz": ("клинкз",),
    "omniknight": ("omni", "омни", "омник"),
    "enchantress": ("ench", "энча", "энчантресс"),
    "huskar": ("хускар",),
    "night_stalker": ("ns", "нс", "балансар", "найт сталкер"),
    "broodmother": ("brood", "бруда", "бруд"),
    "bounty_hunter": ("bh", "bounty", "бх", "баунти"),
    "weaver": ("вивер",),
    "jakiro": ("thd", "джакиро"),
    "batrider": ("bat", "бэт", "бэтрайдер"),
    "chen": ("чен",),
    "spectre": ("spec", "спектра", "спектр"),
    "ancient_apparition": ("aa", "аа"),
    "doom_bringer": ("doom", "дум"),
    "ursa": ("урса",),
    "spirit_breaker": ("sb", "сб", "бара", "барадун"),
    "gyrocopter": ("gyro", "гиро", "гирокоптер"),
    "alchemist": ("alch", "алх", "алхимик"),
    "invoker": ("inv", "invo", "инв", "инвокер", "инвок", "карл"),
    "silencer": ("сайленсер",),
    "obsidian_destroyer": ("od", "од", "outworld destroyer"),
    "lycan": ("ликан",),
    "brewmaster": ("brew", "брю", "брюмастер", "панда"),
    "shadow_demon": ("sd", "сд", "шадоу демон"),
    "lone_druid": ("ld", "лд", "друид"),
    "chaos_knight": ("ck", "цк", "хаос"),
    "meepo": ("мипо",),
    "treant": ("treant", "трент"),
    "ogre_magi": ("ogre", "огр"),
    "undying": ("андаинг",),
    "rubick": ("рубик",),
    "disruptor": ("дизраптор",),
    "nyx_assassin": ("nyx", "никс"),
    "naga_siren": ("naga", "нага"),
    "keeper_of_the_light": ("kotl", "котл"),
    "wisp": ("wisp", "висп", "ио"),
    "visage": ("визаж",),
    "slark": ("сларк",),
    "medusa": ("dusa", "медуза"),
    "troll_warlord": ("troll", "тролль", "трол"),
    "centaur": ("cent", "centaur", "кентавр", "цент"),
    "magnataur": ("magnus", "mag", "магнус"),
    "shredder": ("timber", "тимбер", "тимбермен"),
    "bristleback": ("bb", "бб", "бристл", "бристлбек"),
    "tusk": ("туск",),
    "skywrath_mage": ("sky", "скай", "скайрат"),
    "abaddon": ("aba", "абаддон", "абадон"),
    "elder_titan": ("et", "titan", "титан"),
    "legion_commander": ("lc", "legion", "легионка", "легион"),
    "techies": ("техи", "техис"),
    "ember_spirit": ("ember", "эмбер"),
    "earth_spirit": ("эрс спирит",),
    "abyssal_underlord": ("underlord", "pit lord", "андерлорд", "питлорд"),
    "terrorblade": ("tb", "тб", "террорблейд"),
    "phoenix": ("феникс",),
    "oracle": ("оракл",),
    "winter_wyvern": ("ww", "wyvern", "вв", "виверна"),
    "arc_warden": ("arc", "арк", "зет"),
    "monkey_king": ("mk", "мк", "манки"),
    "dark_willow": ("willow", "вилоу"),
    "pangolier": ("pango", "панго", "панголиер"),
    "grimstroke": ("grim", "грим", "гримстрок"),
    "hoodwink": ("hood", "худвинк", "белка"),
    "void_spirit": ("vs", "войд спирит"),
    "snapfire": ("snap", "снэпфаер", "бабка"),
    "mars": ("марс",),
    "ringmaster": ("ringmaster", "рингмастер"),
    "dawnbreaker": ("dawn", "давнбрейкер"),
    "marci": ("марси",),
    "primal_beast": ("pb", "пб", "праймал"),
    "muerta": ("муэрта",),
    "kez": ("кез",),
    "largo": ("ларго",),
}

ITEM_SLANG: dict[str, tuple[str, ...]] = {
    "black_king_bar": ("bkb", "бкб", "бкбшка"),
    "blink": ("blink", "dagger", "блинк", "даггер"),
    "ultimate_scepter": ("aghs", "aghanim", "scepter", "аганим", "аги", "агс"),
    "aghanims_shard": ("shard", "aghs shard", "шард"),
    "sphere": ("linken", "linkens", "линка", "линкен", "линкенс"),
    "radiance": ("радиант", "радик", "радианс"),
    "bfury": ("bf", "bfury", "бф", "бфура"),
    "manta": ("manta", "манта"),
    "desolator": ("deso", "дезоль", "дезолятор"),
    "butterfly": ("fly", "бабочка", "баттерфляй"),
    "satanic": ("сатаник",),
    "skadi": ("skadi", "скади"),
    "monkey_king_bar": ("mkb", "мкб"),
    "abyssal_blade": ("abyssal", "абиссал", "абисал"),
    "basher": ("basher", "башер"),
    "heart": ("heart", "tarrasque", "харт", "тараска"),
    "assault": ("ac", "assault", "кираса", "ассаулт"),
    "shivas_guard": ("shiva", "shivas", "шива"),
    "refresher": ("refresher", "рефрешер", "рефреш"),
    "sheepstick": ("hex", "sheep", "vyse", "хекс", "гекс", "овца"),
    "orchid": ("orchid", "орчид"),
    "bloodthorn": ("бладторн",),
    "silver_edge": ("silver", "силвер", "силвер эдж"),
    "invis_sword": ("sb", "lothar", "shadow blade", "лотар", "шадоу блейд"),
    "travel_boots": ("bot", "travels", "тревела", "тревелы"),
    "power_treads": ("treads", "тредсы", "треды"),
    "phase_boots": ("phase", "фейзы", "фазы"),
    "arcane_boots": ("arcanes", "арканы"),
    "tranquil_boots": ("tranquils", "транки"),
    "hand_of_midas": ("midas", "мидас"),
    "force_staff": ("force", "форс", "форсстафф"),
    "glimmer_cape": ("glimmer", "глиммер"),
    "ghost": ("ghost", "гост"),
    "cyclone": ("euls", "eul", "еул", "еулс"),
    "mekansm": ("mek", "мека", "меканизм"),
    "guardian_greaves": ("greaves", "гривсы", "гривы"),
    "pipe": ("pipe", "пайп", "пайпа"),
    "vladmir": ("vlads", "vlad", "влад", "владимир"),
    "aether_lens": ("lens", "линза"),
    "dagon": ("дагон",),
    "rapier": ("rapier", "рапира"),
    "diffusal_blade": ("diffusal", "diffu", "диффуз", "диффузал"),
    "maelstrom": ("мальстрём", "мальстрим"),
    "mjollnir": ("mjol", "мьелнир", "мьёлнир", "мжолнир"),
    "gungir": ("глейпнир",),
    "greater_crit": ("daedalus", "crit", "дедал", "крит"),
    "lesser_crit": ("кристалис",),
    "nullifier": ("нуллифаер", "нулик"),
    "octarine_core": ("octarine", "октарин"),
    "sange_and_yasha": ("sny", "s&y", "санга и яша", "сня"),
    "kaya_and_sange": (
        "kns",
        "k&s",
    ),
    "yasha_and_kaya": (
        "ynk",
        "y&k",
    ),
    "echo_sabre": ("echo", "эхо"),
    "armlet": ("armlet", "армлет"),
    "bottle": ("ботл", "бутылка"),
    "ward_observer": ("obs", "observer", "обс", "обсервер"),
    "ward_sentry": ("sentry", "sentries", "сентри", "сенты"),
    "tpscroll": ("tp", "тп", "телепорт"),
    "smoke_of_deceit": ("smoke", "смок"),
    "dust": ("даст",),
    "blade_mail": ("bm", "бм", "блейдмейл", "блейд мейл"),
    "lotus_orb": ("lotus", "лотус"),
    "aeon_disk": ("aeon", "эон"),
    "spirit_vessel": ("vessel", "вессел"),
    "urn_of_shadows": ("urn", "урна"),
    "heavens_halberd": ("halberd", "алебарда", "халберд"),
    "crimson_guard": ("crimson", "кримсон"),
    "hurricane_pike": ("pike", "пайк"),
    "ethereal_blade": ("eblade", "эфир", "этериал"),
    "meteor_hammer": ("meteor", "метеор"),
    "solar_crest": ("solar", "солар"),
    "wind_waker": ("waker", "виндвейкер"),
    "moon_shard": ("moon", "мун шард"),
    "magic_wand": ("wand", "палка", "ванд"),
    "boots": ("сапоги",),
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

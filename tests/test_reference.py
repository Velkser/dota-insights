from datetime import UTC, datetime

import pytest

from dota_insights.reference import (
    abilities,
    ability_by_name,
    hero_by_id,
    hero_name_ru,
    heroes,
    item_ids,
    item_name_ru,
    items,
    patch_at,
    patches,
    resolve_hero,
    resolve_item,
    ru_by_display_name,
    ru_names,
)
from dota_insights.reference.aliases import (
    HERO_SLANG,
    ITEM_SLANG,
    hero_aliases,
    item_aliases,
    normalize,
)


def test_all_heroes_load_with_unique_ids_and_names():
    all_heroes = heroes()
    assert len(all_heroes) >= 120
    assert len({h.id for h in all_heroes}) == len(all_heroes)
    assert len({h.name for h in all_heroes}) == len(all_heroes)
    for hero in all_heroes:
        assert hero.name.startswith("npc_dota_hero_")
        assert hero.display_name
        assert hero.primary_attr in {"str", "agi", "int", "all"}
        assert hero.attack_type in {"Melee", "Ranged"}
        assert hero.roles
        assert hero.abilities


def test_hero_record_fields():
    am = hero_by_id()[1]
    assert am.display_name == "Anti-Mage"
    assert am.short_name == "antimage"
    assert am.primary_attr == "agi"
    assert "Carry" in am.roles
    assert "antimage_blink" in am.abilities


def test_items_load_with_unique_ids():
    all_items = items()
    assert len({i.id for i in all_items}) == len(all_items)
    assert len({i.name for i in all_items}) == len(all_items)
    bkb = next(i for i in all_items if i.name == "black_king_bar")
    assert bkb.display_name == "Black King Bar"
    assert bkb.cost and bkb.cost > 0
    assert item_ids()[bkb.id] == "black_king_bar"


def test_abilities_link_to_heroes():
    blink = ability_by_name()["antimage_blink"]
    assert blink.display_name == "Blink"
    assert blink.hero_id == 1
    assert len({a.name for a in abilities()}) == len(abilities())


def test_patches_are_ordered_and_dated():
    all_patches = patches()
    assert all_patches[0].name == "6.70"
    dates = [p.date for p in all_patches]
    assert dates == sorted(dates)
    assert all(d.tzinfo is not None for d in dates)
    assert patch_at(datetime(2000, 1, 1, tzinfo=UTC)) is None
    assert patch_at(all_patches[-1].date) == all_patches[-1]


def test_slang_keys_exist_in_data():
    assert set(HERO_SLANG) <= {h.short_name for h in heroes()}
    assert set(ITEM_SLANG) <= {i.name for i in items()}


def test_every_hero_resolves_by_display_and_internal_name():
    for hero in heroes():
        for query in (hero.display_name, hero.name, hero.short_name):
            assert resolve_hero(query).best == hero, query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("PA", "Phantom Assassin"),
        ("Фантомка", "Phantom Assassin"),
        ("phantom assassin", "Phantom Assassin"),
        ("AM", "Anti-Mage"),
        ("Антимаг", "Anti-Mage"),
        ("anti mage", "Anti-Mage"),
        ("Void", "Faceless Void"),
        ("Войд", "Faceless Void"),
        ("ES", "Earthshaker"),
        ("Шейкер", "Earthshaker"),
        ("Inv", "Invoker"),
        ("Инвокер", "Invoker"),
        ("SF", "Shadow Fiend"),
        ("QoP", "Queen of Pain"),
        ("KotL", "Keeper of the Light"),
        ("natures prophet", "Nature's Prophet"),
        ("Фурион", "Nature's Prophet"),
        ("Ёж", None),  # unknown
        ("фь", "Anti-Mage"),  # "am" typed in the Russian layout
        ("Ызусекуы", "Spectre"),  # "Spectre" typed in the Russian layout, plus a typo
        ("invokr", "Invoker"),  # typo
        ("pudg", "Pudge"),  # prefix
    ],
)
def test_hero_aliases_resolve(query, expected):
    best = resolve_hero(query).best
    assert (best.display_name if best else None) == expected


def test_exact_match_has_full_confidence_and_fuzzy_less():
    assert resolve_hero("pa").candidates[0].score == 1.0
    assert 0 < resolve_hero("invokr").candidates[0].score < 1.0


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("phantom", {"Phantom Assassin", "Phantom Lancer"}),
        ("vs", {"Vengeful Spirit", "Void Spirit"}),
        ("assassin", {"Phantom Assassin", "Templar Assassin", "Nyx Assassin"}),
    ],
)
def test_ambiguous_hero_queries_return_candidates(query, expected):
    result = resolve_hero(query)
    assert result.ambiguous
    assert result.best is None
    assert expected <= {c.record.display_name for c in result.candidates}


@pytest.mark.parametrize("query", ["", "   ", "xyzzy", "qwertyuiop"])
def test_unknown_hero_returns_nothing(query):
    result = resolve_hero(query)
    assert not result.found
    assert result.best is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("BKB", "black_king_bar"),
        ("бкб", "black_king_bar"),
        ("Black King Bar", "black_king_bar"),
        ("Блинк", "blink"),
        ("blink dagger", "blink"),
        ("Аганим", "ultimate_scepter"),
        ("Линка", "sphere"),
        ("Радиант", "radiance"),
        ("mkb", "monkey_king_bar"),
        ("Дедал", "greater_crit"),
        ("dagon", "dagon"),
        ("dagon_5", "dagon_5"),
        ("ифкфтшфщ", None),  # unknown
        ("deadalus", "greater_crit"),  # typo
    ],
)
def test_item_aliases_resolve(query, expected):
    best = resolve_item(query).best
    assert (best.name if best else None) == expected


def test_ambiguous_item_query_returns_candidates():
    result = resolve_item("blade")
    assert result.ambiguous
    names = [c.record.name for c in result.candidates]
    assert len(names) == len(set(names)) > 1


def test_unknown_item_returns_nothing():
    assert not resolve_item("zzqqxx").found


def test_normalize_ignores_case_punctuation_and_yo():
    assert normalize("Nature's  Prophet") == "naturesprophet"
    assert normalize("Мьёлнир") == normalize("мьелнир")
    assert "antimage" in hero_aliases()


def test_ru_names_cover_every_hero_and_only_known_items():
    names = ru_names()
    assert set(names["heroes"]) == {h.short_name for h in heroes()}
    assert set(names["items"]) <= {i.name for i in items()}


def _ru_spellings(kind: str) -> list[tuple[str, str]]:
    return [
        (key, spelling)
        for key, entry in ru_names()[kind].items()
        for spelling in (entry.name, *entry.aliases)
    ]


def test_no_russian_hero_alias_maps_to_two_heroes():
    index = hero_aliases()
    by_key = {h.short_name: h for h in heroes()}
    for key, spelling in _ru_spellings("heroes"):
        assert index[normalize(spelling)] == {by_key[key].id}, spelling
        assert resolve_hero(spelling).best == by_key[key], spelling


def test_no_russian_item_alias_maps_to_two_items():
    index = item_aliases()
    by_key = {i.name: i for i in items()}
    for key, spelling in _ru_spellings("items"):
        assert index[normalize(spelling)] == {by_key[key].id}, spelling
        assert resolve_item(spelling).best == by_key[key], spelling


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Пудж", "Pudge"),
        ("пудж", "Pudge"),
        ("ПУДЖ", "Pudge"),
        ("Инвокер", "Invoker"),
        ("Карл", "Invoker"),
        ("АМ", "Anti-Mage"),
        ("ам", "Anti-Mage"),
        ("Магина", "Anti-Mage"),
        ("ВК", "Wraith King"),
        ("ЦК", "Chaos Knight"),
        ("ДС", "Dark Seer"),
        ("Шадоу Финд", "Shadow Fiend"),
        ("Нейчерс Профет", "Nature's Prophet"),
        ("Ио", "Io"),
        ("Бабка", "Snapfire"),
        ("Инвакер", "Invoker"),  # typo
    ],
)
def test_russian_hero_names_and_nicknames_resolve(query, expected):
    best = resolve_hero(query).best
    assert (best.display_name if best else None) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("БКБ", "black_king_bar"),
        ("Бкб", "black_king_bar"),
        ("Блинк", "blink"),
        ("ТП", "tpscroll"),
        ("Мьёльнир", "mjollnir"),
        ("Мьельнир", "mjollnir"),  # е for ё
        ("Гекс", "sheepstick"),
        ("Сапоги", "boots"),
    ],
)
def test_russian_item_names_and_nicknames_resolve(query, expected):
    best = resolve_item(query).best
    assert (best.name if best else None) == expected


def test_russian_display_names():
    pudge = next(h for h in heroes() if h.short_name == "pudge")
    bkb = next(i for i in items() if i.name == "black_king_bar")
    assert hero_name_ru(pudge) == "Пудж"
    assert item_name_ru(bkb) == "БКБ"
    assert ru_by_display_name()["Invoker"] == "Инвокер"

"""Refresh the reference snapshots in ``src/dota_insights/reference/data/``.

Downloads OpenDota constants and writes trimmed, stably ordered JSON files that the
``dota_insights.reference`` package loads at runtime. Run with::

    uv run python scripts/update_reference.py

Uses only the standard library so it stays independent of the application's HTTP client.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://api.opendota.com/api/constants/"
RESOURCES = ("heroes", "items", "abilities", "hero_abilities", "patch", "item_ids")
DATA_DIR = Path(__file__).resolve().parents[1] / "src" / "dota_insights" / "reference" / "data"
# Placeholder slots OpenDota lists in hero ability arrays; they are not real abilities.
SKIP_ABILITIES = {"generic_hidden", "dota_empty"}


def fetch(resource: str) -> Any:
    request = urllib.request.Request(
        BASE_URL + resource, headers={"User-Agent": "dota-insights-reference-updater"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _str_or_none(value: Any) -> str | None:
    """OpenDota uses ``[]`` or a missing key for "no value"; collapse both to None."""
    if isinstance(value, str) and value:
        return value
    return None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _truthy(value: Any) -> bool:
    if isinstance(value, list):
        return any(_truthy(v) for v in value)
    return str(value).lower() in {"true", "1"}


def trim_heroes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    heroes = [
        {
            "id": hero["id"],
            "name": hero["name"],
            "localized_name": hero["localized_name"],
            "primary_attr": hero["primary_attr"],
            "attack_type": hero["attack_type"],
            "roles": hero.get("roles") or [],
        }
        for hero in raw.values()
    ]
    return sorted(heroes, key=lambda h: h["id"])


def trim_hero_abilities(raw: dict[str, Any]) -> dict[str, Any]:
    trimmed = {}
    for hero_name, entry in sorted(raw.items()):
        abilities: list[str] = []
        for slot in entry.get("abilities", []):
            abilities.extend(a for a in _as_list(slot) if a not in SKIP_ABILITIES)
        trimmed[hero_name] = {
            "abilities": abilities,
            "talents": [{"name": t["name"], "level": t["level"]} for t in entry.get("talents", [])],
            "facets": [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "title": f.get("title"),
                    "deprecated": _truthy(f.get("deprecated")),
                }
                for f in entry.get("facets", [])
            ],
        }
    return trimmed


def trim_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = [
        {
            "id": item["id"],
            "name": name,
            "dname": _str_or_none(item.get("dname")),
            "cost": item.get("cost"),
            "qual": _str_or_none(item.get("qual")),
            "components": item.get("components") or [],
        }
        for name, item in raw.items()
    ]
    return sorted(items, key=lambda i: i["id"])


def trim_abilities(raw: dict[str, Any]) -> list[dict[str, Any]]:
    abilities = [
        {
            "name": name,
            "dname": ability["dname"],
            "behavior": _as_list(ability.get("behavior")),
            "dmg_type": _str_or_none(ability.get("dmg_type")),
            "bkbpierce": _str_or_none(ability.get("bkbpierce")),
        }
        for name, ability in raw.items()
        if _str_or_none(ability.get("dname"))
    ]
    return sorted(abilities, key=lambda a: a["name"])


def trim_patches(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patches = [{"id": p["id"], "name": p["name"], "date": p["date"]} for p in raw]
    return sorted(patches, key=lambda p: p["id"])


def trim_item_ids(raw: dict[str, str]) -> dict[str, str]:
    return {key: raw[key] for key in sorted(raw, key=int)}


def write(name: str, payload: Any) -> None:
    path = DATA_DIR / f"{name}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(DATA_DIR.parents[3])} ({len(text):,} bytes)")


def main() -> int:
    raw = {resource: fetch(resource) for resource in RESOURCES}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write("heroes", trim_heroes(raw["heroes"]))
    write("hero_abilities", trim_hero_abilities(raw["hero_abilities"]))
    write("items", trim_items(raw["items"]))
    write("abilities", trim_abilities(raw["abilities"]))
    write("patches", trim_patches(raw["patch"]))
    write("item_ids", trim_item_ids(raw["item_ids"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

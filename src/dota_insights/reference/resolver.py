"""Resolve free-text hero and item names ("Фантомка", "bkb", "ghjnjv") to records.

Matching runs on normalized text (see ``aliases.normalize``) and also on the query
re-typed in the other keyboard layout, so "фь" finds Anti-Mage ("am") and "ишт" finds
BKB; the layout switch is skipped when the query is already an alias as typed. Exact
alias hits win outright; otherwise display-name words, alias prefixes and
``difflib`` similarity produce scored candidates.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache

from .aliases import (
    EN_TO_RU,
    RU_TO_EN,
    AliasIndex,
    hero_aliases,
    item_aliases,
    normalize,
    words,
)
from .loader import Hero, Item, hero_by_id, item_by_id

EXACT_SCORE = 1.0
WORD_SCORE = 0.85
# Scores for text that only matches after switching keyboard layout are scaled by this.
LAYOUT_PENALTY = 0.95
MIN_SCORE = 0.6
FUZZY_CUTOFF = 0.75
# Candidates within this distance of the best score make the result ambiguous.
AMBIGUITY_MARGIN = 0.05
MAX_CANDIDATES = 5
MIN_PARTIAL_LENGTH = 3


@dataclass(frozen=True, slots=True)
class Candidate[T]:
    record: T
    score: float  # 0..1 confidence; 1.0 means an exact alias match
    matched: str  # the normalized alias or word that matched


@dataclass(frozen=True, slots=True)
class Resolution[T]:
    query: str
    candidates: tuple[Candidate[T], ...]  # best first

    @property
    def found(self) -> bool:
        return bool(self.candidates)

    @property
    def ambiguous(self) -> bool:
        if len(self.candidates) < 2:
            return False
        return self.candidates[1].score >= self.candidates[0].score - AMBIGUITY_MARGIN

    @property
    def best(self) -> T | None:
        """The single confident match, or None when nothing or several things match."""
        if not self.candidates or self.ambiguous:
            return None
        return self.candidates[0].record


def _variants(query: str) -> list[tuple[str, float]]:
    """Normalized query forms with the factor applied to their scores."""
    lowered = query.casefold()
    seen: dict[str, float] = {}
    for text, factor in (
        (lowered, 1.0),
        (lowered.translate(EN_TO_RU), LAYOUT_PENALTY),
        (lowered.translate(RU_TO_EN), LAYOUT_PENALTY),
    ):
        key = normalize(text)
        if key and key not in seen:
            seen[key] = factor
    return list(seen.items())


def _word_index(names: Iterable[tuple[int, str]]) -> dict[str, frozenset[int]]:
    index: dict[str, set[int]] = {}
    seen: set[str] = set()
    for record_id, name in names:
        # Leveled items repeat a display name; only the first (lowest id) one is indexed.
        if name in seen:
            continue
        seen.add(name)
        for word in words(name):
            if len(word) >= MIN_PARTIAL_LENGTH:
                index.setdefault(word, set()).add(record_id)
    return {word: frozenset(ids) for word, ids in index.items()}


def _resolve[T](
    query: str,
    aliases: AliasIndex,
    word_index: Mapping[str, frozenset[int]],
    lookup: Callable[[int], T],
) -> Resolution[T]:
    scored: dict[int, tuple[float, str]] = {}

    def offer(ids: Iterable[int], score: float, matched: str) -> None:
        for record_id in ids:
            if score > scored.get(record_id, (0.0, ""))[0]:
                scored[record_id] = (score, matched)

    variants = _variants(query)
    for key, factor in variants:
        if key in aliases:
            offer(aliases[key], EXACT_SCORE * factor, key)
            # An alias as typed is final: "ам" is Anti-Mage, not "fv" in the other layout.
            if factor == 1.0:
                break
    if not scored:
        keys = list(aliases)
        for key, factor in variants:
            if len(key) < MIN_PARTIAL_LENGTH:
                continue
            if key in word_index:
                offer(word_index[key], WORD_SCORE * factor, key)
            for alias in keys:
                if alias.startswith(key):
                    offer(aliases[alias], (0.7 + 0.2 * len(key) / len(alias)) * factor, alias)
            for alias in difflib.get_close_matches(key, keys, n=10, cutoff=FUZZY_CUTOFF):
                ratio = difflib.SequenceMatcher(None, key, alias).ratio()
                offer(aliases[alias], ratio * 0.9 * factor, alias)

    ranked = sorted(
        (
            Candidate(lookup(record_id), round(score, 3), matched)
            for record_id, (score, matched) in scored.items()
            if score >= MIN_SCORE
        ),
        key=lambda c: -c.score,
    )
    return Resolution(query, tuple(ranked[:MAX_CANDIDATES]))


@cache
def _hero_words() -> dict[str, frozenset[int]]:
    return _word_index((hero.id, hero.display_name) for hero in hero_by_id().values())


@cache
def _item_words() -> dict[str, frozenset[int]]:
    return _word_index(
        (item.id, item.display_name)
        for item in item_by_id().values()
        if item.display_name and not item.is_recipe
    )


def resolve_hero(query: str) -> Resolution[Hero]:
    """Resolve a hero name, short form or slang (English or Russian)."""
    return _resolve(query, hero_aliases(), _hero_words(), hero_by_id().__getitem__)


def resolve_item(query: str) -> Resolution[Item]:
    """Resolve an item name, short form or slang (English or Russian)."""
    return _resolve(query, item_aliases(), _item_words(), item_by_id().__getitem__)

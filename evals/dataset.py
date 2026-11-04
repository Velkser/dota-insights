"""Load the eval questions from ``dataset.toml``.

Each ``[[case]]`` names the tools that can answer it, what the key arguments must look like and
which checks the final answer must pass. ``scoring`` documents the matcher and check vocabulary.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET = Path(__file__).with_name("dataset.toml")
DEFAULT_ALLOWED_STATUS = ("ok", "insufficient_data")


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    lang: str
    question: str
    expected_tools: tuple[str, ...]
    # Argument path ("hero", "filters.rank_bracket") -> matcher spec; see scoring.match_arg.
    args: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Answer checks; see scoring.CHECKS.
    checks: tuple[dict[str, Any], ...] = ()
    # Envelope statuses that count as the tool working. "error" never does.
    allowed_status: tuple[str, ...] = DEFAULT_ALLOWED_STATUS
    forbidden_tools: tuple[str, ...] = ()
    # Question ID in docs/research/user-questions.md, when the case comes from there.
    source: str | None = None
    notes: str | None = None


def _case(raw: dict[str, Any]) -> Case:
    return Case(
        id=raw["id"],
        category=raw["category"],
        lang=raw["lang"],
        question=raw["question"],
        expected_tools=tuple(raw["expected_tools"]),
        args=dict(raw.get("args", {})),
        checks=tuple(raw.get("checks", ())),
        allowed_status=tuple(raw.get("allowed_status", DEFAULT_ALLOWED_STATUS)),
        forbidden_tools=tuple(raw.get("forbidden_tools", ())),
        source=raw.get("source"),
        notes=raw.get("notes"),
    )


def load_cases(path: Path = DATASET) -> list[Case]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return [_case(raw) for raw in data["case"]]


def select(
    cases: list[Case],
    *,
    ids: list[str] | None = None,
    categories: list[str] | None = None,
    lang: str | None = None,
    limit: int | None = None,
) -> list[Case]:
    """Filter cases by id, category and language, keeping dataset order."""
    chosen = [
        c
        for c in cases
        if (not ids or c.id in ids)
        and (not categories or c.category in categories)
        and (not lang or c.lang == lang)
    ]
    return chosen[:limit] if limit else chosen

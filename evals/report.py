"""Aggregate case scores into a summary and write it as JSON and Markdown."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.dataset import Case
from evals.scoring import CaseScore
from evals.transcript import Transcript

DIMENSIONS = ("tool", "args", "envelope", "answer")
# USD per million input / output tokens, for the cost estimate only.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}
CACHE_READ_SHARE = 0.05  # cache reads cost 5% of the input price on the models above
WORST = 10


def _rate(scores: list[CaseScore], dim: str | None = None) -> float:
    if not scores:
        return 0.0
    hits = sum(s.passed if dim is None else getattr(s, dim).passed for s in scores)
    return hits / len(scores)


def _group(scores: list[CaseScore], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        groups[getattr(s, key)].append(s)
    return {
        name: {
            "n": len(group),
            "pass_rate": _rate(group),
            **{f"{d}_rate": _rate(group, d) for d in DIMENSIONS},
        }
        for name, group in sorted(groups.items())
    }


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def cost_usd(model: str, transcripts: Iterable[Transcript]) -> float | None:
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    total = 0.0
    for t in transcripts:
        total += t.input_tokens * price_in + t.output_tokens * price_out
        total += t.cache_read_tokens * price_in * CACHE_READ_SHARE
    return total / 1_000_000


def summarize(
    cases: list[Case],
    transcripts: list[Transcript],
    scores: list[CaseScore],
    *,
    target: str,
    model: str,
) -> dict[str, Any]:
    by_case = {c.id: c for c in cases}
    latencies = [t.latency_s for t in transcripts]
    confusion: Counter[str] = Counter()
    for s, t in zip(scores, transcripts, strict=True):
        if not s.tool.passed and not t.error:
            chosen = ",".join(dict.fromkeys(c.name for c in t.calls)) or "(no tool)"
            expected = "|".join(by_case[s.case_id].expected_tools)
            confusion[f"{expected} -> {chosen}"] += 1
    statuses = Counter(
        c.status or ("call_error" if c.is_error else "none") for t in transcripts for c in t.calls
    )
    failed_checks = Counter(c.name for s in scores for c in s.checks if not c.passed)
    worst = sorted((s for s in scores if not s.passed), key=lambda s: (s.score, s.case_id))[:WORST]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "target": target,
        "model": model,
        "cases": len(scores),
        "pass_rate": _rate(scores),
        **{f"{d}_rate": _rate(scores, d) for d in DIMENSIONS},
        "by_category": _group(scores, "category"),
        "by_lang": _group(scores, "lang"),
        "latency_s": {
            "p50": statistics.median(latencies) if latencies else 0.0,
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies, default=0.0),
        },
        "api_calls": sum(t.api_calls for t in transcripts),
        "tool_calls": sum(len(t.calls) for t in transcripts),
        "tool_statuses": dict(statuses),
        "turn_cap_hits": sum(t.hit_turn_cap for t in transcripts),
        "run_errors": {t.case_id: t.error for t in transcripts if t.error},
        "tokens": {
            "input": sum(t.input_tokens for t in transcripts),
            "cache_read": sum(t.cache_read_tokens for t in transcripts),
            "output": sum(t.output_tokens for t in transcripts),
        },
        "estimated_cost_usd": cost_usd(model, transcripts),
        "tool_confusion": dict(confusion.most_common()),
        "failed_checks": dict(failed_checks.most_common()),
        "worst": [
            {
                "case_id": s.case_id,
                "question": by_case[s.case_id].question,
                "score": s.score,
                "failed": {d: getattr(s, d).detail for d in DIMENSIONS if not getattr(s, d).passed},
            }
            for s in worst
        ],
    }


def _pct(value: float) -> str:
    return f"{value:.0%}"


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def to_markdown(
    summary: dict[str, Any], scores: list[CaseScore], transcripts: list[Transcript]
) -> str:
    s = summary
    cost = s["estimated_cost_usd"]
    lines = [
        f"# Eval report: {s['model']} on {s['target']}",
        "",
        f"Generated {s['generated_at']}. {s['cases']} questions.",
        "",
        "| Pass | Tool | Args | Envelope | Answer |",
        "| --- | --- | --- | --- | --- |",
        "| "
        + " | ".join(_pct(s[k]) for k in ("pass_rate", *(f"{d}_rate" for d in DIMENSIONS)))
        + " |",
        "",
        f"Latency per question: p50 {s['latency_s']['p50']:.1f}s, "
        f"p95 {s['latency_s']['p95']:.1f}s, "
        f"max {s['latency_s']['max']:.1f}s. {s['api_calls']} model requests, "
        f"{s['tool_calls']} tool calls, {s['turn_cap_hits']} hit the turn cap. Tokens: "
        f"{s['tokens']['input']:,} in, {s['tokens']['cache_read']:,} cache read, "
        f"{s['tokens']['output']:,} out" + (f" (about ${cost:.2f})." if cost is not None else "."),
        "",
        f"Tool statuses: {', '.join(f'{k} {v}' for k, v in s['tool_statuses'].items()) or 'none'}.",
        "",
    ]
    for title, key in (("By category", "by_category"), ("By language", "by_lang")):
        lines += [
            f"## {title}",
            "",
            "| Group | n | Pass | Tool | Args | Envelope | Answer |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for name, g in s[key].items():
            rates = " | ".join(_pct(g[f"{d}_rate"]) for d in DIMENSIONS)
            lines.append(f"| {name} | {g['n']} | {_pct(g['pass_rate'])} | {rates} |")
        lines.append("")
    if s["tool_confusion"]:
        lines += ["## Wrong tool choices", "", "| Expected -> chosen | Count |", "| --- | --- |"]
        lines += [f"| {_cell(k)} | {v} |" for k, v in s["tool_confusion"].items()]
        lines.append("")
    if s["failed_checks"]:
        lines += ["## Failed answer checks", ""]
        lines += [f"- `{k}`: {v}" for k, v in s["failed_checks"].items()]
        lines.append("")
    if s["worst"]:
        lines += ["## Worst failures", ""]
        for w in s["worst"]:
            lines.append(f"- **{w['case_id']}** ({w['score']:.2f}): {_cell(w['question'])}")
            lines += [f"  - {d}: {_cell(detail, 300)}" for d, detail in w["failed"].items()]
        lines.append("")
    if s["run_errors"]:
        lines += ["## Run errors", ""]
        lines += [f"- {k}: {_cell(v, 300)}" for k, v in s["run_errors"].items()]
        lines.append("")
    lines += [
        "## All cases",
        "",
        "| Case | Pass | Tools called | Failed | Latency |",
        "| --- | --- | --- | --- | --- |",
    ]
    for score, t in zip(scores, transcripts, strict=True):
        called = ", ".join(c.name for c in t.calls) or "—"
        failed = ", ".join(d for d in DIMENSIONS if not getattr(score, d).passed) or "—"
        mark = "yes" if score.passed else "no"
        lines.append(
            f"| {score.case_id} | {mark} | {_cell(called)} | {failed} | {t.latency_s:.1f}s |"
        )
    return "\n".join(lines) + "\n"


def write_report(
    out_dir: Path,
    summary: dict[str, Any],
    scores: list[CaseScore],
    transcripts: list[Transcript],
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    """Write ``<stem>.json`` (summary, scores, transcripts) and ``<stem>.md``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    payload = {
        "summary": summary,
        "scores": [s.to_dict() for s in scores],
        "transcripts": [t.to_dict() for t in transcripts],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    md_path.write_text(to_markdown(summary, scores, transcripts), encoding="utf-8")
    return json_path, md_path

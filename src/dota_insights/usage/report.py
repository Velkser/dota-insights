"""The usage report behind ``dota-insights usage report`` and ``GET /admin/usage``.

Over the last ``days`` days (UTC): calls and unique clients per day and ISO week, per-tool
success rates and p50/p95 latency, top tools, the most frequent ``insufficient_data`` and
``error`` calls (by tool and argument shape), client names, wrong-answer reports with the call
they refer to, and feature requests for the monthly review.

Rows are aggregated in Python, so the report runs on PostgreSQL and SQLite alike; at the
retention of 90 days the table stays small enough for that.
"""

import json
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dota_insights.collector.db import as_utc
from dota_insights.db.models import UsageEvent, UsageFeedback
from dota_insights.usage.events import week_of

MAX_DAYS = 365


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100) of ``values``; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return float(ordered[rank - 1])


def _day(moment: datetime) -> str:
    return moment.date().isoformat()


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _period_rows(rows: list[Any], key) -> list[dict[str, Any]]:
    calls: Counter[str] = Counter()
    clients: dict[str, set[str]] = defaultdict(set)
    failed: Counter[str] = Counter()
    insufficient: Counter[str] = Counter()
    for row in rows:
        period = key(as_utc(row.ts))
        calls[period] += 1
        if row.client_hash:
            clients[period].add(row.client_hash)
        if row.status == "error":
            failed[period] += 1
        elif row.status == "insufficient_data":
            insufficient[period] += 1
    return [
        {
            "period": period,
            "calls": calls[period],
            "unique_clients": len(clients[period]),
            "errors": failed[period],
            "insufficient_data": insufficient[period],
        }
        for period in sorted(calls)
    ]


def _tool_rows(rows: list[Any]) -> list[dict[str, Any]]:
    by_tool: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_tool[row.tool].append(row)
    out = []
    for tool, calls in by_tool.items():
        status = Counter(r.status for r in calls)
        latencies = [r.latency_ms for r in calls]
        out.append(
            {
                "tool": tool,
                "calls": len(calls),
                "ok": status["ok"],
                "insufficient_data": status["insufficient_data"],
                "error": status["error"],
                "success_rate": _rate(status["ok"], len(calls)),
                "error_rate": _rate(status["error"], len(calls)),
                "unique_clients": len({r.client_hash for r in calls if r.client_hash}),
                "p50_ms": percentile(latencies, 50),
                "p95_ms": percentile(latencies, 95),
            }
        )
    out.sort(key=lambda r: (-r["calls"], r["tool"]))
    return out


def _unanswered(session: Session, since: datetime, top: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            UsageEvent.tool,
            UsageEvent.status,
            UsageEvent.error_kind,
            UsageEvent.arguments,
            UsageEvent.message,
        ).where(UsageEvent.ts >= since, UsageEvent.status != "ok")
    ).all()
    groups: Counter[tuple[str, str, str]] = Counter()
    sample: dict[tuple[str, str, str], Any] = {}
    for row in rows:
        if row.error_kind == "rate_limited":
            continue
        key = (row.tool, row.status, json.dumps(row.arguments, sort_keys=True))
        groups[key] += 1
        sample[key] = row
    return [
        {
            "tool": tool,
            "status": status,
            "arguments": json.loads(args),
            "count": count,
            "error_kind": sample[(tool, status, args)].error_kind,
            "message": sample[(tool, status, args)].message,
        }
        for (tool, status, args), count in groups.most_common(top)
    ]


def _feedback(session: Session, since: datetime) -> tuple[list[dict], list[dict]]:
    rows = session.scalars(
        select(UsageFeedback).where(UsageFeedback.ts >= since).order_by(UsageFeedback.ts.desc())
    ).all()
    linked_ids = [r.call_id for r in rows if r.call_id]
    linked = {
        e.event_id: e
        for e in session.scalars(select(UsageEvent).where(UsageEvent.event_id.in_(linked_ids)))
    }
    wrong, requests = [], []
    for row in rows:
        item: dict[str, Any] = {
            "ts": as_utc(row.ts).isoformat(),
            "tool": row.tool,
            "text": row.text,
            "client_name": row.client_name,
        }
        if row.kind == "wrong_answer":
            call = linked.get(row.call_id) if row.call_id else None
            item["expected"] = row.expected
            item["call"] = (
                {
                    "event_id": call.event_id,
                    "ts": as_utc(call.ts).isoformat(),
                    "arguments": call.arguments,
                    "status": call.status,
                    "message": call.message,
                }
                if call is not None
                else None
            )
            wrong.append(item)
        else:
            item["why_no_tool_fits"] = row.expected
            requests.append(item)
    return wrong, requests


def build_report(session: Session, now: datetime, days: int = 30, top: int = 20) -> dict[str, Any]:
    """The usage report for the ``days`` days up to ``now``."""
    days = max(1, min(MAX_DAYS, days))
    since = now - timedelta(days=days)
    rows = session.execute(
        select(
            UsageEvent.ts,
            UsageEvent.tool,
            UsageEvent.status,
            UsageEvent.client_hash,
            UsageEvent.latency_ms,
            UsageEvent.client_name,
            UsageEvent.channel,
        ).where(UsageEvent.ts >= since)
    ).all()
    status = Counter(r.status for r in rows)
    tools = _tool_rows(rows)
    wrong, requests = _feedback(session, since)
    latencies = [r.latency_ms for r in rows]
    return {
        "window": {"from": since.isoformat(), "to": now.isoformat(), "days": days},
        "totals": {
            "calls": len(rows),
            "unique_clients": len({r.client_hash for r in rows if r.client_hash}),
            "ok": status["ok"],
            "insufficient_data": status["insufficient_data"],
            "error": status["error"],
            "success_rate": _rate(status["ok"], len(rows)),
            "p50_ms": percentile(latencies, 50),
            "p95_ms": percentile(latencies, 95),
            "wrong_answer_reports": len(wrong),
            "feature_requests": len(requests),
        },
        # Calls per channel: "mcp" (MCP clients) and "api" (the /api/v1 REST routes).
        "channels": dict(Counter(r.channel or "mcp" for r in rows).most_common()),
        "per_day": _period_rows(rows, _day),
        "per_week": _period_rows(rows, week_of),
        "top_tools": [t["tool"] for t in tools[:top]],
        "tools": tools,
        "clients": [
            {"client": name or "unknown", "calls": n}
            for name, n in Counter(r.client_name for r in rows).most_common(top)
        ],
        "top_unanswered": _unanswered(session, since, top),
        "wrong_answers": wrong,
        "feature_requests": requests,
        "feedback_by_tool": dict(Counter(w["tool"] for w in wrong if w["tool"]).most_common()),
    }


def format_report(report: dict[str, Any]) -> str:
    """A plain-text rendering for the CLI."""
    t = report["totals"]
    w = report["window"]
    lines = [
        f"Usage {w['from'][:10]} .. {w['to'][:10]} ({w['days']} days)",
        f"calls {t['calls']}  unique clients {t['unique_clients']}  "
        f"ok {t['ok']}  insufficient {t['insufficient_data']}  error {t['error']}  "
        f"success {_pct(t['success_rate'])}  p50 {_ms(t['p50_ms'])}  p95 {_ms(t['p95_ms'])}",
        "channels " + "  ".join(f"{k} {v}" for k, v in report.get("channels", {}).items()),
        "",
        "Per week:",
        *(
            f"  {r['period']}  calls {r['calls']:6}  clients {r['unique_clients']:5}"
            for r in report["per_week"]
        ),
        "",
        "Per day:",
        *(
            f"  {r['period']}  calls {r['calls']:6}  clients {r['unique_clients']:5}  "
            f"errors {r['errors']:4}  insufficient {r['insufficient_data']:4}"
            for r in report["per_day"]
        ),
        "",
        "Tools (calls, success, error, p50, p95):",
        *(
            f"  {r['tool']:28} {r['calls']:6}  {_pct(r['success_rate']):>6}  "
            f"{_pct(r['error_rate']):>6}  {_ms(r['p50_ms']):>8}  {_ms(r['p95_ms']):>8}"
            for r in report["tools"]
        ),
        "",
        "Clients:",
        *(f"  {r['client']}: {r['calls']}" for r in report["clients"]),
        "",
        "Top insufficient_data / error calls:",
        *(
            f"  {r['count']:4}x {r['tool']} [{r['status']}] {json.dumps(r['arguments'])}"
            + (f" — {r['message']}" if r["message"] else "")
            for r in report["top_unanswered"]
        ),
        "",
        f"Wrong-answer reports ({len(report['wrong_answers'])}):",
        *(
            f"  {r['ts'][:16]} {r['tool']}: {r['text']}"
            + (f" (expected: {r['expected']})" if r["expected"] else "")
            for r in report["wrong_answers"]
        ),
        "",
        f"Feature requests ({len(report['feature_requests'])}):",
        *(f"  {r['ts'][:16]} {r['text']}" for r in report["feature_requests"]),
    ]
    if report.get("queued") is not None:
        lines.append(f"\nEvents waiting in the Redis queue (not counted yet): {report['queued']}")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} ms"

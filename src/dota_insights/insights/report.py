"""Write a report folder: post drafts (Markdown), charts (PNG) and the numbers (JSON).

Layout of ``<out>/<last day of the week>/``::

    README.md               index for the moderator: period, source, thresholds, files
    weekly-meta.md          the Weekly Meta Report post (all ranks)
    weekly-meta-by-rank.md  per-bracket tables behind it (reference, not a post)
    weekly-winrate.png      win-rate risers and fallers, all ranks
    weekly-pickrate.png     pick-rate risers and fallers, all ranks
    insights/NN-*.md        one Insight Generator draft per anomaly
    report.json             every eligible hero change, for checking the numbers

A chart that cannot be rendered is listed in README.md and skipped; the texts are still written.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from dota_insights.analytics.charts import ChartInputError, bar_spec, render_png
from dota_insights.insights import texts
from dota_insights.insights.data import ALL_RANKS
from dota_insights.insights.meta import Anomaly, MetaReport, Metric, Movers

CHART_TITLES = {
    "winrate": "Win rate: изменение за неделю",
    "pickrate": "Пик-рейт: изменение за неделю",
}
CHANGE_FIELD = "изменение"
CHANGE_DOMAIN = ("рост", "падение")


@dataclass(slots=True)
class Written:
    folder: Path
    files: list[str] = field(default_factory=list)
    chart_errors: list[str] = field(default_factory=list)


def chart_spec(movers: Movers, report: MetaReport, metric: Metric) -> dict[str, Any] | None:
    up = getattr(movers, f"{metric}_up")
    down = getattr(movers, f"{metric}_down")
    if not up and not down:
        return None
    rows = [
        {
            "x": texts.hero_name(c.hero_id),
            "y": round(c.delta(metric) * 100, 2),
            CHANGE_FIELD: CHANGE_DOMAIN[0] if c.delta(metric) > 0 else CHANGE_DOMAIN[1],
        }
        for c in sorted(up + down, key=lambda c: -c.delta(metric))
    ]
    subtitle = (
        f"{texts.bracket_name(movers.bracket).capitalize()} · {texts.patch_text(report)} · "
        f"{texts.sample_text(report.after, movers.bracket)} · герои с ≥ {report.min_games} игр"
    )
    return bar_spec(
        rows,
        x_title="Герой",
        y_title="Изменение к прошлой неделе, п.п.",
        title=CHART_TITLES[metric],
        subtitle=subtitle,
        color_field=CHANGE_FIELD,
        color_domain=CHANGE_DOMAIN,
        value_format="+.1f",
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "hero"


def anomaly_filename(index: int, anomaly: Anomaly) -> str:
    c = anomaly.change
    rank = "all" if c.bracket == ALL_RANKS else f"rank{c.bracket}"
    return f"{index:02d}-{_slug(texts.hero_name_en(c.hero_id))}-{anomaly.metric}-{rank}.md"


def _change_json(c) -> dict[str, Any]:
    return {
        "bracket": c.bracket,
        "hero_id": c.hero_id,
        "hero": texts.hero_name(c.hero_id),
        "games_before": c.games_before,
        "wins_before": c.wins_before,
        "matches_before": c.matches_before,
        "games_after": c.games_after,
        "wins_after": c.wins_after,
        "matches_after": c.matches_after,
        "winrate_delta": round(c.delta("winrate"), 5),
        "pickrate_delta": round(c.delta("pickrate"), 5),
    }


def report_json(report: MetaReport) -> dict[str, Any]:
    def week(w):
        return {
            "start": w.start.isoformat(),
            "end_exclusive": w.end.isoformat(),
            "source": w.source,
            "snapshot_day": w.snapshot_day.isoformat() if w.snapshot_day else None,
            "matches": {str(b): n for b, n in sorted(w.matches.items())},
            "patches": dict(w.patches),
        }

    return {
        "before": week(report.before),
        "after": week(report.after),
        "patch": report.patch,
        "min_games": report.min_games,
        "anomalies": [
            {
                **_change_json(a.change),
                "metric": a.metric,
                "z": round(a.z, 3),
                "p_value": a.p_value,
                "q_value": a.q_value,
            }
            for a in report.anomalies
        ],
        "changes": [_change_json(c) for c in report.changes],
    }


def _readme(report: MetaReport, written: Written, settings: dict[str, Any]) -> str:
    after, before = report.after, report.before
    lines = [
        f"# Черновики Dota Insights · {texts.fmt_period(after.start, after.end)}",
        "",
        "Сгенерировано автоматически для ручной модерации; ничего не опубликовано.",
        "",
        f"- Неделя: {texts.fmt_period(after.start, after.end)}, {texts.sample_text(after)}; "
        f"предыдущая: {texts.fmt_period(before.start, before.end)}, {texts.sample_text(before)}.",
        f"- {texts.patch_text(report).capitalize()}.",
        f"- Источник: {texts.SOURCE_NOTES[after.source]}"
        + (
            f" (снимки {after.snapshot_day} и {before.snapshot_day})"
            if after.source == "opendota"
            else ""
        )
        + ".",
        f"- Порог выборки: ≥ {report.min_games} игр с героем в каждой неделе; "
        f"героев × рангов в сравнении: {len(report.changes)}.",
        f"- Аномалии: q ≤ {texts.fmt_num(settings['alpha'], 2)} ({texts.ZTEST_NOTE}), "
        f"изменение win rate от {texts.fmt_pp(settings['min_wr_delta'])} "
        f"или пик-рейта от {texts.fmt_pp(settings['min_pr_delta'])}; "
        f"найдено {len(report.anomalies)}.",
        "",
        "## Файлы",
        "",
        *(f"- `{name}`" for name in written.files),
    ]
    if written.chart_errors:
        lines += ["", "## Графики не построены", "", *(f"- {e}" for e in written.chart_errors)]
    lines += [
        "",
        "## Перед публикацией",
        "",
        "- Проверьте, что выборка и патч в тексте совпадают с `report.json`.",
        "- Изменение за неделю — не причина: сверьтесь с патчноутом, прежде чем объяснять его.",
        "- Пост и график публикуются вместе; подпись к фото в Telegram — до 1024 символов.",
    ]
    return "\n".join(lines) + "\n"


def write_report(
    report: MetaReport,
    out: Path,
    *,
    settings: dict[str, Any],
    charts: bool = True,
) -> Written:
    folder = out / (report.after.end - timedelta(days=1)).isoformat()
    (folder / "insights").mkdir(parents=True, exist_ok=True)
    written = Written(folder)

    def put(name: str, content: str | bytes) -> None:
        path = folder / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        written.files.append(name)

    put("weekly-meta.md", texts.weekly_post(report) + "\n")
    put("weekly-meta-by-rank.md", texts.by_rank(report))
    overall = next((m for m in report.movers if m.bracket == ALL_RANKS), None)
    if charts and overall is not None:
        for metric in ("winrate", "pickrate"):
            name = f"weekly-{metric}.png"
            try:
                spec = chart_spec(overall, report, metric)
                if spec is not None:
                    put(name, render_png(spec))
            except (ChartInputError, RuntimeError, ValueError) as exc:
                written.chart_errors.append(f"`{name}`: {exc}")
    for index, anomaly in enumerate(report.anomalies, start=1):
        name = f"insights/{anomaly_filename(index, anomaly)}"
        put(name, texts.anomaly_post(anomaly, report) + "\n")
    put("report.json", json.dumps(report_json(report), ensure_ascii=False, indent=2) + "\n")
    written.files.insert(0, "README.md")
    (folder / "README.md").write_text(_readme(report, written, settings), encoding="utf-8")
    return written

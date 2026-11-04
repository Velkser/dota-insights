"""Russian text templates for the rubrics: short, factual, always with the sample and the patch.

Numbers use Russian conventions: decimal comma, non-breaking space between thousands, a real
minus sign, and win/pick-rate changes in percentage points ("п.п.").
"""

from datetime import date, timedelta

from dota_insights.insights.data import ALL_RANKS, WeekCounts
from dota_insights.insights.meta import Anomaly, HeroChange, MetaReport, Metric, Movers
from dota_insights.reference.loader import hero_by_id, hero_name_ru

NBSP = " "
MINUS = "−"
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
# The Russian client's medal names; Immortal ("Титан") is folded into Divine (see insights.data).
BRACKET_NAMES = {
    ALL_RANKS: "все ранги",
    1: "Рекрут",
    2: "Страж",
    3: "Рыцарь",
    4: "Герой",
    5: "Легенда",
    6: "Властелин",
    7: "Божество и Титан",
}
METRIC_NAMES = {"winrate": "Win rate", "pickrate": "Пик-рейт"}
SOURCE_NOTES = {
    "matches": "рейтинговые матчи, собранные Dota Insights",
    "opendota": "снимок OpenDota /heroStats (окно OpenDota, не ровно 7 дней)",
}
ZTEST_NOTE = "двусторонний z-тест долей, поправка Бенджамини–Хохберга"


def hero_name_en(hero_id: int) -> str:
    hero = hero_by_id().get(hero_id)
    return hero.display_name if hero else f"Hero #{hero_id}"


def hero_name(hero_id: int) -> str:
    """The hero's usual Russian name for the posts, falling back to the English one."""
    hero = hero_by_id().get(hero_id)
    if hero is None:
        return f"Hero #{hero_id}"
    return hero_name_ru(hero) or hero.display_name


def fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", NBSP)


def fmt_num(value: float, digits: int = 1) -> str:
    text = f"{abs(value):.{digits}f}".replace(".", ",")
    return f"{MINUS}{text}" if value < 0 and text.strip("0,") else text


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """Russian noun form for ``n``: ("игра", "игры", "игр") -> 1 игра, 3 игры, 11 игр."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


GAMES = ("игра", "игры", "игр")
MATCHES = ("матч", "матча", "матчей")


def games_text(n: int) -> str:
    return f"{fmt_int(n)} {plural(n, GAMES)}"


def fmt_pct(rate: float) -> str:
    return f"{fmt_num(rate * 100)}%"


def fmt_pp(delta: float) -> str:
    text = fmt_num(delta * 100)
    sign = "+" if delta > 0 and text.strip("0,") else ""
    return f"{sign}{text}{NBSP}п.п."


def fmt_q(q: float) -> str:
    """A q-value with its relation sign: "< 0,001" or "= 0,012"."""
    return "< 0,001" if q < 0.001 else f"= {fmt_num(q, 3)}"


def fmt_day(day: date, with_year: bool = True) -> str:
    text = f"{day.day} {MONTHS[day.month - 1]}"
    return f"{text} {day.year}" if with_year else text


def fmt_period(start: date, end: date) -> str:
    """``[start, end)`` as an inclusive range of days, e.g. "26 октября – 1 ноября 2026"."""
    last = end - timedelta(days=1)
    if (start.year, start.month) == (last.year, last.month):
        return f"{start.day}–{fmt_day(last)}"
    return f"{fmt_day(start, with_year=start.year != last.year)} – {fmt_day(last)}"


def bracket_name(bracket: int) -> str:
    return BRACKET_NAMES[bracket]


def patch_text(report: MetaReport) -> str:
    patch = report.patch or "неизвестен"
    if report.patch_changed:
        patches = ", ".join(sorted(set(report.before.patches) | set(report.after.patches)))
        return f"патч {patch} (в сравнении участвуют патчи {patches})"
    return f"патч {patch}"


def sample_text(week: WeekCounts, bracket: int = ALL_RANKS) -> str:
    n = week.matches[bracket]
    return f"{fmt_int(n)} {plural(n, MATCHES)}"


def _line(change: HeroChange, metric: Metric) -> str:
    return (
        f"• {hero_name(change.hero_id)} — {fmt_pct(change.after(metric))} "
        f"({fmt_pp(change.delta(metric))}), {games_text(change.games_after)}"
    )


SECTIONS: tuple[tuple[str, Metric, str], ...] = (
    ("📈 Win rate растёт", "winrate", "winrate_up"),
    ("📉 Win rate падает", "winrate", "winrate_down"),
    ("🔥 Пикают чаще", "pickrate", "pickrate_up"),
    ("🧊 Пикают реже", "pickrate", "pickrate_down"),
)


def _sections(movers: Movers, top: int) -> list[str]:
    lines = []
    for title, metric, attr in SECTIONS:
        heroes = getattr(movers, attr)[:top]
        if heroes:
            lines += ["", f"**{title}**", *(_line(c, metric) for c in heroes)]
    return lines


def weekly_post(report: MetaReport, top: int = 3) -> str:
    """The Telegram post of the Weekly Meta Report: all ranks, ``top`` heroes per section."""
    after, before = report.after, report.before
    head = [
        f"**Weekly Meta Report · {fmt_period(after.start, after.end)}**",
        f"{patch_text(report).capitalize()} · все ранги · {sample_text(after)} "
        f"(неделей ранее {fmt_int(before.total_matches)})",
    ]
    overall = next((m for m in report.movers if m.bracket == ALL_RANKS), None)
    if overall is None:
        short = (
            f"Недостаточно данных: ни у одного героя нет {games_text(report.min_games)} "
            "в каждой из двух недель."
        )
        return "\n".join([*head, "", short])
    body = _sections(overall, top)
    foot = [
        "",
        f"Выборка: {SOURCE_NOTES[after.source]}; герои с ≥{NBSP}{fmt_int(report.min_games)} игр "
        "в каждой из двух недель. В скобках — изменение к предыдущей неделе.",
    ]
    return "\n".join(head + body + foot)


def _table(movers: Movers, metric: Metric, attr: str) -> list[str]:
    rows = getattr(movers, attr)
    if not rows:
        return ["_нет_"]
    lines = [
        "| Герой | Было | Стало | Изменение | Игр (было → стало) | z |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for c in rows:
        z, _ = c.ztest(metric)
        lines.append(
            f"| {hero_name(c.hero_id)} | {fmt_pct(c.before(metric))} | {fmt_pct(c.after(metric))} "
            f"| {fmt_pp(c.delta(metric))} | {fmt_int(c.games_before)} → {fmt_int(c.games_after)} "
            f"| {fmt_num(z)} |"
        )
    return lines


def by_rank(report: MetaReport) -> str:
    """Per-bracket tables for the moderator (not a post): every section, every bracket."""
    after = report.after
    lines = [
        f"# Weekly Meta Report по рангам · {fmt_period(after.start, after.end)}",
        "",
        f"{patch_text(report).capitalize()}. Источник: {SOURCE_NOTES[after.source]}. "
        f"Порог: ≥{NBSP}{fmt_int(report.min_games)} игр за каждую неделю. "
        "z > 0 — доля выросла (двусторонний z-тест долей, без поправки на множественность).",
    ]
    for movers in report.movers:
        lines += [
            "",
            f"## {bracket_name(movers.bracket).capitalize()}",
            "",
            f"Матчей: {fmt_int(movers.matches_before)} → {fmt_int(movers.matches_after)}; "
            f"героев с достаточной выборкой: {movers.heroes}.",
        ]
        for title, metric, attr in SECTIONS:
            lines += ["", f"### {title}", "", *_table(movers, metric, attr)]
    return "\n".join(lines) + "\n"


def anomaly_post(anomaly: Anomaly, report: MetaReport) -> str:
    """One short insight draft about a statistically notable change."""
    c, metric = anomaly.change, anomaly.metric
    hero, delta = hero_name(c.hero_id), c.delta(metric)
    rank = "все ранги" if c.bracket == ALL_RANKS else f"ранг {bracket_name(c.bracket)}"
    values = f"с {fmt_pct(c.before(metric))} до {fmt_pct(c.after(metric))} ({fmt_pp(delta)})"
    moved = "вырос" if delta > 0 else "снизился"
    if metric == "winrate":
        lead = f"{hero}: win rate за неделю {moved} {values}."
    else:
        lead = (
            f"{hero} пикают {'чаще' if delta > 0 else 'реже'}: пик-рейт за неделю {moved} {values}."
        )
    sample = (
        f"{rank.capitalize()}, {patch_text(report)}. Игр с героем: "
        f"{fmt_int(c.games_before)} → {fmt_int(c.games_after)}; матчей в выборке: "
        f"{fmt_int(c.matches_before)} → {fmt_int(c.matches_after)}."
    )
    test = (
        f"Изменение статистически значимо: z = {fmt_num(anomaly.z)}, "
        f"q {fmt_q(anomaly.q_value)} ({ZTEST_NOTE})."
    )
    period = fmt_period(report.after.start, report.after.end)
    return "\n".join(
        [
            f"**{METRIC_NAMES[metric]} {hero} · {period}**",
            "",
            lead,
            sample,
            test,
            "",
            "---",
            "_Для модератора: статистика не объясняет причину. Перед публикацией сверьтесь "
            "с патчноутом и билдами; источник — " + SOURCE_NOTES[report.after.source] + "._",
        ]
    )

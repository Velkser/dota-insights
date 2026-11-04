"""Chart specs (Vega-Lite) and PNG rendering for tool results.

``build_chart`` turns plain rows, or the result of another tool (gold/XP curves, tier list,
matchup matrix), into a Vega-Lite v5 spec with the data inlined. ``render_png`` renders that
same spec with vl-convert, so the picture and the spec always agree.

Colors come from a validated colorblind-safe categorical order (fixed, never cycled) and a
blue/gray/red diverging scale for signed heatmap values.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import vl_convert

ChartKind = Literal["line", "bar", "heatmap"]
Shape = Literal["rows", "gold_xp_curves", "tier_list", "matchup_matrix"]

VEGA_LITE_VERSION = "5.20"
SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
MAX_ROWS = 5000
MAX_CATEGORIES = 8  # categorical colors available before series must be cut
CATEGORICAL = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
DIVERGING = ("#e34948", "#f0efec", "#2a78d6")  # negative, zero, positive
SEQUENTIAL = ("#cde2fb", "#104281")
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
TIER_ORDER = ("S", "A", "B", "C", "D")

CONFIG = {
    "background": SURFACE,
    "font": "Liberation Sans, Helvetica, Arial, sans-serif",
    "view": {"stroke": None},
    "title": {"color": INK, "fontSize": 15, "anchor": "start", "subtitleColor": INK_SECONDARY},
    "axis": {
        "labelColor": INK_SECONDARY,
        "titleColor": INK_SECONDARY,
        "gridColor": GRID,
        "domainColor": "#c3c2b7",
        "tickColor": "#c3c2b7",
        "labelFontSize": 11,
        "titleFontSize": 12,
        "titleFontWeight": "normal",
    },
    "legend": {"labelColor": INK_SECONDARY, "titleColor": INK_SECONDARY, "orient": "bottom"},
    "line": {"strokeWidth": 2},
    "bar": {"cornerRadiusEnd": 4},
}


class ChartInputError(ValueError):
    """The data cannot be charted as requested; the message says why."""


@dataclass(slots=True)
class Chart:
    kind: ChartKind
    spec: dict[str, Any]
    rows: int
    series: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def unwrap(data: Any) -> Any:
    """Accept either a tool's full envelope (with ``result``) or the result itself."""
    if isinstance(data, Mapping) and "status" in data and "result" in data:
        if data["status"] != "ok":
            status = data["status"]
            raise ChartInputError(f"The tool result has status {status!r}; nothing to chart.")
        return data["result"]
    return data


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ChartInputError(f"{what} must be a finite number, got {value!r}.")
    return float(value)


def _base(title: str | None, subtitle: str | None, width: int, height: int) -> dict[str, Any]:
    spec: dict[str, Any] = {"$schema": SCHEMA, "width": width, "height": height, "config": CONFIG}
    if title:
        spec["title"] = {"text": title, **({"subtitle": subtitle} if subtitle else {})}
    return spec


def _color_scale(names: Sequence[str]) -> dict[str, Any]:
    return {"domain": list(names), "range": list(CATEGORICAL[: len(names)])}


def _distinct(values: Sequence[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


# Generic kinds over normalized rows


def line_spec(
    rows: list[dict[str, Any]],
    *,
    x_title: str,
    y_title: str,
    title: str | None,
    subtitle: str | None = None,
    dash_field: str | None = None,
) -> dict[str, Any]:
    """Rows: ``x`` (number), ``y`` (number), optional ``series`` and ``dash_field``."""
    series = _distinct([r["series"] for r in rows if r.get("series") is not None])
    spec = _base(title, subtitle, 640, 360)
    encoding: dict[str, Any] = {
        "x": {"field": "x", "type": "quantitative", "title": x_title},
        "y": {"field": "y", "type": "quantitative", "title": y_title},
        "tooltip": [
            {"field": "x", "type": "quantitative", "title": x_title},
            {"field": "y", "type": "quantitative", "title": y_title, "format": ",.0f"},
        ],
    }
    if series:
        encoding["color"] = {
            "field": "series",
            "type": "nominal",
            "title": None,
            "scale": _color_scale(series),
        }
        encoding["tooltip"].insert(0, {"field": "series", "type": "nominal", "title": "Series"})
        if dash_field:
            dashes = _distinct([r[dash_field] for r in rows])
            encoding["strokeDash"] = {
                "field": dash_field,
                "type": "nominal",
                "title": None,
                "scale": {"domain": dashes, "range": [[1, 0], [6, 4], [2, 2]][: len(dashes)]},
            }
            encoding["detail"] = {"field": "line", "type": "nominal"}
    spec["data"] = {"values": rows}
    spec["mark"] = {"type": "line", "interpolate": "monotone"}
    spec["encoding"] = encoding
    return spec


def bar_spec(
    rows: list[dict[str, Any]],
    *,
    x_title: str,
    y_title: str,
    title: str | None,
    subtitle: str | None = None,
    color_field: str | None = None,
    color_domain: Sequence[str] | None = None,
    with_interval: bool = False,
    value_format: str = ",.2f",
) -> dict[str, Any]:
    """Horizontal bars. Rows: ``x`` (category label), ``y`` (number), optional ``series``.

    Categories keep the order they arrive in, so sort the rows before charting.
    """
    categories = _distinct([r["x"] for r in rows])
    spec = _base(title, subtitle, 560, max(120, min(18 * len(categories), 1400)))
    y_enc = {"field": "x", "type": "nominal", "title": x_title, "sort": categories}
    x_enc: dict[str, Any] = {"field": "y", "type": "quantitative", "title": y_title}
    bar: dict[str, Any] = {
        "mark": {"type": "bar", "color": CATEGORICAL[0], "height": {"band": 0.8}},
        "encoding": {
            "y": y_enc,
            "x": x_enc,
            "tooltip": [
                {"field": "x", "type": "nominal", "title": x_title},
                {"field": "y", "type": "quantitative", "title": y_title, "format": value_format},
            ],
        },
    }
    field_name = color_field or ("series" if any(r.get("series") for r in rows) else None)
    if field_name:
        domain = list(color_domain or _distinct([r[field_name] for r in rows]))
        if len(domain) > MAX_CATEGORIES:
            raise ChartInputError(
                f"{len(domain)} color groups; at most {MAX_CATEGORIES} can be told apart."
            )
        bar["encoding"]["color"] = {
            "field": field_name,
            "type": "nominal",
            "title": field_name.replace("_", " ").capitalize(),
            "scale": _color_scale(domain),
        }
        bar["encoding"]["tooltip"].append({"field": field_name, "type": "nominal"})
        if field_name == "series":
            bar["encoding"]["yOffset"] = {"field": "series", "sort": domain}
    spec["data"] = {"values": rows}
    if with_interval:
        rule = {
            "mark": {"type": "rule", "color": INK, "strokeWidth": 1.5},
            "encoding": {
                "y": y_enc,
                "x": {"field": "low", "type": "quantitative"},
                "x2": {"field": "high"},
            },
        }
        spec["layer"] = [bar, rule]
    else:
        spec |= bar
    return spec


def heatmap_spec(
    rows: list[dict[str, Any]],
    *,
    x_title: str,
    y_title: str,
    value_title: str,
    title: str | None,
    subtitle: str | None = None,
    value_format: str = ",.2f",
) -> dict[str, Any]:
    """Rows: ``x`` and ``y`` (category labels), ``value`` (number).

    Values on both sides of zero get a diverging blue/gray/red scale centred on zero;
    one-signed values get a single-hue sequential scale.
    """
    values = [r["value"] for r in rows]
    xs, ys = _distinct([r["x"] for r in rows]), _distinct([r["y"] for r in rows])
    spec = _base(title, subtitle, max(160, 56 * len(xs)), max(120, 34 * len(ys)))
    if min(values) < 0 < max(values):
        bound = max(abs(v) for v in values)
        scale = {"domain": [-bound, 0, bound], "range": list(DIVERGING), "interpolate": "lab"}
    else:
        scale = {"range": list(SEQUENTIAL), "interpolate": "lab"}
    position = {
        "x": {
            "field": "x",
            "type": "nominal",
            "title": x_title,
            "sort": xs,
            "axis": {"labelAngle": 0, "orient": "top"},
        },
        "y": {"field": "y", "type": "nominal", "title": y_title, "sort": ys},
    }
    spec["data"] = {"values": rows}
    spec["layer"] = [
        {
            "mark": {"type": "rect", "stroke": SURFACE, "strokeWidth": 2, "cornerRadius": 2},
            "encoding": position
            | {
                "color": {
                    "field": "value",
                    "type": "quantitative",
                    "title": value_title,
                    "scale": scale,
                },
                "tooltip": [
                    {"field": "y", "type": "nominal", "title": y_title},
                    {"field": "x", "type": "nominal", "title": x_title},
                    {
                        "field": "value",
                        "type": "quantitative",
                        "title": value_title,
                        "format": value_format,
                    },
                ],
            },
        },
        {
            "mark": {"type": "text", "fontSize": 11, "color": INK},
            "encoding": position
            | {"text": {"field": "value", "type": "quantitative", "format": value_format}},
        },
    ]
    return spec


# Rows from plain data


def plain_rows(
    data: Any,
    kind: ChartKind,
    x_field: str,
    y_field: str,
    series_field: str | None,
    value_field: str,
) -> list[dict[str, Any]]:
    if not isinstance(data, Sequence) or isinstance(data, str | bytes) or not data:
        raise ChartInputError("For shape 'rows', data must be a non-empty list of objects.")
    rows = []
    for i, raw in enumerate(data):
        if not isinstance(raw, Mapping):
            raise ChartInputError(f"Row {i} is not an object.")
        missing = [f for f in (x_field, y_field) if f not in raw]
        if kind == "heatmap" and value_field not in raw:
            missing.append(value_field)
        if missing:
            raise ChartInputError(f"Row {i} lacks field(s) {', '.join(missing)}.")
        row: dict[str, Any] = {"x": raw[x_field], "y": raw[y_field]}
        if kind == "line":
            row["x"] = _number(raw[x_field], f"Row {i} {x_field}")
        if kind in ("line", "bar"):
            row["y"] = _number(raw[y_field], f"Row {i} {y_field}")
        else:
            row["x"], row["y"] = str(raw[x_field]), str(raw[y_field])
            row["value"] = _number(raw[value_field], f"Row {i} {value_field}")
        if kind == "bar":
            row["x"] = str(raw[x_field])
        if series_field and raw.get(series_field) is not None:
            row["series"] = str(raw[series_field])
        rows.append(row)
    return rows


# Rows from other tools' results


def curve_rows(data: Any, metric: Literal["gold", "xp"]) -> list[dict[str, Any]]:
    """get_gold_xp_curves result: list of {hero, wins: [CurvePoint], losses: [...]}."""
    if not isinstance(data, Sequence) or not data:
        raise ChartInputError("gold_xp_curves data must be the list get_gold_xp_curves returns.")
    rows = []
    for hero in data:
        if not isinstance(hero, Mapping) or "hero" not in hero:
            raise ChartInputError("Each gold_xp_curves entry needs 'hero' and 'wins'/'losses'.")
        for key, label in (("wins", "won"), ("losses", "lost")):
            for point in hero.get(key) or []:
                rows.append(
                    {
                        "x": point["minute"],
                        "y": _number(point[f"{metric}_median"], f"{metric}_median"),
                        "series": hero["hero"],
                        "outcome": label,
                        "line": f"{hero['hero']} ({label})",
                        "games": point.get("games"),
                    }
                )
    if not rows:
        raise ChartInputError("The curves contain no points.")
    return rows


def tier_rows(data: Any) -> list[dict[str, Any]]:
    """get_tier_list result: {heroes: [TierEntry]} (or the list of entries itself)."""
    entries = data.get("heroes") if isinstance(data, Mapping) else data
    if not isinstance(entries, Sequence) or not entries:
        raise ChartInputError("tier_list data must be the get_tier_list result (with 'heroes').")
    rows = []
    for e in entries:
        if not isinstance(e, Mapping) or "hero" not in e or "win_rate" not in e:
            raise ChartInputError("Each tier_list entry needs 'hero' and 'win_rate'.")
        ci = e.get("win_rate_ci") or (None, None)
        rows.append(
            {
                "x": e["hero"],
                "y": round(_number(e["win_rate"], "win_rate") * 100, 2),
                "tier": e.get("tier") or "?",
                "low": round(ci[0] * 100, 2) if ci[0] is not None else None,
                "high": round(ci[1] * 100, 2) if ci[1] is not None else None,
                "matches": e.get("matches"),
            }
        )
    return rows


def matchup_rows(data: Any) -> list[dict[str, Any]]:
    """analyze_draft result ({matchups: [...]}) or a list of {radiant, dire, radiant_advantage}."""
    pairs = data.get("matchups") if isinstance(data, Mapping) else data
    if not isinstance(pairs, Sequence) or not pairs:
        raise ChartInputError(
            "matchup_matrix data must be the analyze_draft result (with 'matchups') or a list "
            "of {radiant, dire, radiant_advantage}."
        )
    rows = []
    for p in pairs:
        if not isinstance(p, Mapping) or not {"radiant", "dire", "radiant_advantage"} <= set(p):
            raise ChartInputError("Each matchup needs 'radiant', 'dire' and 'radiant_advantage'.")
        rows.append(
            {
                "x": str(p["dire"]),
                "y": str(p["radiant"]),
                "value": round(_number(p["radiant_advantage"], "radiant_advantage") * 100, 2),
                "games": p.get("games"),
            }
        )
    return rows


DEFAULT_KIND: dict[Shape, ChartKind] = {
    "gold_xp_curves": "line",
    "tier_list": "bar",
    "matchup_matrix": "heatmap",
}


def build_chart(
    data: Any,
    *,
    shape: Shape = "rows",
    kind: ChartKind | None = None,
    title: str | None = None,
    x_title: str | None = None,
    y_title: str | None = None,
    x_field: str = "x",
    y_field: str = "y",
    series_field: str | None = "series",
    value_field: str = "value",
    metric: Literal["gold", "xp"] = "gold",
) -> Chart:
    data = unwrap(data)
    if shape != "rows":
        expected = DEFAULT_KIND[shape]
        if kind not in (None, expected):
            raise ChartInputError(f"Shape {shape!r} is drawn as a {expected} chart, not {kind}.")
        kind = expected
    if kind is None:
        raise ChartInputError("Pass kind (line, bar or heatmap) for shape 'rows'.")

    notes: list[str] = []
    if shape == "gold_xp_curves":
        rows = curve_rows(data, metric)
        heroes = _distinct([r["series"] for r in rows])
        unit = "gold (net worth)" if metric == "gold" else "XP"
        spec = line_spec(
            rows,
            x_title=x_title or "Minute",
            y_title=y_title or f"Median {unit}",
            title=title or f"Median {unit} by minute",
            subtitle="Solid: won matches. Dashed: lost matches.",
            dash_field="outcome",
        )
        notes.append("Late minutes rest on fewer, longer matches (see each point's games).")
        return _checked(Chart("line", spec, len(rows), heroes, notes))
    if shape == "tier_list":
        rows = tier_rows(data)
        tiers = [t for t in TIER_ORDER if any(r["tier"] == t for r in rows)]
        tiers += _distinct([r["tier"] for r in rows if r["tier"] not in tiers])
        has_ci = all(r["low"] is not None for r in rows)
        spec = bar_spec(
            rows,
            x_title=x_title or "Hero",
            y_title=y_title or "Win rate, %",
            title=title or "Tier list",
            subtitle="Bars: win rate. Lines: 95% confidence interval." if has_ci else None,
            color_field="tier",
            color_domain=tiers,
            with_interval=has_ci,
        )
        return _checked(Chart("bar", spec, len(rows), tiers, notes))
    if shape == "matchup_matrix":
        rows = matchup_rows(data)
        spec = heatmap_spec(
            rows,
            x_title=x_title or "Dire hero",
            y_title=y_title or "Radiant hero",
            value_title="Radiant advantage, pp",
            title=title or "Draft matchups",
            subtitle="Win-rate points for the Radiant hero against each Dire hero.",
            value_format="+.1f",
        )
        return _checked(Chart("heatmap", spec, len(rows), [], notes))

    rows = plain_rows(data, kind, x_field, y_field, series_field, value_field)
    series = _distinct([r["series"] for r in rows if "series" in r])
    if len(series) > MAX_CATEGORIES:
        raise ChartInputError(
            f"{len(series)} series; at most {MAX_CATEGORIES} can be told apart by color. "
            "Chart fewer series or split them over several charts."
        )
    if kind == "line":
        spec = line_spec(rows, x_title=x_title or x_field, y_title=y_title or y_field, title=title)
    elif kind == "bar":
        spec = bar_spec(rows, x_title=x_title or x_field, y_title=y_title or y_field, title=title)
    else:
        spec = heatmap_spec(
            rows,
            x_title=x_title or x_field,
            y_title=y_title or y_field,
            value_title=value_field,
            title=title,
        )
    return _checked(Chart(kind, spec, len(rows), series, notes))


def _checked(chart: Chart) -> Chart:
    if chart.rows > MAX_ROWS:
        raise ChartInputError(f"{chart.rows} data points; at most {MAX_ROWS} can be charted.")
    return chart


def render_png(spec: dict[str, Any], scale: float = 2.0) -> bytes:
    """PNG bytes of a Vega-Lite spec (blocking; run it in a worker thread)."""
    return vl_convert.vegalite_to_png(spec, vl_version=VEGA_LITE_VERSION, scale=scale)

"""Chart tool: Vega-Lite spec plus a PNG of any tool's numbers."""

import base64
from typing import Annotated, Any, Literal

import anyio
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult as McpToolResult
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field

from dota_insights.analytics import charts
from dota_insights.mcp.envelope import ToolResult, error, ok


class ChartResult(BaseModel):
    kind: str = Field(description='"line", "bar" or "heatmap".')
    shape: str = Field(description="Which input shape the data was read as.")
    vega_lite_spec: dict[str, Any] = Field(
        description="Vega-Lite v5 spec with the data inlined; render it with vega-embed."
    )
    data_points: int
    series: list[str] = Field(description="Series (lines), tiers or groups shown, in color order.")
    png_attached: bool = Field(description="Whether a PNG image block follows this result.")
    png_bytes: int | None = Field(default=None, description="Size of the attached PNG.")


def _response(envelope: ToolResult, png: bytes | None = None) -> McpToolResult:
    """The envelope as structured content and text, plus the PNG as an image block."""
    content: list = [TextContent(type="text", text=envelope.model_dump_json(exclude_none=True))]
    if png is not None:
        content.append(
            ImageContent(type="image", data=base64.b64encode(png).decode(), mimeType="image/png")
        )
    return McpToolResult(content=content, structured_content=envelope.model_dump(mode="json"))


def register(mcp: FastMCP) -> None:
    @mcp.tool(output_schema=ToolResult[ChartResult].model_json_schema())
    async def render_chart(
        data: Annotated[
            Any,
            Field(
                description=(
                    "What to chart. For shape 'rows': a list of objects, e.g. "
                    '[{"x": 10, "y": 4200, "series": "Anti-Mage"}]. For the other shapes: the '
                    "result (or whole response) of get_gold_xp_curves, get_tier_list or "
                    "analyze_draft, passed unchanged."
                )
            ),
        ],
        shape: Annotated[
            charts.Shape,
            Field(
                description=(
                    '"rows" for your own data; "gold_xp_curves" (line), "tier_list" (bar with '
                    'confidence intervals) or "matchup_matrix" (heatmap of analyze_draft '
                    "matchups) to chart another tool's result directly."
                )
            ),
        ] = "rows",
        kind: Annotated[
            charts.ChartKind | None,
            Field(
                description=(
                    '"line" (x and y numbers), "bar" (x category, y number) or "heatmap" '
                    "(x and y categories, value number). Required for shape 'rows'; the other "
                    "shapes pick their own."
                )
            ),
        ] = None,
        title: Annotated[str | None, Field(description="Chart title.", max_length=120)] = None,
        x_title: Annotated[str | None, Field(description="X-axis title.", max_length=60)] = None,
        y_title: Annotated[str | None, Field(description="Y-axis title.", max_length=60)] = None,
        x_field: Annotated[str, Field(description="Row field for x (shape 'rows').")] = "x",
        y_field: Annotated[str, Field(description="Row field for y (shape 'rows').")] = "y",
        series_field: Annotated[
            str | None,
            Field(description="Row field that names the series/group (shape 'rows'), at most 8."),
        ] = "series",
        value_field: Annotated[
            str, Field(description="Row field for the heatmap cell value (shape 'rows').")
        ] = "value",
        metric: Annotated[
            Literal["gold", "xp"], Field(description="For gold_xp_curves: which curve to draw.")
        ] = "gold",
        output: Annotated[
            Literal["both", "vega", "png"],
            Field(
                description=(
                    '"both" returns the spec and a PNG image; "vega" only the spec (cheaper); '
                    '"png" still returns the spec in the result, plus the image.'
                )
            ),
        ] = "both",
    ):
        """Draw a line, bar or heatmap chart of numbers from this server's tools.

        Returns a Vega-Lite spec (for clients that render Vega) and a PNG image of the same
        chart. Pass another tool's result with the matching shape to chart it without
        reshaping, or your own rows with shape "rows"."""  # fmt: skip
        try:
            chart = charts.build_chart(
                data,
                shape=shape,
                kind=kind,
                title=title,
                x_title=x_title,
                y_title=y_title,
                x_field=x_field,
                y_field=y_field,
                series_field=series_field,
                value_field=value_field,
                metric=metric,
            )
        except charts.ChartInputError as exc:
            return _response(error(str(exc), hint="Fix the data or the shape and call again."))

        caveats = list(chart.notes)
        png = None
        if output != "vega":
            try:
                png = await anyio.to_thread.run_sync(charts.render_png, chart.spec)
            except Exception as exc:  # renderer failures should not lose the spec
                caveats.append(f"PNG rendering failed ({exc}); only the spec is returned.")
        result = ChartResult(
            kind=chart.kind,
            shape=shape,
            vega_lite_spec=chart.spec,
            data_points=chart.rows,
            series=chart.series,
            png_attached=png is not None,
            png_bytes=len(png) if png is not None else None,
        )
        caveats.append(
            "The chart shows the numbers it was given; their caveats and sample sizes still "
            "apply and belong next to the chart."
        )
        envelope = ok(
            result,
            definitions={
                "vega_lite_spec": "Vega-Lite v5 JSON; data is inlined under data.values.",
                "data_points": "Rows plotted (points, bars or cells).",
                "png": "Rendered from vega_lite_spec with vl-convert, so both show the same.",
            },
            caveats=caveats,
        )
        return _response(envelope, png)

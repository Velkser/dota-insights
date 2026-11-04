"""Common result envelope returned by every analytics tool.

The envelope carries the provenance an AI caller needs to report a number honestly: how many
matches it rests on, over which filters and dates, how each metric is defined, its confidence
interval, caveats and data sources. Build it with `ok`, `insufficient_data` or `error`.
"""

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dota_insights.models.filters import Filters

Status = Literal["ok", "insufficient_data", "error"]
# "dota-insights-db": the collector's aggregates in our own database (see db/readers.py); it is
# listed together with the upstream sources those matches were collected from.
Source = Literal["opendota", "stratz", "steam", "dota-insights-db"]
DB_SOURCE: Source = "dota-insights-db"


class Period(BaseModel):
    """Inclusive date range of the matches behind a result."""

    model_config = ConfigDict(frozen=True)

    from_date: date = Field(description="First match date included (UTC).")
    to_date: date = Field(description="Last match date included (UTC).")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.to_date < self.from_date:
            raise ValueError("period to_date is before from_date")
        return self


class ToolResult[T](BaseModel):
    """Envelope around a tool's answer. Check `status` before reading `result`."""

    status: Status = Field(
        description=(
            '"ok": result is usable. "insufficient_data": too few matches for the filters; '
            'result is empty and "hint" says how to widen them. "error": the tool failed; '
            '"message" says why. Never present a number from a non-ok result as fact.'
        ),
    )
    result: T | None = Field(default=None, description="The answer; null unless status is ok.")
    message: str | None = Field(
        default=None,
        description="Why the status is not ok (reason for insufficient data, or error text).",
    )
    hint: str | None = Field(
        default=None,
        description="How to change the filters to get an ok result, when status is not ok.",
    )
    filters: Filters | None = Field(
        default=None, description="Filters the result was actually computed with."
    )
    sample_size: int | None = Field(
        default=None, ge=0, description="Number of matches the result is based on."
    )
    period: Period | None = Field(
        default=None, description="Date range of the matches behind the result."
    )
    definitions: dict[str, str] = Field(
        default_factory=dict,
        description="Metric name -> how it is computed. Use these when explaining numbers.",
    )
    confidence: dict[str, tuple[float, float]] = Field(
        default_factory=dict,
        description=(
            "Metric name -> [low, high] 95% confidence interval. Mention it when the "
            "interval is wide or overlaps the value being compared against."
        ),
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Limitations the user should hear alongside the result.",
    )
    source: list[Source] = Field(
        default_factory=list, description="Data sources the result was computed from."
    )
    data_freshness: datetime | None = Field(
        default=None, description="When the underlying data was last updated (UTC)."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for metric, (low, high) in self.confidence.items():
            if low > high:
                raise ValueError(f"confidence interval for {metric!r} has low > high")
        if self.status == "ok":
            if self.filters is not None:
                if self.sample_size is None:
                    raise ValueError("ok result with filters must report sample_size")
                if self.sample_size < self.filters.min_sample:
                    raise ValueError(
                        f"sample_size {self.sample_size} is below min_sample "
                        f"{self.filters.min_sample}; return insufficient_data instead"
                    )
        else:
            if self.result is not None:
                raise ValueError(f"{self.status} result must not carry a result")
            if not self.message:
                raise ValueError(f"{self.status} result must explain itself in message")
            if self.status == "insufficient_data" and not (self.hint and self.hint.strip()):
                raise ValueError("insufficient_data result must say how to widen filters in hint")
        return self


def ok[T](
    result: T,
    *,
    filters: Filters | None = None,
    sample_size: int | None = None,
    period: Period | None = None,
    definitions: dict[str, str] | None = None,
    confidence: dict[str, tuple[float, float]] | None = None,
    caveats: list[str] | None = None,
    source: list[Source] | None = None,
    data_freshness: datetime | None = None,
) -> ToolResult[T]:
    """Successful result. Raises ValueError if sample_size is below filters.min_sample."""
    return ToolResult(
        status="ok",
        result=result,
        filters=filters,
        sample_size=sample_size,
        period=period,
        definitions=definitions or {},
        confidence=confidence or {},
        caveats=caveats or [],
        source=source or [],
        data_freshness=data_freshness,
    )


def insufficient_data(
    reason: str,
    hint: str,
    *,
    filters: Filters | None = None,
    sample_size: int | None = None,
    period: Period | None = None,
    caveats: list[str] | None = None,
    source: list[Source] | None = None,
    data_freshness: datetime | None = None,
) -> ToolResult:
    """Too few matches to answer. `hint` tells the caller how to widen the filters."""
    return ToolResult(
        status="insufficient_data",
        message=reason,
        hint=hint,
        filters=filters,
        sample_size=sample_size,
        period=period,
        caveats=caveats or [],
        source=source or [],
        data_freshness=data_freshness,
    )


def error(
    message: str,
    *,
    hint: str | None = None,
    filters: Filters | None = None,
    source: list[Source] | None = None,
) -> ToolResult:
    """The tool failed (bad input, upstream outage). `hint` may suggest what to retry."""
    return ToolResult(
        status="error",
        message=message,
        hint=hint,
        filters=filters,
        source=source or [],
    )

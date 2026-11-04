"""What one eval run of a question produced: the tool calls, the final answer and the costs.

Transcripts are plain data so they can be saved in a report and replayed by the scoring tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """One tool call the model made and what the server returned."""

    name: str
    arguments: dict[str, Any]
    # Envelope status ("ok", "insufficient_data", "error"); None when the result is not an envelope.
    status: str | None = None
    envelope: dict[str, Any] | None = None
    # True when the MCP call itself failed (unknown tool, argument validation, exception).
    is_error: bool = False
    error: str | None = None
    latency_s: float = 0.0


@dataclass
class Transcript:
    """Everything the harness recorded for one question."""

    case_id: str
    model: str
    calls: list[ToolCall] = field(default_factory=list)
    answer: str = ""
    turns: int = 0
    api_calls: int = 0
    stop_reason: str | None = None
    hit_turn_cap: bool = False
    latency_s: float = 0.0
    model_latency_s: float = 0.0
    tool_latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    # Set when the run broke off (API error, refusal); scoring then fails every dimension.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        calls = [ToolCall(**c) for c in data.get("calls", [])]
        return cls(**{**data, "calls": calls})

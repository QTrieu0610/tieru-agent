"""Typed, public records for Tieru Replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ReplayStatus = Literal["running", "completed", "failed"]


@dataclass(frozen=True)
class ReplayRun:
    run_id: str
    session_id: str
    source: str
    started_at: str
    completed_at: str | None = None
    status: ReplayStatus = "running"
    role: str = "main"
    model: str = ""
    provider: str = ""
    iterations: int = 0
    latency_ms: int | None = None
    event_count: int = 0
    tool_count: int = 0
    trust_decision_count: int = 0
    input_preview: str = ""
    output_preview: str = ""
    error_code: str = ""
    error_summary: str = ""


@dataclass(frozen=True)
class ReplayEvent:
    event_id: str
    run_id: str
    sequence: int
    timestamp: str
    category: str
    event_type: str
    node: str = ""
    tool: str = ""
    role: str = ""
    model: str = ""
    provider: str = ""
    duration_ms: int | None = None
    safe_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedEvent:
    category: str
    event_type: str
    safe_payload: dict[str, Any]
    node: str = ""
    tool: str = ""
    role: str = ""
    model: str = ""
    provider: str = ""
    duration_ms: int | None = None

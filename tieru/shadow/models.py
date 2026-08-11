"""Typed, public records for passive Tieru Shadow metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ShadowPattern:
    pattern_id: str
    workflow_signature: str
    status: str
    occurrence_count: int
    successful_count: int
    verification_count: int
    first_seen_at: str
    last_seen_at: str
    source_run_ids: list[str] = field(default_factory=list)
    representative_tools: list[str] = field(default_factory=list)
    representative_operations: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    confidence: str = "low"
    suppress_until_count: int = 0
    snoozed_until: str = ""
    forge_draft_id: str = ""
    metadata: dict = field(default_factory=dict)

    def public(self) -> dict:
        return asdict(self)

@dataclass(frozen=True)
class ShadowSuggestion:
    suggestion_id: str
    pattern_id: str
    workflow_signature: str
    status: str
    occurrence_count: int
    confidence: str
    representative_run_ids: list[str]
    suggested_name: str
    summary: str
    required_tools: list[str]
    required_capabilities: list[str]
    explanation: list[str]
    created_at: str
    updated_at: str
    snoozed_until: str = ""
    forge_draft_id: str = ""

    def public(self) -> dict:
        return asdict(self)

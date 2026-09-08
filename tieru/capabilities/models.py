"""Production-owned capability and routing models for M24."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Capability:
    """Production-owned capability representing one or more related tool operations."""

    capability_id: str
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    risk: str = "low"
    read_only: bool = True
    operations: tuple[str, ...] = ("read",)
    always_visible: bool = False

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise ValueError("capability_id must not be empty")
        if not self.name.strip():
            raise ValueError("name must not be empty")


@dataclass(frozen=True)
class CapabilityMatch:
    """Ranked match result for a capability candidate."""

    capability_id: str
    score: float
    lexical_score: float
    semantic_score: float | None
    reason: str
    tool_names: tuple[str, ...]
    matched_alias: str | None = None


@dataclass(frozen=True)
class CapabilityRoutingResult:
    """Outcome of capability discovery and tool subset selection."""

    selected_capabilities: tuple[CapabilityMatch, ...]
    selected_tools: tuple[str, ...]
    truncated: bool = False
    candidate_tool_count: int = 0
    reason: str = ""

    @property
    def selected_capability_ids(self) -> tuple[str, ...]:
        return tuple(m.capability_id for m in self.selected_capabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_capabilities": [m.capability_id for m in self.selected_capabilities],
            "selected_tools": list(self.selected_tools),
            "matches": [
                {
                    "capability_id": m.capability_id,
                    "score": m.score,
                    "reason": m.reason,
                    "tools": list(m.tool_names),
                }
                for m in self.selected_capabilities
            ],
            "truncated": self.truncated,
            "candidate_tool_count": self.candidate_tool_count,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CapabilityRouterConfig:
    """Deterministic configuration for capability routing."""

    max_visible_tools: int = 8
    min_score: float = 0.30
    top_k: int = 4
    lexical_weight: float = 0.60
    semantic_weight: float = 0.40
    semantic_enabled: bool = False
    bm25_k1: float = 1.2
    mandatory_tools: tuple[str, ...] = ()
    mandatory_capabilities: tuple[str, ...] = ()
    filter_destructive: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.max_visible_tools <= 64:
            raise ValueError("max_visible_tools must be between 1 and 64")
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be between 0.0 and 1.0")
        if not 1 <= self.top_k <= 32:
            raise ValueError("top_k must be between 1 and 32")
        if self.lexical_weight < 0 or self.semantic_weight < 0:
            raise ValueError("weights cannot be negative")
        if not math.isclose(self.lexical_weight + self.semantic_weight, 1.0):
            raise ValueError("lexical_weight and semantic_weight must sum to 1.0")

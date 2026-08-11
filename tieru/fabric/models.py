"""Typed, public-safe Model Fabric decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ExecutionMode(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    AGENT = "agent"
    DEEP = "deep"


class PrivacyPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    LOCAL_FIRST = "local_first"
    BALANCED = "balanced"
    QUALITY_FIRST = "quality_first"


@dataclass(frozen=True)
class ModelCandidate:
    """One explicitly configured or compatibility-derived execution target."""

    candidate_id: str
    provider: str
    model: str
    protocol: str
    role_compatibility: tuple[str, ...] = ("main", "small")
    local: bool = False
    enabled: bool = True
    configured: bool = True
    capabilities: dict[str, bool | None] = field(default_factory=dict)
    context_limit: int | None = None
    output_limit: int | None = None
    cost_tier: str = "unknown"
    latency_tier: str = "unknown"
    privacy_class: str = "standard"
    preference_weight: float = 0.5
    base_url: str | None = None
    api_key_env: str = field(default="", repr=False)
    explicit: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "id": self.candidate_id,
            "provider": self.provider,
            "model": self.model,
            "protocol": self.protocol,
            "role_compatibility": list(self.role_compatibility),
            "local": self.local,
            "enabled": self.enabled,
            "configured": self.configured,
            "capabilities": dict(self.capabilities),
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "cost_tier": self.cost_tier,
            "latency_tier": self.latency_tier,
            "privacy_class": self.privacy_class,
            "preference": self.preference_weight,
            "explicit": self.explicit,
        }


@dataclass(frozen=True)
class AvailabilityStatus:
    candidate_id: str
    configured: bool
    credential_available: bool | None
    endpoint_available: bool | None
    model_available: bool | None
    available: bool
    reason: str
    checked_at: str
    cached: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: str
    provider: str
    model: str
    eligible: bool
    exclusion_reasons: tuple[str, ...] = ()
    total_score: float | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)
    availability: AvailabilityStatus | None = None

    def public(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "model": self.model,
            "eligible": self.eligible,
            "exclusion_reasons": list(self.exclusion_reasons),
            "total_score": self.total_score,
            "score_breakdown": dict(self.score_breakdown),
            "availability": self.availability.public() if self.availability else None,
        }


@dataclass(frozen=True)
class ModelSelection:
    candidate_id: str
    provider: str
    model: str
    role: str
    total_score: float
    score_breakdown: dict[str, float]
    candidates: tuple[CandidateEvaluation, ...]
    reasons: tuple[str, ...]
    fallback_chain: tuple[str, ...] = ()
    fallback_count: int = 0
    initial_candidate_id: str = ""

    @property
    def excluded_candidates(self) -> tuple[CandidateEvaluation, ...]:
        return tuple(item for item in self.candidates if not item.eligible)

    def public(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "initial_candidate_id": self.initial_candidate_id or self.candidate_id,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "total_score": self.total_score,
            "score_breakdown": dict(self.score_breakdown),
            "candidates": [item.public() for item in self.candidates],
            "excluded_candidates": [item.public() for item in self.excluded_candidates],
            "reasons": list(self.reasons),
            "fallback_chain": list(self.fallback_chain),
            "fallback_count": self.fallback_count,
        }


@dataclass(frozen=True)
class TaskProfile:
    task_type: str
    complexity: str
    requires_tools: bool = False
    requires_memory: bool = False
    requires_deep_context: bool = False
    requires_verification: bool = False
    privacy: str = "private"
    interactive: bool = True
    estimated_scope: str = "single_turn"
    signals: tuple[str, ...] = field(default_factory=tuple)
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def public(self) -> dict:
        value = asdict(self)
        value["signals"] = list(self.signals)
        value["reason_codes"] = list(self.reason_codes)
        return value


@dataclass(frozen=True)
class ExecutionProfile:
    mode: ExecutionMode
    role: str
    max_tokens: int
    max_iterations: int
    history_turns: int
    tools_enabled: bool
    memory_enabled: bool
    graph_memory_enabled: bool
    verification_enabled: bool

    def public(self) -> dict:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


@dataclass(frozen=True)
class RouteDecision:
    mode: ExecutionMode
    profile: ExecutionProfile
    task_profile: TaskProfile
    provider: str
    model: str
    role: str
    reason_codes: tuple[str, ...]
    explanation: str
    fallback_used: bool = False
    classifier_source: str = "deterministic"
    model_selection: ModelSelection | None = None

    def public(self) -> dict:
        return {
            "mode": self.mode.value,
            "profile": self.profile.public(),
            "task_profile": self.task_profile.public(),
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "reason_codes": list(self.reason_codes),
            "explanation": self.explanation,
            "fallback_used": self.fallback_used,
            "classifier_source": self.classifier_source,
            "model_selection": (
                self.model_selection.public() if self.model_selection else None
            ),
        }

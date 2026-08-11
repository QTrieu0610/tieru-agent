"""Typed records for the local Skill Forge lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DraftStatus = Literal[
    "draft", "validated", "evaluation_failed", "ready_for_review",
    "approved", "installed", "rejected", "archived",
]


@dataclass
class WorkflowStep:
    step_id: str
    operation: str
    tool: str
    status: str
    arguments: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    trust_requirements: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    verification: bool = False


@dataclass
class WorkflowCandidate:
    candidate_id: str
    source_run_ids: list[str]
    title: str
    intent: str
    inputs: list[dict[str, Any]]
    steps: list[WorkflowStep]
    outputs: list[str]
    tools: list[str]
    capabilities: list[str]
    trust_requirements: list[str]
    preconditions: list[str]
    postconditions: list[str]
    failure_paths: list[str]
    evidence: list[str]
    created_at: str
    workflow_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowCandidate:
        data = dict(value)
        data["steps"] = [WorkflowStep(**step) for step in data.get("steps", [])]
        return cls(**data)


@dataclass
class SkillDraft:
    draft_id: str
    metadata: dict[str, Any]
    workflow: WorkflowCandidate
    content: str
    validation: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "metadata": self.metadata,
            "workflow": self.workflow.to_dict(),
            "content": self.content,
            "validation": self.validation,
            "evaluation": self.evaluation,
        }

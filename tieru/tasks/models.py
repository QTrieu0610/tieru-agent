"""Typed state for Tieru's local durable-task engine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"


class PlanReviewDecision(StrEnum):
    KEEP = "keep"
    REVISE_REMAINING = "revise_remaining"
    BLOCK = "block"


class StepFailureDisposition(StrEnum):
    RETRY_SAME_STEP = "retry_same_step"
    REPLAN = "replan"
    BLOCK = "block"
    FAIL = "fail"


@dataclass(frozen=True)
class StepFailureAssessment:
    disposition: StepFailureDisposition
    reason_code: str
    evidence_summary: str
    is_recoverable: bool = False
    strategy_fingerprint: str = ""


class VerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class StepExecutionKind(StrEnum):
    REASONING = "reasoning"
    READ = "read"
    WRITE = "write"
    COMMAND = "command"
    EXTERNAL_ACTION = "external_action"
    MIXED = "mixed"


StepVerificationKind = StepExecutionKind


@dataclass(frozen=True)
class StepEvidenceRequirement:
    kind: str
    description: str
    required: bool = True


class CheckpointKind(StrEnum):
    FILE_READ = "file_read"
    ARTIFACT_MUTATION = "artifact_mutation"
    COMMAND_EXECUTION = "command_execution"
    TEST_RESULT = "test_result"
    TOOL_EXECUTION = "tool_execution"
    REASONING_OUTPUT = "reasoning_output"


class CheckpointLossClass(StrEnum):
    CHECKPOINT_NOT_CREATED = "checkpoint_not_created"
    CHECKPOINT_NOT_PERSISTED = "checkpoint_not_persisted"
    CHECKPOINT_NOT_RELOADED = "checkpoint_not_reloaded"
    CHECKPOINT_NOT_ATTACHED_TO_STEP = "checkpoint_not_attached_to_step"
    CHECKPOINT_DROPPED_DURING_NORMALIZATION = "checkpoint_dropped_during_normalization"
    CHECKPOINT_NOT_CONSUMED_BY_VERIFIER = "checkpoint_not_consumed_by_verifier"
    CHECKPOINT_CONSUMED_BUT_INSUFFICIENT = "checkpoint_consumed_but_insufficient"
    NO_EXECUTION_OCCURRED = "no_execution_occurred"


class DivergenceAttribution(StrEnum):
    MODEL_QUALITY = "MODEL_QUALITY"
    ROLE_ROUTING = "ROLE_ROUTING"
    CHECKPOINT_PIPELINE = "CHECKPOINT_PIPELINE"
    VERIFICATION = "VERIFICATION"
    BUDGET = "BUDGET"
    CAPABILITY_ROUTING = "CAPABILITY_ROUTING"
    TRUST_POLICY = "TRUST_POLICY"
    PROVIDER = "PROVIDER"
    EVAL_METRICS = "EVAL_METRICS"
    FIXTURE = "FIXTURE"
    UNKNOWN = "UNKNOWN"


def compute_checkpoint_evidence_hash(data: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 evidence hash over stable bounded fields without secrets."""
    clean = {
        "action_ledger_id": data.get("action_ledger_id"),
        "after_hash": data.get("after_hash"),
        "before_hash": data.get("before_hash"),
        "exit_code": data.get("exit_code"),
        "kind": str(data.get("kind") or ""),
        "path": data.get("path"),
        "source": str(data.get("source") or ""),
        "timed_out": bool(data.get("timed_out", False)),
        "tool_name": str(data.get("tool_name") or ""),
    }
    raw = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ExecutionCheckpoint:
    checkpoint_id: str
    task_id: str
    step_id: str
    kind: str
    source: str
    evidence_hash: str
    created_at: str
    run_id: str = ""
    tool_name: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: float = 0.0
    path: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    exists: bool | None = None
    action_ledger_id: str | None = None
    consumed_by_verifier: bool = False
    summary: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "kind": self.kind,
            "source": self.source,
            "evidence_hash": self.evidence_hash,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "path": self.path,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "exists": self.exists,
            "action_ledger_id": self.action_ledger_id,
            "consumed_by_verifier": self.consumed_by_verifier,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class StepEvidence:
    step_id: str
    kind: StepExecutionKind
    tools_requested: tuple[str, ...] = ()
    tools_executed: tuple[str, ...] = ()
    successful_tool_results: tuple[dict[str, Any], ...] = ()
    failed_tool_results: tuple[dict[str, Any], ...] = ()
    command_results: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    assistant_output_summary: str | None = None
    has_uncertain_action: bool = False
    missing_requirements: tuple[str, ...] = ()
    checkpoints: tuple[ExecutionCheckpoint, ...] = ()


def classify_checkpoint_loss(
    *,
    has_execution: bool = True,
    checkpoint_created: bool = True,
    checkpoint_persisted: bool = True,
    checkpoint_reloaded: bool = True,
    checkpoint_attached: bool = True,
    checkpoint_dropped: bool = False,
    consumed_by_verifier: bool = True,
    is_sufficient: bool = True,
) -> CheckpointLossClass:
    """Classify the precise point of checkpoint evidence loss."""
    if not has_execution:
        return CheckpointLossClass.NO_EXECUTION_OCCURRED
    if not checkpoint_created:
        return CheckpointLossClass.CHECKPOINT_NOT_CREATED
    if not checkpoint_persisted:
        return CheckpointLossClass.CHECKPOINT_NOT_PERSISTED
    if not checkpoint_reloaded:
        return CheckpointLossClass.CHECKPOINT_NOT_RELOADED
    if not checkpoint_attached:
        return CheckpointLossClass.CHECKPOINT_NOT_ATTACHED_TO_STEP
    if checkpoint_dropped:
        return CheckpointLossClass.CHECKPOINT_DROPPED_DURING_NORMALIZATION
    if not consumed_by_verifier:
        return CheckpointLossClass.CHECKPOINT_NOT_CONSUMED_BY_VERIFIER
    if not is_sufficient:
        return CheckpointLossClass.CHECKPOINT_CONSUMED_BUT_INSUFFICIENT
    return CheckpointLossClass.CHECKPOINT_NOT_CONSUMED_BY_VERIFIER


def compute_step_checkpoint_completeness(
    required_kinds: tuple[str, ...],
    observed_checkpoints: tuple[ExecutionCheckpoint, ...],
) -> dict[str, tuple[str, ...]]:
    """Compute required vs observed vs missing checkpoint diagnostic for a step."""
    observed_kinds = tuple(sorted({cp.kind for cp in observed_checkpoints}))
    missing = tuple(sorted(k for k in required_kinds if k not in observed_kinds))
    return {
        "required_checkpoint_kinds": tuple(sorted(required_kinds)),
        "observed_checkpoint_kinds": observed_kinds,
        "missing_checkpoint_kinds": missing,
    }


class GoalVerificationStatus(StrEnum):
    PASS = "pass"
    FAIL_REPLANABLE = "fail_replanable"
    FAIL_TERMINAL = "fail_terminal"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class CriterionStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class StepClaimOutcome(StrEnum):
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    NO_PENDING_STEP = "no_pending_step"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class TaskLimits:
    max_steps_per_task: int = 8
    max_goal_bytes: int = 4096
    max_title_bytes: int = 256
    max_instruction_bytes: int = 4096
    max_result_bytes: int = 4096
    max_verification_summary_bytes: int = 1024
    max_context_bytes: int = 12_000
    max_execution_steps_per_invocation: int = 1
    max_attempts_per_step: int = 1
    running_step_stale_after_seconds: int = 900
    max_replans_per_task: int = 2
    max_criteria_per_contract: int = 6
    max_constraints_per_contract: int = 6
    max_criterion_description_bytes: int = 500
    max_constraint_description_bytes: int = 500
    max_execution_turns_per_step: int = 3

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if int(value) < 1:
                raise ValueError(f"{name} must be positive")

    def default_budget(self) -> TaskBudget:
        return TaskBudget(
            max_steps=self.max_steps_per_task,
            max_replans=self.max_replans_per_task,
        )


class ModelCallCriticality(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


BudgetCriticality = ModelCallCriticality


class ModelCallPurpose(StrEnum):
    STEP_EXECUTION = "step_execution"
    GOAL_VERIFICATION = "goal_verification"
    STEP_VERIFICATION = "step_verification"
    FAILURE_RECOVERY = "failure_recovery"
    PLAN_REVIEW_OPPORTUNISTIC = "plan_review_opportunistic"
    FINAL_SYNTHESIS = "final_synthesis"
    STEP_CONTINUATION = "step_continuation"


class BudgetDecision(StrEnum):
    ALLOW = "allow"
    EXHAUSTED = "exhausted"


class BudgetResource(StrEnum):
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    STEPS = "steps"
    REPLANS = "replans"
    VERIFICATION_CALLS = "verification_calls"
    RETRIES = "retries"
    COMMAND_RUNTIME = "command_runtime"
    ACTIVE_RUNTIME = "active_runtime"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"


class BudgetExhaustionReason(StrEnum):
    MODEL_CALL_LIMIT = "model_call_limit"
    TOOL_CALL_LIMIT = "tool_call_limit"
    STEP_LIMIT = "step_limit"
    REPLAN_LIMIT = "replan_limit"
    VERIFICATION_LIMIT = "verification_limit"
    RETRY_LIMIT = "retry_limit"
    COMMAND_RUNTIME_LIMIT = "command_runtime_limit"
    ACTIVE_RUNTIME_LIMIT = "active_runtime_limit"
    TOKEN_LIMIT = "token_limit"


@dataclass(frozen=True)
class TaskBudget:
    max_model_calls: int = 20
    max_tool_calls: int = 30
    max_steps: int = 8
    max_replans: int = 2
    max_verification_calls: int = 10
    max_retries: int = 4
    max_command_runtime_seconds: float = 120.0
    max_active_runtime_seconds: float = 600.0
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls must be positive")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.max_replans < 0:
            raise ValueError("max_replans cannot be negative")
        if self.max_verification_calls < 1:
            raise ValueError("max_verification_calls must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_command_runtime_seconds <= 0:
            raise ValueError("max_command_runtime_seconds must be positive")
        if self.max_active_runtime_seconds <= 0:
            raise ValueError("max_active_runtime_seconds must be positive")
        if self.max_input_tokens is not None and self.max_input_tokens < 1:
            raise ValueError("max_input_tokens must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True)
class TaskBudgetUsage:
    model_calls: int = 0
    tool_calls: int = 0
    steps_started: int = 0
    replans: int = 0
    verification_calls: int = 0
    retries: int = 0
    command_runtime_seconds: float = 0.0
    active_runtime_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def steps(self) -> int:
        return self.steps_started


@dataclass(frozen=True)
class BudgetReservationResult:
    decision: BudgetDecision
    resource: BudgetResource
    amount: float
    current_usage: float
    limit_value: float
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == BudgetDecision.ALLOW


@dataclass(frozen=True)
class BudgetAllocation:
    allocation_id: str
    task_id: str
    action: str
    resource: str
    amount: float
    previous_limit: float
    new_limit: float
    actor: str = "system"
    note: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class SuccessCriterion:
    criterion_id: str
    description: str
    verification_kind: str = "deterministic"
    required_evidence: str = ""
    required: bool = True


@dataclass(frozen=True)
class GoalConstraint:
    constraint_id: str
    description: str
    constraint_kind: str = "user_intent"
    required: bool = True


@dataclass(frozen=True)
class GoalContract:
    contract_id: str
    task_id: str
    goal: str
    success_criteria: tuple[SuccessCriterion, ...] = ()
    constraints: tuple[GoalConstraint, ...] = ()
    created_at: str = ""


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    status: CriterionStatus
    evidence_summary: str = ""


@dataclass(frozen=True)
class TaskGoalVerification:
    verification_id: str
    task_id: str
    status: GoalVerificationStatus
    criterion_results: tuple[CriterionResult, ...]
    summary: str
    plan_revision_number: int = 0
    evidence_hash: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class PlanStep:
    title: str
    instruction: str
    verification: str
    execution_kind: StepExecutionKind | None = None
    evidence_requirements: tuple[StepEvidenceRequirement, ...] = ()


@dataclass(frozen=True)
class TaskPlanRevision:
    revision_id: str
    task_id: str
    revision_number: int
    reason: str
    created_at: str
    trigger_step_id: str | None = None


@dataclass(frozen=True)
class PlanReviewResult:
    decision: PlanReviewDecision
    reason: str
    remaining_steps: tuple[PlanStep, ...] = ()


@dataclass(frozen=True)
class Task:
    task_id: str
    goal: str
    status: TaskStatus
    current_step_id: str | None
    source: str
    session_id: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    source_id: str | None = None


@dataclass(frozen=True)
class TaskStep:
    step_id: str
    task_id: str
    position: int
    title: str
    instruction: str
    verification_instruction: str
    status: StepStatus
    attempt_count: int
    max_attempts: int
    result: str | None
    result_size: int
    result_truncated: bool
    verification_status: VerificationStatus | None
    verification_summary: str | None
    execution_run_id: str | None
    started_at: str | None
    completed_at: str | None
    updated_at: str
    plan_revision_id: str | None = None
    superseded_by_revision: str | None = None
    execution_kind: StepExecutionKind | None = None
    evidence_requirements: tuple[StepEvidenceRequirement, ...] = ()


@dataclass(frozen=True)
class StepExecutionResult:
    status: StepStatus
    evidence: StepEvidence
    missing_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepClaim:
    outcome: StepClaimOutcome
    task: Task
    step: TaskStep | None = None
    code: str = ""


@dataclass(frozen=True)
class StepExecution:
    result: str
    run_id: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    summary: str


@dataclass(frozen=True)
class TaskRunResult:
    task: Task
    step: TaskStep | None
    code: str
    executed: bool


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PLANNED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PAUSED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.CANCELLED}
    ),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset(
        {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.SUPERSEDED}
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.BLOCKED,
            StepStatus.SKIPPED,
        }
    ),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.BLOCKED: frozenset({StepStatus.PENDING, StepStatus.SUCCEEDED}),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.SUPERSEDED: frozenset(),
}


class TaskStateError(RuntimeError):
    """Raised when a requested task or step transition is illegal."""


class TaskValidationError(ValueError):
    """Raised before malformed or oversized task data reaches SQLite."""


class PlanValidationError(TaskValidationError):
    """Raised when structured planner output violates the bounded contract."""


class ReplanLimitExceededError(TaskStateError):
    """Raised when a task exceeds its configured maximum replan revisions."""


class InvalidPlanRevisionError(PlanValidationError):
    """Raised when structured replanner output violates the bounded contract."""


class GoalContractError(TaskValidationError):
    """Raised when a goal contract is malformed or invalid."""


class GoalVerificationError(RuntimeError):
    """Raised when goal verification encounters an unrecoverable error."""


class TaskBudgetExhaustedError(TaskStateError):
    """Raised when a durable task exhausts an allocated resource budget."""

    def __init__(self, resource: str, current: float, limit: float, reason: str = "") -> None:
        super().__init__(f"Task budget exhausted for {resource}: {current}/{limit} ({reason})")
        self.resource = resource
        self.current = current
        self.limit = limit
        self.reason = reason


class StepContinuationDecision(StrEnum):
    """Authoritative runtime decision regarding step completion and continuation."""

    READY_TO_VERIFY = "ready_to_verify"
    CONTINUE = "continue"
    REPLAN_REQUIRED = "replan_required"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class StepCompletionAssessment:
    """Structured assessment of step evidence completeness produced by the controller."""

    decision: StepContinuationDecision
    satisfied_requirements: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    reason: str


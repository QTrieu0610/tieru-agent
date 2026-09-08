"""Versioned, bounded public models for reliability evaluation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvalVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class LiveBaselineStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED_PROVIDER_UNAVAILABLE = "BLOCKED_PROVIDER_UNAVAILABLE"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class FailureStage(StrEnum):
    CONTRACT = "contract"
    PLANNING = "planning"
    CAPABILITY_ROUTING = "capability_routing"
    TOOL_SELECTION = "tool_selection"
    TRUST = "trust"
    TOOL_EXECUTION = "tool_execution"
    STEP_VERIFICATION = "step_verification"
    REPLANNING = "replanning"
    GOAL_VERIFICATION = "goal_verification"
    BUDGET = "budget"
    PROVIDER = "provider"


class RootCauseClass(StrEnum):
    TRUST_POLICY_MISCONFIGURATION = "trust_policy_misconfiguration"
    TRUST_METADATA_MISMATCH = "trust_metadata_mismatch"
    LEGITIMATE_CONFIRMATION_REQUIRED = "legitimate_confirmation_required"
    UNNECESSARY_HIGH_RISK_TOOL_SELECTED = "unnecessary_high_risk_tool_selected"
    WRONG_OPERATION_SELECTED = "wrong_operation_selected"
    TOOL_ARGUMENT_ERROR = "tool_argument_error"
    CAPABILITY_ROUTER_OVEREXPOSURE = "capability_router_overexposure"
    CAPABILITY_ROUTER_UNDEREXPOSURE = "capability_router_underexposure"
    PLANNER_STEP_MISMATCH = "planner_step_mismatch"
    EVAL_FIXTURE_POLICY_MISMATCH = "eval_fixture_policy_mismatch"
    RUNTIME_STATE_TRANSITION_BUG = "runtime_state_transition_bug"
    INSUFFICIENT_STEP_EVIDENCE = "insufficient_step_evidence"
    LEGITIMATE_SECURITY_DENIAL = "legitimate_security_denial"
    PROVIDER_FAILURE = "provider_failure"
    SEMANTIC_VERIFICATION_FAILED = "semantic_verification_failed"
    SEMANTIC_VERIFIER_UNAVAILABLE = "semantic_verifier_unavailable"
    DETERMINISTIC_VERIFICATION_FAILED = "deterministic_verification_failed"
    TOOL_EVIDENCE_FAILED = "tool_evidence_failed"
    PLAN_EVIDENCE_GAP = "plan_evidence_gap"
    PLAN_CAPABILITY_MISMATCH = "plan_capability_mismatch"
    REQUIRED_TOOL_NOT_INVOKED = "required_tool_not_invoked"
    EVIDENCE_CORRECTION_EXHAUSTED = "evidence_correction_exhausted"
    EXECUTOR_UNKNOWN_TOOL = "executor_unknown_tool"
    EXECUTOR_INVALID_TOOL_ARGUMENTS = "executor_invalid_tool_arguments"
    EXECUTOR_TOOL_OMISSION = "executor_tool_omission"
    EXECUTOR_PREMATURE_FINAL = "executor_premature_final"
    EXECUTOR_NO_PROGRESS = "executor_no_progress"
    EXECUTOR_SEQUENCE_EXHAUSTED = "executor_sequence_exhausted"


class FailureType(StrEnum):
    PLANNING_ERROR = "planning_error"
    SKILL_RETRIEVAL_ERROR = "skill_retrieval_error"
    TOOL_SELECTION_ERROR = "tool_selection_error"
    TRUST_DENIAL_EXPECTED = "trust_denial_expected"
    TRUST_DENIAL_UNEXPECTED = "trust_denial_unexpected"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    VERIFICATION_ERROR = "verification_error"
    FALSE_SUCCESS = "false_success"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    MODEL_BUDGET_EXCEEDED = "model_budget_exceeded"
    TOOL_BUDGET_EXCEEDED = "tool_budget_exceeded"
    STEP_BUDGET_EXCEEDED = "step_budget_exceeded"
    RUNTIME_BUDGET_EXCEEDED = "runtime_budget_exceeded"
    VERIFICATION_BUDGET_EXCEEDED = "verification_budget_exceeded"
    RETRY_BUDGET_EXCEEDED = "retry_budget_exceeded"
    REPLAN_BUDGET_EXCEEDED = "replan_budget_exceeded"
    RECOVERY_ERROR = "recovery_error"
    PROMPT_INJECTION_FAILURE = "prompt_injection_failure"
    MODEL_ERROR = "model_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    REPLANNING_ERROR = "replanning_error"
    REPLAN_LIMIT_EXCEEDED = "replan_limit_exceeded"
    INVALID_PLAN_REVISION = "invalid_plan_revision"
    GOAL_CONTRACT_ERROR = "goal_contract_error"
    GOAL_VERIFICATION_ERROR = "goal_verification_error"
    STEP_VERIFICATION_ERROR = "step_verification_error"
    PRE_GOAL_BLOCK = "pre_goal_block"
    GOAL_FALSE_PASS = "goal_false_pass"
    CONSTRAINT_VIOLATION = "constraint_violation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CAPABILITY_ROUTING_ERROR = "capability_routing_error"
    REQUIRED_TOOL_HIDDEN = "required_tool_hidden"
    IRRELEVANT_TOOL_SELECTED = "irrelevant_tool_selected"
    SEMANTIC_VERIFICATION_FAILED = "semantic_verification_failed"
    SEMANTIC_VERIFIER_UNAVAILABLE = "semantic_verifier_unavailable"
    DETERMINISTIC_VERIFICATION_FAILED = "deterministic_verification_failed"
    TOOL_EVIDENCE_FAILED = "tool_evidence_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_MALFORMED_RESPONSE = "provider_malformed_response"
    PROVIDER_CONTEXT_LIMIT = "provider_context_limit"
    PROVIDER_AUTH_ERROR = "provider_auth_error"
    PLAN_EVIDENCE_GAP = "plan_evidence_gap"
    PLAN_CAPABILITY_MISMATCH = "plan_capability_mismatch"
    REQUIRED_TOOL_NOT_INVOKED = "required_tool_not_invoked"
    EVIDENCE_CORRECTION_EXHAUSTED = "evidence_correction_exhausted"
    STEP_CONTINUATION_EXHAUSTED = "step_continuation_exhausted"
    EARLY_MODEL_TERMINATION = "early_model_termination"
    EVIDENCE_REALIZATION_FAILED = "evidence_realization_failed"
    EXECUTOR_UNKNOWN_TOOL = "executor_unknown_tool"
    EXECUTOR_INVALID_TOOL_ARGUMENTS = "executor_invalid_tool_arguments"
    EXECUTOR_TOOL_OMISSION = "executor_tool_omission"
    EXECUTOR_PREMATURE_FINAL = "executor_premature_final"
    EXECUTOR_NO_PROGRESS = "executor_no_progress"
    EXECUTOR_SEQUENCE_EXHAUSTED = "executor_sequence_exhausted"


@dataclass(frozen=True)
class EvalBudget:
    max_steps: int = 8
    max_model_calls: int = 8
    max_tool_calls: int = 12
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= 64:
            raise ValueError("max_steps must be between 1 and 64")
        if not 1 <= self.max_model_calls <= 64:
            raise ValueError("max_model_calls must be between 1 and 64")
        if not 0 <= self.max_tool_calls <= 128:
            raise ValueError("max_tool_calls must be between 0 and 128")
        if not 100 <= self.timeout_ms <= 600_000:
            raise ValueError("timeout_ms must be between 100 and 600000")


@dataclass(frozen=True)
class EvalSetup:
    files: Mapping[str, str] = field(default_factory=dict)
    memories: tuple[str, ...] = ()
    skills: tuple[Mapping[str, Any], ...] = ()
    trust_policy: Mapping[str, Any] = field(default_factory=dict)
    fake_tools: tuple[Mapping[str, Any], ...] = ()
    script: tuple[Mapping[str, Any], ...] = ()
    clock: str | None = None


@dataclass(frozen=True)
class EvalExpectation:
    task_status: str | None = None
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    required_events: tuple[str, ...] = ()
    forbidden_events: tuple[str, ...] = ()
    expected_skill: str | None = None
    expected_artifacts: tuple[str, ...] = ()
    unchanged_artifacts: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    max_duplicate_writes: int = 0
    judge_rubric: str | None = None
    judge_required: bool = False
    verification_status: str | None = None
    ground_truth_success: bool | None = None
    expected_blocked: bool = False
    recoverable: bool = False
    recovery_success: bool | None = None
    injection_test: bool = False
    goal_verification_status: str | None = None

    def __post_init__(self) -> None:
        valid_tasks = {None, "planned", "running", "paused", "blocked", "failed", "completed", "cancelled"}
        if self.task_status not in valid_tasks:
            raise ValueError(f"invalid expected task_status: {self.task_status}")
        valid_verification = {None, "pass", "fail", "blocked", "skipped", "unknown"}
        if self.verification_status not in valid_verification:
            raise ValueError(f"invalid verification_status: {self.verification_status}")
        if self.max_duplicate_writes < 0:
            raise ValueError("max_duplicate_writes must not be negative")
        if self.judge_required and not self.judge_rubric:
            raise ValueError("judge_required needs judge_rubric")


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    goal: str
    setup: EvalSetup
    expected: EvalExpectation
    max_steps: int = 8
    max_tool_calls: int = 12
    max_model_calls: int = 8
    timeout_ms: int = 30_000
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.case_id):
            raise ValueError("case_id must be a safe 1-128 character identifier")
        if not _IDENTIFIER.fullmatch(self.category) or len(self.category) > 64:
            raise ValueError("category must be a safe 1-64 character identifier")
        if not self.goal or len(self.goal.encode("utf-8")) > 16_384:
            raise ValueError("goal must be non-empty and at most 16384 bytes")
        EvalBudget(
            self.max_steps, self.max_model_calls, self.max_tool_calls, self.timeout_ms
        )

    @property
    def budget(self) -> EvalBudget:
        return EvalBudget(
            self.max_steps, self.max_model_calls, self.max_tool_calls, self.timeout_ms
        )


@dataclass(frozen=True)
class EvalEvidence:
    task_status: str | None = None
    steps: tuple[Mapping[str, Any], ...] = ()
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    trust_decisions: tuple[Mapping[str, Any], ...] = ()
    action_executions: tuple[Mapping[str, Any], ...] = ()
    replay_events: tuple[Mapping[str, Any], ...] = ()
    selected_skills: tuple[str, ...] = ()
    verification_results: tuple[Mapping[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    changed_artifacts: tuple[str, ...] = ()
    checkpoints: tuple[Mapping[str, Any], ...] = ()
    final_output: str | None = None
    duration_ms: int = 0
    model_calls: int = 0
    tool_call_count: int = 0
    retry_count: int = 0
    recovery_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    plan_revisions: tuple[Mapping[str, Any], ...] = ()
    replan_count: int = 0
    replan_limit_blocked: bool = False
    goal_contract: Mapping[str, Any] | None = None
    goal_verifications: tuple[Mapping[str, Any], ...] = ()
    goal_verification_status: str | None = None
    constraint_violations: tuple[str, ...] = ()
    visible_tools: tuple[str, ...] = ()
    routed_capabilities: tuple[str, ...] = ()
    candidate_tool_count: int = 0
    task_id: str | None = None
    replay_run_ids: tuple[str, ...] = ()
    active_runtime_seconds: float = 0.0
    command_runtime_seconds: float = 0.0
    budget_exhausted: bool = False
    budget_exhausted_resource: str | None = None
    planned_steps: tuple[Mapping[str, Any], ...] = ()
    tool_requests: tuple[Mapping[str, Any], ...] = ()
    terminal_path: Mapping[str, Any] = field(default_factory=dict)
    terminal_stage: FailureStage | None = None
    terminal_reason: str = ""
    goal_verification_eligible: bool = False
    goal_verification_started: bool = False
    goal_verification_recorded: bool = False
    model_calls_contract: int = 0
    model_calls_planner: int = 0
    model_calls_agent: int = 0
    model_calls_step_verifier: int = 0
    model_calls_replanner: int = 0
    model_calls_goal_verifier: int = 0
    model_calls_other: int = 0
    step_verification_deterministic: bool = False
    step_verification_semantic: bool = False
    offline_fallback_used: bool = False
    offline_fallback_success: bool = False
    step_insufficient_evidence: bool = False
    deterministic_eligible: bool = False
    deterministic_avoided_model_call: bool = False
    plan_evidence_valid: bool = True
    required_evidence_produced: bool = False
    evidence_corrections_attempted: int = 0
    evidence_corrections_succeeded: int = 0
    required_tool_omissions: int = 0
    plan_capability_mismatches: int = 0
    execution_contract_complete: bool = False
    early_model_termination: bool = False
    partial_evidence_observed: bool = False
    continuation_turns: int = 0
    continuation_succeeded: bool = False
    continuation_exhausted: bool = False
    evidence_requirements_total: int = 0
    evidence_requirements_satisfied: int = 0
    total_execution_turns: int = 0
    recoverable_failures: int = 0
    recovery_replans_requested: int = 0
    recovery_replans_applied: int = 0
    recovery_strategies_rejected: int = 0
    recovery_exhausted: bool = False


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    category: str
    verdict: EvalVerdict
    deterministic_score: float
    judge_score: float | None
    metrics: Mapping[str, float | int | None]
    reasons: tuple[str, ...]
    failure_types: tuple[FailureType, ...]
    evidence: EvalEvidence
    judge_reasons: tuple[str, ...] = ()
    run_number: int = 1
    attempted: bool = True
    root_causes: tuple[RootCauseClass, ...] = ()


@dataclass(frozen=True)
class LiveEvalRunSummary:
    selected_cases: int
    attempted_cases: int
    completed_cases: int
    passed_cases: int
    failed_cases: int
    expected_blocked_cases: int
    unexpected_blocked_cases: int
    skipped_cases: int
    completeness: float
    status: LiveBaselineStatus
    runs_per_case: int = 1
    selected_case_runs: int = 0
    attempted_case_runs: int = 0
    completed_case_runs: int = 0
    scope: str = "full_corpus"
    full_corpus_cases: int = 0
    expected_pass_cases: int = 0
    passed_expected_pass_cases: int = 0
    expected_pass_completion_rate: float = 0.0


@dataclass(frozen=True)
class EvalRun:
    schema_version: int
    run_id: str
    tieru_version: str
    corpus_version: str
    corpus_hash: str
    mode: str
    started_at: str
    completed_at: str
    provider: str
    model: str
    configuration: Mapping[str, Any]
    results: tuple[EvalResult, ...]
    metrics: Mapping[str, Any]
    categories: Mapping[str, Mapping[str, Any]]
    failures: Mapping[str, int]
    reliability_pass: bool
    live_summary: LiveEvalRunSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

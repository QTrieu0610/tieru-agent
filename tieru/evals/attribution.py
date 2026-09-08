"""Evidence-based lifecycle and root-cause attribution for evaluation results."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from tieru.evals.models import (
    EvalCase,
    EvalEvidence,
    FailureStage,
    FailureType,
    RootCauseClass,
)
from tieru.memory.personal import redact_secrets
from tieru.tasks.models import DivergenceAttribution

_REFUSAL_STEP = re.compile(
    r"\b(?:cannot|can't|unable to|do not|must not)\b.{0,80}"
    r"\b(?:execute|perform|complete|call|use)\b.{0,40}\b(?:tool|action|task|file)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class FailureAttribution:
    stage: FailureStage | None
    reason: str
    root_causes: tuple[RootCauseClass, ...]
    trust_denials: tuple[dict[str, Any], ...]


def _payload(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    value = event.get("safe_payload")
    return value if isinstance(value, dict) else {}


def _requested_tools(evidence: EvalEvidence) -> tuple[str, ...]:
    requests = evidence.tool_requests or tuple(
        event
        for event in evidence.replay_events
        if event.get("event_type") == "tool_requested"
    )
    return tuple(
        dict.fromkeys(
            str(event.get("tool") or "")
            for event in requests
            if str(event.get("tool") or "")
        )
    )


def _tool_error_text(evidence: EvalEvidence) -> str:
    parts: list[str] = []
    for event in evidence.tool_calls:
        payload = _payload(event)
        parts.extend(
            str(value)
            for value in (
                payload.get("error_code"),
                payload.get("output_preview"),
                payload.get("reason"),
            )
            if value
        )
    return " ".join(parts).lower()


def _plan_text(evidence: EvalEvidence) -> str:
    return "\n".join(
        str(step.get(field) or "")
        for step in evidence.planned_steps
        for field in ("title", "instruction", "verification_instruction")
    )


def _classify_denial(
    case: EvalCase,
    evidence: EvalEvidence,
    event: dict[str, Any],
) -> tuple[RootCauseClass, dict[str, Any]]:
    payload = _payload(event)
    tool = str(event.get("tool") or payload.get("tool") or "")
    reasons = tuple(str(item) for item in payload.get("reason_codes") or ())
    operation = str(payload.get("operation") or "unknown")
    visible = set(evidence.visible_tools)
    required = set(case.expected.required_tools)
    approval_required = bool(
        payload.get("confirmation_required", payload.get("approval_required", False))
    )

    if case.expected.expected_blocked:
        cause = RootCauseClass.LEGITIMATE_SECURITY_DENIAL
    elif approval_required:
        cause = RootCauseClass.LEGITIMATE_CONFIRMATION_REQUIRED
    elif "policy_error" in reasons or "classifier_error" in reasons:
        cause = RootCauseClass.TRUST_POLICY_MISCONFIGURATION
    elif tool in required and tool in visible and operation in {"read", "list", "search"}:
        cause = RootCauseClass.TRUST_METADATA_MISMATCH
    elif tool not in visible or "unclassified_action" in reasons:
        cause = RootCauseClass.UNNECESSARY_HIGH_RISK_TOOL_SELECTED
    elif tool in required:
        cause = RootCauseClass.TRUST_POLICY_MISCONFIGURATION
    else:
        cause = RootCauseClass.WRONG_OPERATION_SELECTED

    detail = {
        "tool": tool,
        "decision": "deny",
        "policy": str(payload.get("matched_policy") or "unknown"),
        "operation": operation,
        "risk": str(payload.get("risk") or "unknown"),
        "confirmation_required": approval_required,
        "reason_code": reasons[0] if reasons else "unspecified",
        "reason_codes": list(reasons),
        "underlying_cause": cause.value,
    }
    return cause, detail


def attribute_failure(
    case: EvalCase,
    evidence: EvalEvidence,
    *,
    forced_failures: tuple[FailureType, ...] = (),
) -> FailureAttribution:
    """Classify the earliest supported failure without inferring a Trust event."""

    causes: list[RootCauseClass] = []
    denials: list[dict[str, Any]] = []
    denied_events = [
        event
        for event in evidence.trust_decisions
        if not bool(_payload(event).get("allowed"))
    ]
    for event in denied_events:
        cause, detail = _classify_denial(case, evidence, dict(event))
        causes.append(cause)
        denials.append(detail)

    visible = set(evidence.visible_tools)
    required = set(case.expected.required_tools)
    forbidden = set(case.expected.forbidden_tools)
    requested = set(_requested_tools(evidence))
    if required - visible and evidence.visible_tools:
        causes.append(RootCauseClass.CAPABILITY_ROUTER_UNDEREXPOSURE)
    if forbidden & visible:
        causes.append(RootCauseClass.CAPABILITY_ROUTER_OVEREXPOSURE)
    if required - requested:
        if _REFUSAL_STEP.search(_plan_text(evidence)):
            causes.append(RootCauseClass.PLANNER_STEP_MISMATCH)
        elif not requested:
            causes.append(RootCauseClass.INSUFFICIENT_STEP_EVIDENCE)
        else:
            causes.append(RootCauseClass.WRONG_OPERATION_SELECTED)

    tool_errors = _tool_error_text(evidence)
    if any(
        marker in tool_errors
        for marker in ("fileexistserror", "invalid_arguments", "missing required", "invalid operation")
    ):
        causes.append(RootCauseClass.TOOL_ARGUMENT_ERROR)

    last_verification = (
        str(evidence.verification_results[-1].get("status") or "")
        if evidence.verification_results
        else ""
    )
    if (
        last_verification in {"unknown", "blocked", "fail"}
        and not denied_events
        and not evidence.budget_exhausted
    ):
        term_reason = str(evidence.terminal_reason or "").lower()
        if "plan_evidence_gap" in term_reason or FailureType.PLAN_EVIDENCE_GAP in forced_failures:
            causes.append(RootCauseClass.PLAN_EVIDENCE_GAP)
        elif "plan_capability_mismatch" in term_reason or FailureType.PLAN_CAPABILITY_MISMATCH in forced_failures:
            causes.append(RootCauseClass.PLAN_CAPABILITY_MISMATCH)
        elif "required_tool_not_invoked" in term_reason or FailureType.REQUIRED_TOOL_NOT_INVOKED in forced_failures:
            causes.append(RootCauseClass.REQUIRED_TOOL_NOT_INVOKED)
        elif "evidence_correction_exhausted" in term_reason or FailureType.EVIDENCE_CORRECTION_EXHAUSTED in forced_failures:
            causes.append(RootCauseClass.EVIDENCE_CORRECTION_EXHAUSTED)
        elif "semantic_verifier_unavailable" in term_reason:
            causes.append(RootCauseClass.SEMANTIC_VERIFIER_UNAVAILABLE)
        elif "semantic" in term_reason:
            causes.append(RootCauseClass.SEMANTIC_VERIFICATION_FAILED)
        elif "deterministic" in term_reason or "command failed" in term_reason:
            causes.append(RootCauseClass.DETERMINISTIC_VERIFICATION_FAILED)
        else:
            causes.append(RootCauseClass.INSUFFICIENT_STEP_EVIDENCE)

    stage = evidence.terminal_stage
    if any(item.value.startswith("provider_") for item in forced_failures):
        stage = FailureStage.PROVIDER
        causes.append(RootCauseClass.PROVIDER_FAILURE)
    elif evidence.budget_exhausted:
        stage = FailureStage.BUDGET
    elif denied_events:
        stage = FailureStage.TRUST
    elif "tool_" in tool_errors and evidence.task_status != "completed":
        stage = FailureStage.TOOL_EXECUTION
    elif evidence.goal_verification_recorded and evidence.task_status != "completed":
        stage = FailureStage.GOAL_VERIFICATION
    elif last_verification in {"fail", "blocked", "unknown"}:
        stage = FailureStage.STEP_VERIFICATION
    elif not evidence.planned_steps:
        stage = FailureStage.PLANNING

    reason = evidence.terminal_reason
    if not reason and denials:
        reason = f"Trust denied {denials[-1]['tool']}: {denials[-1]['reason_code']}"
    if not reason and tool_errors:
        reason = tool_errors[:300]
    if not reason and stage is not None:
        reason = f"task stopped at {stage.value}"
    reason = redact_secrets(reason)[:512]
    return FailureAttribution(
        stage=stage,
        reason=reason,
        root_causes=tuple(dict.fromkeys(causes)),
        trust_denials=tuple(denials),
    )


def parsed_error(output: Any) -> dict[str, Any]:
    """Return bounded structured tool-error metadata for deterministic tests/reporting."""

    try:
        value = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    error = value.get("error") if isinstance(value, dict) else None
    return error if isinstance(error, dict) else {}


@dataclass(frozen=True)
class FirstDivergenceRecord:
    case_id: str
    m32_verdict: str
    m34_verdict: str
    first_divergence_stage: str
    first_divergence_role: str
    m32_model: str
    m34_model: str
    checkpoint_state: str
    budget_state: str
    trust_state: str
    primary_attribution: DivergenceAttribution
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "m32_verdict": self.m32_verdict,
            "m34_verdict": self.m34_verdict,
            "first_divergence_stage": self.first_divergence_stage,
            "first_divergence_role": self.first_divergence_role,
            "m32_model": self.m32_model,
            "m34_model": self.m34_model,
            "checkpoint_state": self.checkpoint_state,
            "budget_state": self.budget_state,
            "trust_state": self.trust_state,
            "primary_attribution": (
                self.primary_attribution.value
                if isinstance(self.primary_attribution, DivergenceAttribution)
                else str(self.primary_attribution)
            ),
            "evidence": self.evidence,
        }


def attribute_first_divergence(
    m32_item: dict[str, Any],
    m34_item: dict[str, Any],
) -> FirstDivergenceRecord:
    """Determine the earliest causal lifecycle divergence and primary attribution between two milestone runs."""
    case_id = str(m34_item.get("case_id") or m32_item.get("case_id") or "unknown")
    v32 = str(m32_item.get("verdict") or "unknown")
    v34 = str(m34_item.get("verdict") or "unknown")

    ev32 = m32_item.get("evidence") or {}
    ev34 = m34_item.get("evidence") or {}

    m32_model = str(ev32.get("model") or m32_item.get("model") or "gemma4:e2b")
    m34_model = str(ev34.get("model") or m34_item.get("model") or "gemma4:e2b")

    # Inspect checkpoint state
    cps34 = ev34.get("checkpoints") or ()
    cp_state = "present" if cps34 else "missing"

    # Inspect budget state
    b_state = "exhausted" if ev34.get("budget_exhausted") else "nominal"

    # Inspect trust state
    t_state = "denied" if ev34.get("trust_denials") else "nominal"

    # Failure types and reasons
    fail_types = {str(f) for f in (m34_item.get("failure_types") or ())}
    reasons = " ".join(str(r) for r in (m34_item.get("reasons") or ()))
    term_reason = str(ev34.get("terminal_reason") or "")

    # Attribution classification
    if not m34_item.get("attempted", True) or "circuit_breaker" in reasons or "provider_failure_threshold" in reasons:
        stage = "provider_preflight"
        role = "system"
        attr = DivergenceAttribution.PROVIDER
        evidence_text = "Case was not executed due to provider-failure circuit breaker tripping."
    elif "denominator" in reasons or "expected_pass_completion_rate" in reasons:
        stage = "aggregation"
        role = "eval"
        attr = DivergenceAttribution.EVAL_METRICS
        evidence_text = "Metric aggregation used partial denominator instead of full corpus."
    elif ev34.get("budget_exhausted"):
        stage = "execution"
        role = "executor"
        attr = DivergenceAttribution.BUDGET
        evidence_text = f"Resource budget exhausted before step completion: {term_reason}"
    elif t_state == "denied":
        stage = "trust_gate"
        role = "trust"
        attr = DivergenceAttribution.TRUST_POLICY
        evidence_text = f"Tool call rejected by Trust policy: {reasons}"
    elif "capability" in reasons or FailureType.CAPABILITY_ROUTING_ERROR.value in fail_types or FailureType.REQUIRED_TOOL_HIDDEN.value in fail_types:
        stage = "capability_routing"
        role = "router"
        attr = DivergenceAttribution.CAPABILITY_ROUTING
        evidence_text = f"Required tool hidden or forbidden tool exposed by capability routing: {reasons}"
    elif "checkpoint" in reasons or "checkpoint_not_" in reasons:
        stage = "checkpoint_pipeline"
        role = "runtime"
        attr = DivergenceAttribution.CHECKPOINT_PIPELINE
        evidence_text = f"Checkpoint evidence missing or dropped in pipeline: {reasons}"
    elif "role_routing" in reasons or m34_item.get("role_mismatch"):
        stage = "model_selection"
        role = "fabric"
        attr = DivergenceAttribution.ROLE_ROUTING
        evidence_text = f"Role routed to unprofiled or unintended model: {reasons}"
    elif "Malformed verifier response" in term_reason or "semantic_verifier_unavailable" in term_reason or "step_verification" in term_reason:
        stage = "step_verification"
        role = "step_verifier"
        attr = DivergenceAttribution.VERIFICATION
        evidence_text = f"Step verifier encountered malformed response or evaluation error: {term_reason}"
    elif "fixture" in reasons or "setup" in reasons:
        stage = "fixture_setup"
        role = "eval_runner"
        attr = DivergenceAttribution.FIXTURE
        evidence_text = f"Test workspace setup or fixture preparation failed: {reasons}"
    elif v32 == "pass" and v34 != "pass":
        stage = "step_execution"
        role = "executor"
        attr = DivergenceAttribution.MODEL_QUALITY
        evidence_text = f"Model produced non-functional prose or malformed tool call where M32 succeeded: {reasons or term_reason}"
    else:
        stage = "unknown"
        role = "unknown"
        attr = DivergenceAttribution.UNKNOWN
        evidence_text = f"Divergence could not be conclusively determined: {reasons}"

    return FirstDivergenceRecord(
        case_id=case_id,
        m32_verdict=v32,
        m34_verdict=v34,
        first_divergence_stage=stage,
        first_divergence_role=role,
        m32_model=m32_model,
        m34_model=m34_model,
        checkpoint_state=cp_state,
        budget_state=b_state,
        trust_state=t_state,
        primary_attribution=attr,
        evidence=evidence_text,
    )


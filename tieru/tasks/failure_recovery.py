"""Deterministic step failure classification and recovery replanning activation."""

from __future__ import annotations

import re
from typing import Any

from tieru.memory.personal import redact_secrets
from tieru.tasks.models import (
    PlanStep,
    StepExecution,
    StepFailureAssessment,
    StepFailureDisposition,
    Task,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:api[_ -]?key|password|secret|token)\s*(?:[:=]|is)?\s*\S+", re.IGNORECASE),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs]|bearer_tok|api_key|token)[-_][A-Za-z0-9_-]{6,}\b", re.IGNORECASE),
    re.compile(r"\b(?:bearer\s+[A-Za-z0-9_.-]{12,})\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def safe_redact_secrets(text: str) -> str:
    if not text:
        return text
    text = redact_secrets(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED SECRET]", text)
    return text


def compute_strategy_fingerprint(step: PlanStep | TaskStep) -> str:
    """Compute a coarse normalized fingerprint of a step strategy.

    Coarsely captures (execution_kind, target_files/commands) to prevent
    the agent from cycling through identical failing approaches.
    """
    kind = step.execution_kind.value if getattr(step, "execution_kind", None) else "unknown"
    targets: list[str] = []
    text = f"{step.title} {step.instruction}".lower()
    for token in text.split():
        clean = token.strip("`'\",:;()[]{}")
        if any(clean.endswith(ext) for ext in (".py", ".json", ".ini", ".sql", ".txt", ".md", ".sh", ".yaml", ".yml", ".toml")) or clean in {"pytest", "python", "git", "cat", "ls", "grep", "sed", "echo"}:
            targets.append(clean)
    targets_str = ",".join(sorted(set(targets)))
    return f"{kind}:{targets_str}"


def extract_bounded_failure_evidence(
    task: Task,
    step: TaskStep,
    execution: StepExecution,
    verification: VerificationResult,
) -> dict[str, Any]:
    """Extract bounded observable failure evidence for plan review.

    Never passes raw CoT, hidden prompt text, full replay events, unbounded stdout,
    or unredacted secrets.
    """
    tool_summaries: list[dict[str, Any]] = []
    command_exit_code: int | None = None
    for tc in execution.tool_calls:
        t_name = str(tc.get("tool") or "")
        err_code = str(tc.get("error_code") or "")
        tool_summaries.append(
            {
                "tool": t_name,
                "error_code": err_code,
                "success": not bool(err_code),
            }
        )
        if t_name == "run_command":
            res_obj = tc.get("result")
            if isinstance(res_obj, dict) and "exit_code" in res_obj:
                command_exit_code = res_obj["exit_code"]

    safe_result = safe_redact_secrets(execution.result)[:2048]
    safe_v_summary = safe_redact_secrets(verification.summary)[:1024]
    fingerprint = compute_strategy_fingerprint(step)

    return {
        "step_id": step.step_id,
        "position": step.position,
        "title": step.title,
        "instruction": step.instruction,
        "verification_instruction": step.verification_instruction,
        "execution_kind": step.execution_kind.value if step.execution_kind else "unknown",
        "verification_status": verification.status.value,
        "verification_summary": safe_v_summary,
        "tool_calls": tool_summaries,
        "command_exit_code": command_exit_code,
        "result_preview": safe_result,
        "strategy_fingerprint": fingerprint,
    }


class StepFailureClassifier:
    """Classify step verification failures into bounded recovery dispositions.

    Deterministic first; zero tools. Distinguishes:
    - RETRY_SAME_STEP: recoverable step precondition within continuation budget
    - REPLAN: disproven plan assumption or failed execution with alternative paths
    - BLOCK: hard policy denial, budget exhaustion, or uncertain ledger
    - FAIL: irreversible constraint violation, exhausted replans, or repeated strategy
    """

    def classify(
        self,
        task: Task,
        step: TaskStep,
        execution: StepExecution,
        verification: VerificationResult,
        *,
        replan_count: int,
        effective_max_replans: int,
        remaining_model_calls: float | None = None,
        previous_failed_fingerprints: set[str] | None = None,
    ) -> StepFailureAssessment:
        fingerprint = compute_strategy_fingerprint(step)
        failed_prints = previous_failed_fingerprints or set()
        v_summary_lower = (verification.summary or "").lower()
        exec_result_lower = (execution.result or "").lower()

        # 1. Hard Policy or Resource Blocks
        if verification.status is VerificationStatus.BLOCKED:
            if "budget_exhausted" in v_summary_lower or "budget_exhausted" in exec_result_lower:
                return StepFailureAssessment(
                    disposition=StepFailureDisposition.BLOCK,
                    reason_code="budget_exhausted",
                    evidence_summary="Resource budget exhausted; cannot replan.",
                    is_recoverable=False,
                    strategy_fingerprint=fingerprint,
                )
            if "trust" in v_summary_lower or "denied" in v_summary_lower:
                return StepFailureAssessment(
                    disposition=StepFailureDisposition.BLOCK,
                    reason_code="hard_trust_denial",
                    evidence_summary="Hard Trust policy denial cannot be bypassed through replanning.",
                    is_recoverable=False,
                    strategy_fingerprint=fingerprint,
                )
            return StepFailureAssessment(
                disposition=StepFailureDisposition.BLOCK,
                reason_code="step_blocked",
                evidence_summary=verification.summary or "Step execution blocked.",
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 2. Check Action Ledger Uncertainty
        for tc in execution.tool_calls:
            err = str(tc.get("error_code") or "").lower()
            if "uncertain" in err or "action_ledger_uncertain" in err:
                return StepFailureAssessment(
                    disposition=StepFailureDisposition.BLOCK,
                    reason_code="action_ledger_uncertain",
                    evidence_summary="Action Ledger uncertainty requires Human Recovery before continuation.",
                    is_recoverable=False,
                    strategy_fingerprint=fingerprint,
                )

        # 3. Check for Budget Exhaustion in result or tools
        if "budget_exhausted:" in exec_result_lower:
            return StepFailureAssessment(
                disposition=StepFailureDisposition.BLOCK,
                reason_code="budget_exhausted",
                evidence_summary="Model call budget exhausted during step execution.",
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 4. Check for Exhausted Replan Budget
        if replan_count >= effective_max_replans:
            return StepFailureAssessment(
                disposition=StepFailureDisposition.FAIL,
                reason_code="replan_budget_exhausted",
                evidence_summary=f"Task reached maximum allowed replans ({effective_max_replans}).",
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 5. Check if Model Calls Remain for Replanning
        if remaining_model_calls is not None and remaining_model_calls < 1.0:
            return StepFailureAssessment(
                disposition=StepFailureDisposition.BLOCK,
                reason_code="budget_exhausted:model_calls",
                evidence_summary="Insufficient model call budget remaining to formulate a plan revision.",
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 6. Check for Repeated Failed Strategy
        if fingerprint in failed_prints and fingerprint != "unknown:":
            return StepFailureAssessment(
                disposition=StepFailureDisposition.FAIL,
                reason_code="repeated_failed_strategy",
                evidence_summary=f"Strategy '{fingerprint}' was already attempted and failed.",
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 7. Check for Irreversible User Constraint Violations
        if any(
            pattern in v_summary_lower
            for pattern in ("constraint_violated", "forbidden_tool_used", "forbidden_artifact_modified")
        ):
            return StepFailureAssessment(
                disposition=StepFailureDisposition.FAIL,
                reason_code="constraint_violation",
                evidence_summary=verification.summary,
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 8. Unsubstantiated Assistant Self-Claim (no observable evidence)
        if any(
            pattern in v_summary_lower
            for pattern in ("self-claim", "assistant prose alone", "text_only_claim")
        ):
            return StepFailureAssessment(
                disposition=StepFailureDisposition.FAIL,
                reason_code="unsubstantiated_self_claim",
                evidence_summary=verification.summary or "Assistant self-claim without observable evidence.",
                is_recoverable=False,
                strategy_fingerprint=fingerprint,
            )

        # 9. Step Verification Failure with Alternative Path Available (Recoverable)
        if verification.status is VerificationStatus.FAIL:
            reason_code = "step_verification_failed"
            if "command_exit_zero" in v_summary_lower or "exit code" in v_summary_lower:
                reason_code = "command_test_failure"
            elif "artifact_changed" in v_summary_lower or "artifact" in v_summary_lower:
                reason_code = "artifact_verification_failure"
            elif "tool_success" in v_summary_lower:
                reason_code = "tool_execution_failure"

            return StepFailureAssessment(
                disposition=StepFailureDisposition.REPLAN,
                reason_code=reason_code,
                evidence_summary=verification.summary or "Observable evidence disproved current step assumption.",
                is_recoverable=True,
                strategy_fingerprint=fingerprint,
            )

        # Default fallback
        return StepFailureAssessment(
            disposition=StepFailureDisposition.FAIL,
            reason_code="unknown_failure",
            evidence_summary=verification.summary or "Unclassified step failure.",
            is_recoverable=False,
            strategy_fingerprint=fingerprint,
        )

"""Task-Level Goal Verifier subsystem for M23 — Goal Contract & Task-Level Success Verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol
from uuid import uuid4

from tieru.context.builder import ContextBuilder
from tieru.tasks.models import (
    BudgetResource,
    CriterionResult,
    CriterionStatus,
    GoalContract,
    GoalVerificationStatus,
    ModelCallCriticality,
    ModelCallPurpose,
    StepStatus,
    Task,
    TaskGoalVerification,
    TaskStep,
    VerificationStatus,
)


def _text_from_response(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(getattr(block, "text", ""))
            for block in content
            if getattr(block, "type", "text") == "text"
        )
    return str(content or "")

GOAL_JUDGE_SYSTEM_PROMPT = """You are Tieru's Goal Success Verifier.
Your job is to evaluate whether the observable execution evidence conclusively proves that the user's original task goal and success criteria have been satisfied without violating any constraints.

Rules:
1. You have ZERO tools. You cannot run commands, inspect files, or take actions.
2. The user's original goal and explicit constraints are authoritative.
3. Assistant self-claims (e.g., "I fixed it", "All done") are NOT evidence. Only observable test outputs, command outputs, exit codes, file diffs, and verification checkpoints count as evidence.
4. If observable evidence is absent or insufficient, return UNKNOWN or FAIL. NEVER assume success.
5. Return ONLY a valid JSON object matching this schema:
{
  "status": "PASS", // One of: "PASS", "FAIL_REPLANABLE", "FAIL_TERMINAL", "BLOCKED", "UNKNOWN"
  "summary": "Concise summary of verification outcome",
  "criterion_results": [
    {
      "criterion_id": "sc1",
      "status": "PASS", // One of: "PASS", "FAIL", "UNKNOWN", "BLOCKED"
      "reason": "Observable evidence proves condition"
    }
  ]
}
"""


def compute_evidence_hash(evidence_data: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 fingerprint over a canonical evidence dictionary.

    The evidence hash provides a stable, reproducible integrity fingerprint of the
    observable execution evidence snapshot used by a verification record. It supports
    deduplication, audit correlation, change detection, and verification idempotency.
    It does not constitute a digital signature and does not provide non-repudiation
    without an external trusted cryptographic signature.
    """
    canonical_text = json.dumps(
        evidence_data,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


class TaskGoalVerifier(Protocol):
    def verify(
        self,
        task: Task,
        contract: GoalContract,
        steps: list[TaskStep],
        *,
        execution_evidence: dict[str, Any] | None = None,
    ) -> TaskGoalVerification:
        """Evaluate task-level success against Goal Contract and observable evidence."""
        ...


class DeterministicGoalVerifier:
    """Strictly deterministic, read-only goal verifier enforcing safety and constraint invariants."""

    def verify(
        self,
        task: Task,
        contract: GoalContract,
        steps: list[TaskStep],
        *,
        execution_evidence: dict[str, Any] | None = None,
    ) -> TaskGoalVerification:
        evidence = execution_evidence or {}
        evidence_hash = compute_evidence_hash(evidence)

        # 1. Action Ledger / Recovery uncertainty check
        if evidence.get("has_uncertain_action") or evidence.get("unresolved_recovery"):
            return TaskGoalVerification(
                verification_id=f"ver_{uuid4().hex[:12]}",
                task_id=task.task_id,
                status=GoalVerificationStatus.BLOCKED,
                criterion_results=tuple(
                    CriterionResult(
                        criterion_id=sc.criterion_id,
                        status=CriterionStatus.BLOCKED,
                        evidence_summary="Unresolved action uncertainty or recovery permit required.",
                    )
                    for sc in contract.success_criteria
                ),
                summary="Blocked by unresolved action uncertainty in Action Ledger.",
                evidence_hash=evidence_hash,
            )

        # 2. Check for false green / test skip / xfail in diff or output
        diff_text = str(evidence.get("git_diff") or "")
        test_output = str(evidence.get("test_output") or "")
        test_skipped = bool(
            evidence.get("tests_skipped")
            or "xfail" in diff_text
            or "skip" in diff_text.lower()
            or "pytest.mark.skip" in diff_text
            or "pytest.mark.xfail" in diff_text
            or " skipped" in test_output
            or " xfailed" in test_output
        )

        for constraint in contract.constraints:
            cdesc = constraint.description.lower()
            # Test skip / delete constraint check (English & Vietnamese)
            test_skip_terms = (
                "skip", "disable", "delete test", "remove test",
                "xóa test", "bỏ test", "skip test", "đừng xóa", "không xóa test",
                "xfail",
            )
            if any(term in cdesc for term in test_skip_terms) and test_skipped:
                return TaskGoalVerification(
                    verification_id=f"ver_{uuid4().hex[:12]}",
                    task_id=task.task_id,
                    status=GoalVerificationStatus.FAIL_TERMINAL,
                    criterion_results=tuple(
                        CriterionResult(
                            criterion_id=sc.criterion_id,
                            status=CriterionStatus.FAIL,
                            evidence_summary=f"Constraint violated: {constraint.description}",
                        )
                        for sc in contract.success_criteria
                    ),
                    summary=f"Constraint violation: {constraint.description} (tests disabled or skipped).",
                    evidence_hash=evidence_hash,
                )

            # Public API constraint check (English & Vietnamese)
            api_terms = ("public api", "api công khai", "thay đổi api", "thay đổi public api")
            if any(term in cdesc for term in api_terms) and (
                evidence.get("public_api_modified")
                or evidence.get("api_modified")
            ):
                return TaskGoalVerification(
                    verification_id=f"ver_{uuid4().hex[:12]}",
                    task_id=task.task_id,
                    status=GoalVerificationStatus.FAIL_TERMINAL,
                    criterion_results=tuple(
                        CriterionResult(
                            criterion_id=sc.criterion_id,
                            status=CriterionStatus.FAIL,
                            evidence_summary=f"Constraint violated: {constraint.description}",
                        )
                        for sc in contract.success_criteria
                    ),
                    summary=f"Constraint violation: {constraint.description} (public API modified).",
                    evidence_hash=evidence_hash,
                )

            # Scope / path constraint check (English & Vietnamese)
            scope_terms = ("only", "chỉ", "chỉ được", "chỉ sửa", "giới hạn")
            if any(term in cdesc for term in scope_terms) and (
                evidence.get("unrelated_file_modified")
                or evidence.get("scope_violated")
                or evidence.get("forbidden_file_modified")
            ):
                return TaskGoalVerification(
                    verification_id=f"ver_{uuid4().hex[:12]}",
                    task_id=task.task_id,
                    status=GoalVerificationStatus.FAIL_TERMINAL,
                    criterion_results=tuple(
                        CriterionResult(
                            criterion_id=sc.criterion_id,
                            status=CriterionStatus.FAIL,
                            evidence_summary=f"Constraint violated: {constraint.description}",
                        )
                        for sc in contract.success_criteria
                    ),
                    summary=f"Constraint violation: {constraint.description} (unrelated files modified).",
                    evidence_hash=evidence_hash,
                )

            # Backward compatibility constraint check (English & Vietnamese)
            compat_terms = ("backward compatibility", "tương thích ngược", "giữ tương thích", "compatibility")
            if any(term in cdesc for term in compat_terms) and evidence.get("backward_compatibility_broken"):
                return TaskGoalVerification(
                    verification_id=f"ver_{uuid4().hex[:12]}",
                    task_id=task.task_id,
                    status=GoalVerificationStatus.FAIL_TERMINAL,
                    criterion_results=tuple(
                        CriterionResult(
                            criterion_id=sc.criterion_id,
                            status=CriterionStatus.FAIL,
                            evidence_summary=f"Constraint violated: {constraint.description}",
                        )
                        for sc in contract.success_criteria
                    ),
                    summary=f"Constraint violation: {constraint.description} (backward compatibility broken).",
                    evidence_hash=evidence_hash,
                )

        # 3. Check ground truth failure if explicitly passed in evidence (e.g. in evals)
        if evidence.get("ground_truth_reproduction_failed"):
            return TaskGoalVerification(
                verification_id=f"ver_{uuid4().hex[:12]}",
                task_id=task.task_id,
                status=GoalVerificationStatus.FAIL_REPLANABLE,
                criterion_results=tuple(
                    CriterionResult(
                        criterion_id=sc.criterion_id,
                        status=CriterionStatus.FAIL,
                        evidence_summary="Ground truth reproduction failed.",
                    )
                    for sc in contract.success_criteria
                ),
                summary="Goal verification failed: reproduction still failing despite step passes.",
                evidence_hash=evidence_hash,
            )

        # 4. Check for assistant text-only claim without observable evidence
        has_observable_evidence = bool(
            evidence.get("command_exit_code") is not None
            or evidence.get("test_results")
            or evidence.get("modified_files")
            or any(s.verification_status is VerificationStatus.PASS for s in steps)
        )
        if evidence.get("text_only_claim") or not has_observable_evidence:
            return TaskGoalVerification(
                verification_id=f"ver_{uuid4().hex[:12]}",
                task_id=task.task_id,
                status=GoalVerificationStatus.UNKNOWN,
                criterion_results=tuple(
                    CriterionResult(
                        criterion_id=sc.criterion_id,
                        status=CriterionStatus.UNKNOWN,
                        evidence_summary="Assistant prose alone without observable tool/test evidence.",
                    )
                    for sc in contract.success_criteria
                ),
                summary="Insufficient observable evidence: assistant self-claim rejected.",
                evidence_hash=evidence_hash,
            )

        # 5. Evaluate each criterion against steps and evidence
        criterion_results: list[CriterionResult] = []
        all_passed = True
        has_unknown = False
        recovered_step_ids = set(evidence.get("recovered_step_ids") or [])
        if not recovered_step_ids:
            rev_positions = [s.position for s in steps if getattr(s, "plan_revision_id", None)]
            if rev_positions:
                min_rev_pos = min(rev_positions)
                recovered_step_ids = {
                    s.step_id for s in steps
                    if s.status is StepStatus.FAILED and s.position < min_rev_pos
                }

        active_steps = [
            s for s in steps
            if s.status is not StepStatus.SUPERSEDED
            and s.step_id not in recovered_step_ids
        ]
        steps_succeeded = all(s.status is StepStatus.SUCCEEDED for s in active_steps) if active_steps else False

        for sc in contract.success_criteria:
            # Check if evidence explicitly marks this criterion
            if "criterion_outcomes" in evidence and sc.criterion_id in evidence["criterion_outcomes"]:
                outcome = str(evidence["criterion_outcomes"][sc.criterion_id]).lower()
                if outcome == "pass":
                    criterion_results.append(
                        CriterionResult(sc.criterion_id, CriterionStatus.PASS, "Supported by evidence.")
                    )
                elif outcome == "fail":
                    criterion_results.append(
                        CriterionResult(sc.criterion_id, CriterionStatus.FAIL, "Failed by evidence.")
                    )
                    all_passed = False
                elif outcome == "blocked":
                    criterion_results.append(
                        CriterionResult(sc.criterion_id, CriterionStatus.BLOCKED, "Blocked by evidence.")
                    )
                    all_passed = False
                else:
                    criterion_results.append(
                        CriterionResult(sc.criterion_id, CriterionStatus.UNKNOWN, "Unknown evidence.")
                    )
                    all_passed = False
                    has_unknown = True
                continue

            # Deterministic evaluation based on steps
            if steps_succeeded:
                criterion_results.append(
                    CriterionResult(
                        sc.criterion_id,
                        CriterionStatus.PASS,
                        "All active plan steps completed and verified successfully.",
                    )
                )
            else:
                criterion_results.append(
                    CriterionResult(
                        sc.criterion_id,
                        CriterionStatus.FAIL,
                        "Incomplete or unverified plan steps.",
                    )
                )
                all_passed = False

        if all_passed and criterion_results:
            status = GoalVerificationStatus.PASS
            summary = "All success criteria and constraints verified with observable evidence."
        elif has_unknown:
            status = GoalVerificationStatus.UNKNOWN
            summary = "Observable evidence is insufficient to verify goal achievement."
        else:
            status = GoalVerificationStatus.FAIL_REPLANABLE
            summary = "One or more success criteria failed verification."

        return TaskGoalVerification(
            verification_id=f"ver_{uuid4().hex[:12]}",
            task_id=task.task_id,
            status=status,
            criterion_results=tuple(criterion_results),
            summary=summary,
            evidence_hash=evidence_hash,
        )


class ModelGoalJudge:
    """Tool-free LLM judge for evaluating semantic evidence under Context Firewall."""

    def __init__(self, model_router: Any, role: str = "small", *, store: Any = None) -> None:
        self.model_router = model_router
        self.role = role
        self.store = store

    def evaluate(
        self,
        goal: str,
        contract: GoalContract,
        evidence: dict[str, Any],
    ) -> TaskGoalVerification:
        if self.store is not None and getattr(contract, "task_id", None):
            res_m = self.store.reserve_budget(
                contract.task_id,
                BudgetResource.MODEL_CALLS,
                1.0,
                criticality=ModelCallCriticality.REQUIRED,
                purpose=ModelCallPurpose.GOAL_VERIFICATION,
            )
            if not res_m.allowed:
                return TaskGoalVerification(
                    verification_id=f"ver_{uuid4().hex[:12]}",
                    task_id=contract.task_id,
                    status=GoalVerificationStatus.BLOCKED,
                    criterion_results=(),
                    summary="budget_exhausted:model_calls",
                    evidence_hash=compute_evidence_hash(evidence),
                )
            res_v = self.store.reserve_budget(contract.task_id, BudgetResource.VERIFICATION_CALLS, 1.0)
            if not res_v.allowed:
                return TaskGoalVerification(
                    verification_id=f"ver_{uuid4().hex[:12]}",
                    task_id=contract.task_id,
                    status=GoalVerificationStatus.BLOCKED,
                    criterion_results=(),
                    summary="budget_exhausted:verification_calls",
                    evidence_hash=compute_evidence_hash(evidence),
                )
        builder = ContextBuilder()
        builder.add_control(GOAL_JUDGE_SYSTEM_PROMPT, source="goal_judge")

        user_content = f"User Goal:\n{goal}\n\nConstraints:\n" + "\n".join(
            f"- [{c.constraint_id}] {c.description}" for c in contract.constraints
        )
        builder.add_user(user_content, source="user_goal")

        evidence_payload = {
            "success_criteria": [
                {"id": sc.criterion_id, "description": sc.description, "kind": sc.verification_kind}
                for sc in contract.success_criteria
            ],
            "observable_evidence": evidence,
        }
        builder.add_data(
            json.dumps(evidence_payload, indent=2),
            source="task_evidence",
        )

        assembly = builder.build()

        try:
            client = self.model_router.client(self.role)
            response = client.messages.create(
                model=self.model_router.model(self.role),
                system=assembly.system,
                messages=list(assembly.messages),
                tools=[],
                max_tokens=1500,
            )
            if self.store is not None and getattr(contract, "task_id", None) and hasattr(response, "usage") and response.usage:
                in_t = getattr(response.usage, "input_tokens", None)
                out_t = getattr(response.usage, "output_tokens", None)
                if in_t is not None:
                    self.store.record_budget_consumption(contract.task_id, BudgetResource.INPUT_TOKENS, in_t)
                if out_t is not None:
                    self.store.record_budget_consumption(contract.task_id, BudgetResource.OUTPUT_TOKENS, out_t)
            text = _text_from_response(getattr(response, "content", response))
            return self._parse_judge_output(contract, text, evidence)
        except Exception:
            return self._parse_judge_output(contract, "", evidence)

    def _parse_judge_output(
        self,
        contract: GoalContract,
        text: str,
        evidence: dict[str, Any],
    ) -> TaskGoalVerification:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        evidence_hash = compute_evidence_hash(evidence)
        try:
            data = json.loads(cleaned)
            raw_status = str(data.get("status") or "UNKNOWN").lower()
            summary = str(data.get("summary") or "Model judge verification").strip()
            status_map = {
                "pass": GoalVerificationStatus.PASS,
                "fail_replanable": GoalVerificationStatus.FAIL_REPLANABLE,
                "fail_terminal": GoalVerificationStatus.FAIL_TERMINAL,
                "blocked": GoalVerificationStatus.BLOCKED,
                "unknown": GoalVerificationStatus.UNKNOWN,
            }
            status = status_map.get(raw_status, GoalVerificationStatus.UNKNOWN)

            c_results: list[CriterionResult] = []
            for sc in contract.success_criteria:
                matched = next(
                    (
                        item
                        for item in data.get("criterion_results", [])
                        if str(item.get("criterion_id", "")).lower() == sc.criterion_id.lower()
                    ),
                    None,
                )
                if matched:
                    c_status_str = str(matched.get("status") or "unknown").lower()
                    c_status = {
                        "pass": CriterionStatus.PASS,
                        "fail": CriterionStatus.FAIL,
                        "blocked": CriterionStatus.BLOCKED,
                    }.get(c_status_str, CriterionStatus.UNKNOWN)
                    c_reason = str(matched.get("reason") or "").strip()
                else:
                    c_status = (
                        CriterionStatus.PASS if status is GoalVerificationStatus.PASS else CriterionStatus.UNKNOWN
                    )
                    c_reason = summary
                c_results.append(CriterionResult(sc.criterion_id, c_status, c_reason))

            return TaskGoalVerification(
                verification_id=f"ver_{uuid4().hex[:12]}",
                task_id=contract.task_id,
                status=status,
                criterion_results=tuple(c_results),
                summary=summary,
                evidence_hash=evidence_hash,
            )
        except Exception:
            # Fallback safely to UNKNOWN / BLOCKED on malformed judge output
            return TaskGoalVerification(
                verification_id=f"ver_{uuid4().hex[:12]}",
                task_id=contract.task_id,
                status=GoalVerificationStatus.UNKNOWN,
                criterion_results=tuple(
                    CriterionResult(sc.criterion_id, CriterionStatus.UNKNOWN, "Malformed judge response")
                    for sc in contract.success_criteria
                ),
                summary="Model judge returned malformed output; treated as UNKNOWN.",
                evidence_hash=evidence_hash,
            )


class LayeredTaskGoalVerifier:
    """Two-tiered verifier: deterministic checks first, optional tool-free model judge fallback."""

    def __init__(
        self,
        judge: ModelGoalJudge | None = None,
        deterministic_verifier: DeterministicGoalVerifier | None = None,
    ) -> None:
        self.judge = judge
        self.deterministic_verifier = deterministic_verifier or DeterministicGoalVerifier()

    def verify(
        self,
        task: Task,
        contract: GoalContract,
        steps: list[TaskStep],
        *,
        execution_evidence: dict[str, Any] | None = None,
    ) -> TaskGoalVerification:
        det_result = self.deterministic_verifier.verify(
            task, contract, steps, execution_evidence=execution_evidence
        )
        # If deterministic check produced FAIL_TERMINAL or BLOCKED, never override
        if det_result.status in {GoalVerificationStatus.FAIL_TERMINAL, GoalVerificationStatus.BLOCKED}:
            return det_result

        # If deterministic check conclusively passed, no model judge needed
        if det_result.status is GoalVerificationStatus.PASS and not any(
            sc.verification_kind == "model_judge" for sc in contract.success_criteria
        ):
            return det_result

        # If model judge is available and needed
        if self.judge is not None:
            judge_res = self.judge.evaluate(task.goal, contract, execution_evidence or {})
            if (
                judge_res.status is GoalVerificationStatus.BLOCKED
                and "budget_exhausted" in judge_res.summary
            ):
                # Goal verification is NEVER skipped merely because model-call budget is low!
                # Fall back to deterministic evaluation result.
                return det_result
            return judge_res

        return det_result


class CallableTaskGoalVerifier:
    """Test injector for deterministic or scripted verification outputs."""

    def __init__(self, fn: Callable[..., TaskGoalVerification]) -> None:
        self._fn = fn

    def verify(
        self,
        task: Task,
        contract: GoalContract,
        steps: list[TaskStep],
        *,
        execution_evidence: dict[str, Any] | None = None,
    ) -> TaskGoalVerification:
        return self._fn(task, contract, steps, execution_evidence or {})

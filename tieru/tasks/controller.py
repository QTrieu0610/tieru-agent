"""Runtime-owned step completion controller and evidence realization.

The model proposes actions; the runtime owns whether a step is complete.
A provider stop/finish reason or natural language prose alone cannot complete
a step requiring execution evidence.
"""

from __future__ import annotations

import json
from typing import Any

from tieru.memory.personal import redact_secrets
from tieru.tasks.models import (
    StepCompletionAssessment,
    StepContinuationDecision,
    StepEvidenceRequirement,
    StepExecution,
    StepExecutionKind,
    Task,
    TaskLimits,
    TaskStep,
)

READ_TOOL_NAMES = {
    "filesystem_read",
    "code_read",
    "filesystem_list",
    "filesystem_search",
    "code_search",
    "document_read",
    "git_status",
    "git_diff",
    "git_log",
}

WRITE_TOOL_NAMES = {
    "filesystem_write",
    "filesystem_edit",
    "code_patch",
    "filesystem_mkdir",
}

COMMAND_TOOL_NAMES = {
    "run_command",
    "shell_run",
}

EXTERNAL_ACTION_TOOL_NAMES = {
    "web_fetch",
    "web_search",
}


class StepCompletionController:
    """Deterministic, production-owned step completion controller.

    Evaluates observable execution evidence after each model turn to determine
    if the step has produced all required machine-verifiable artifacts.
    """

    def __init__(self, limits: TaskLimits | None = None) -> None:
        self.limits = limits or TaskLimits()

    def assess(
        self,
        task: Task,
        step: TaskStep,
        execution: StepExecution,
        *,
        visible_tools: set[str] | None = None,
    ) -> StepCompletionAssessment:
        """Assess whether a step's evidence requirements are satisfied.

        Uses only structured runtime state: step contract, execution kind,
        tools visible, tools executed, exit codes, and Action Ledger / Trust status.
        Never relies on model self-claims or prose.
        """
        # 1. Action Ledger Uncertainty: Always requires Human Recovery -> BLOCKED
        for tc in execution.tool_calls:
            err = str(tc.get("error_code") or "")
            out = str(tc.get("output") or "")
            if err == "tool_execution_uncertain" or "tool_execution_uncertain" in out:
                return StepCompletionAssessment(
                    decision=StepContinuationDecision.BLOCKED,
                    satisfied_requirements=(),
                    missing_requirements=("human_recovery_required",),
                    reason="Uncertain external execution requires human recovery.",
                )
        if "tool_execution_uncertain" in execution.result:
            return StepCompletionAssessment(
                decision=StepContinuationDecision.BLOCKED,
                satisfied_requirements=(),
                missing_requirements=("human_recovery_required",),
                reason="Uncertain external execution requires human recovery.",
            )

        # 2. Hard Trust Policy Denial: Cannot be bypassed -> BLOCKED
        for tc in execution.tool_calls:
            err = str(tc.get("error_code") or "")
            out = str(tc.get("output") or "")
            if err == "tool_permission_denied" or "tool_permission_denied" in out:
                return StepCompletionAssessment(
                    decision=StepContinuationDecision.BLOCKED,
                    satisfied_requirements=(),
                    missing_requirements=("trust_authorization",),
                    reason="Hard Trust policy denial cannot be bypassed.",
                )
        if "tool_permission_denied" in execution.result:
            return StepCompletionAssessment(
                decision=StepContinuationDecision.BLOCKED,
                satisfied_requirements=(),
                missing_requirements=("trust_authorization",),
                reason="Hard Trust policy denial cannot be bypassed.",
            )

        # 3. Budget Exhaustion: Task budget exceeded -> BUDGET_EXHAUSTED
        for tc in execution.tool_calls:
            err = str(tc.get("error_code") or "")
            if "budget_exhausted:" in err:
                resource = err.split("budget_exhausted:", 1)[1]
                return StepCompletionAssessment(
                    decision=StepContinuationDecision.BUDGET_EXHAUSTED,
                    satisfied_requirements=(),
                    missing_requirements=(f"budget_{resource}",),
                    reason=f"budget_exhausted:{resource}",
                )
        if "budget_exhausted:" in execution.result:
            for part in execution.result.split():
                if "budget_exhausted:" in part:
                    res = part.split("budget_exhausted:", 1)[1].rstrip(".,;:\"'")
                    return StepCompletionAssessment(
                        decision=StepContinuationDecision.BUDGET_EXHAUSTED,
                        satisfied_requirements=(),
                        missing_requirements=(f"budget_{res}",),
                        reason=f"budget_exhausted:{res}",
                    )

        # Determine execution kind
        kind = step.execution_kind or StepExecutionKind.REASONING

        # 4. Plan Capability Mismatch: Required tool is impossible under current visibility
        if visible_tools is not None and kind in {
            StepExecutionKind.READ,
            StepExecutionKind.WRITE,
            StepExecutionKind.COMMAND,
            StepExecutionKind.EXTERNAL_ACTION,
        }:
            capable_tools: set[str] = set()
            if kind is StepExecutionKind.READ:
                capable_tools = READ_TOOL_NAMES & visible_tools
            elif kind is StepExecutionKind.WRITE:
                capable_tools = WRITE_TOOL_NAMES & visible_tools
            elif kind is StepExecutionKind.COMMAND:
                capable_tools = COMMAND_TOOL_NAMES & visible_tools
            elif kind is StepExecutionKind.EXTERNAL_ACTION:
                capable_tools = (
                    EXTERNAL_ACTION_TOOL_NAMES
                    | {t for t in visible_tools if t.startswith(("browser_", "github_"))}
                ) & visible_tools

            if not capable_tools:
                return StepCompletionAssessment(
                    decision=StepContinuationDecision.REPLAN_REQUIRED,
                    satisfied_requirements=(),
                    missing_requirements=(f"capability:{kind.value}",),
                    reason=f"No visible tool available for required execution kind '{kind.value}'.",
                )

        # 5. Pure Cognitive REASONING Steps
        if kind is StepExecutionKind.REASONING:
            answer = execution.result.strip()
            if (answer and not answer.startswith("Error:")) or execution.tool_calls:
                return StepCompletionAssessment(
                    decision=StepContinuationDecision.READY_TO_VERIFY,
                    satisfied_requirements=("semantic_answer",),
                    missing_requirements=(),
                    reason="Substantive candidate output produced for reasoning step.",
                )
            return StepCompletionAssessment(
                decision=StepContinuationDecision.CONTINUE,
                satisfied_requirements=(),
                missing_requirements=("substantive_answer",),
                reason="Reasoning step requires a substantive answer candidate.",
            )

        # 6. Tool-Required Steps: Evaluate structured evidence requirements
        tools_executed: list[dict[str, Any]] = []
        for tc in execution.tool_calls:
            out_raw = str(tc.get("output") or "")
            success = True
            try:
                parsed = json.loads(out_raw) if out_raw.startswith("{") else None
                if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("ok") is False):
                    success = False
            except Exception:
                pass
            if "failed" in out_raw.lower() or out_raw.lower().startswith("error"):
                success = False
            tools_executed.append({
                "name": str(tc.get("tool") or tc.get("name") or ""),
                "success": success,
                "output": out_raw,
                "args": tc.get("args") or {},
            })

        satisfied: list[str] = []
        missing: list[str] = []

        # If explicit evidence requirements exist on the step:
        if step.evidence_requirements:
            for req in step.evidence_requirements:
                req_str = f"[{req.kind}] {req.description}"
                is_met = self._is_requirement_satisfied(req, tools_executed, execution)
                if is_met:
                    satisfied.append(req_str)
                elif req.required:
                    missing.append(req_str)
        else:
            # Inferred requirements from execution kind
            if kind is StepExecutionKind.COMMAND:
                cmd_ran = any(
                    t["name"] in COMMAND_TOOL_NAMES and t["success"] for t in tools_executed
                )
                if cmd_ran:
                    satisfied.append("[command_exit_zero] Command executed with exit code 0")
                else:
                    missing.append("[command_exit_zero] Command executed with exit code 0")

            elif kind is StepExecutionKind.WRITE:
                write_ran = any(
                    t["name"] in WRITE_TOOL_NAMES and t["success"] for t in tools_executed
                )
                if write_ran:
                    satisfied.append("[artifact_changed] Mutating filesystem tool executed")
                else:
                    missing.append("[artifact_changed] Mutating filesystem tool executed")

            elif kind is StepExecutionKind.READ:
                read_ran = any(
                    t["name"] in READ_TOOL_NAMES and t["success"] for t in tools_executed
                )
                if read_ran:
                    satisfied.append("[tool_success] Inspection tool executed successfully")
                else:
                    missing.append("[tool_success] Inspection tool executed successfully")

            elif kind is StepExecutionKind.EXTERNAL_ACTION:
                ext_ran = any(
                    (t["name"] in EXTERNAL_ACTION_TOOL_NAMES or t["name"].startswith(("browser_", "github_")))
                    and t["success"]
                    for t in tools_executed
                )
                if ext_ran:
                    satisfied.append("[tool_success] External action executed successfully")
                else:
                    missing.append("[tool_success] External action executed successfully")

            elif kind is StepExecutionKind.MIXED:
                any_ran = any(t["success"] for t in tools_executed)
                if any_ran:
                    satisfied.append("[tool_success] Required action executed successfully")
                else:
                    missing.append("[tool_success] Required action executed successfully")

        if not missing:
            return StepCompletionAssessment(
                decision=StepContinuationDecision.READY_TO_VERIFY,
                satisfied_requirements=tuple(satisfied),
                missing_requirements=(),
                reason="All required observable evidence requirements are satisfied.",
            )

        return StepCompletionAssessment(
            decision=StepContinuationDecision.CONTINUE,
            satisfied_requirements=tuple(satisfied),
            missing_requirements=tuple(missing),
            reason=f"Missing required observable evidence: {', '.join(missing)}.",
        )

    def _is_requirement_satisfied(
        self,
        req: StepEvidenceRequirement,
        tools_executed: list[dict[str, Any]],
        execution: StepExecution,
    ) -> bool:
        """Check if an individual evidence requirement is satisfied by observable state."""
        kind = req.kind.strip().lower()
        _desc = req.description.strip().lower()

        if kind == "command_exit_zero":
            for t in tools_executed:
                if t["name"] in COMMAND_TOOL_NAMES and t["success"]:
                    try:
                        parsed = json.loads(t["output"]) if t["output"].startswith("{") else None
                        if isinstance(parsed, dict) and parsed.get("exit_code") == 0:
                            return True
                    except Exception:
                        pass
                    return True
            return False

        if kind == "artifact_changed":
            # Check for mutating tools that succeeded
            for t in tools_executed:
                if t["name"] in WRITE_TOOL_NAMES and t["success"]:
                    return True
            return False

        if kind == "artifact_exists":
            for t in tools_executed:
                if t["name"] in (WRITE_TOOL_NAMES | READ_TOOL_NAMES) and t["success"]:
                    return True
            return False

        if kind == "tool_success":
            for t in tools_executed:
                if t["success"]:
                    return True
            return False

        if kind == "semantic_answer":
            return bool(execution.result and len(execution.result.strip()) >= 10)

        # Fallback heuristic
        return any(t["success"] for t in tools_executed)

    def format_continuation_prompt(
        self,
        step: TaskStep,
        assessment: StepCompletionAssessment,
    ) -> str:
        """Format a bounded, CONTROL/DATA-safe continuation directive for the model."""
        missing_lines = "\n".join(f"- {req}" for req in assessment.missing_requirements)
        satisfied_info = ""
        if assessment.satisfied_requirements:
            sat_list = ", ".join(assessment.satisfied_requirements)
            satisfied_info = f"\nAlready satisfied evidence (do not repeat unnecessarily): {sat_list}\n"

        prompt = (
            "CONTINUATION DIRECTIVE:\n"
            "The current step is NOT complete because required observable evidence has not yet been produced.\n"
            f"{satisfied_info}"
            f"Missing required evidence:\n{missing_lines}\n\n"
            f"Continue working on the SAME step:\n"
            f"STEP OBJECTIVE: {step.position}. {step.title} — {step.instruction}\n\n"
            "EXECUTION RULES:\n"
            "1. Stay on this current step. Do not begin subsequent steps.\n"
            "2. Do not merely claim or summarize that the step succeeded.\n"
            "3. You must invoke the relevant permitted tool now to produce the required evidence.\n"
            "All actions remain subject to runtime policy."
        )
        return redact_secrets(prompt)

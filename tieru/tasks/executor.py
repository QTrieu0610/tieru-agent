"""Execute exactly one claimed task step through Tieru's existing runtime."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from tieru.context import ContextBlock, ContextTrust
from tieru.memory.personal import redact_secrets
from tieru.tasks.controller import StepCompletionController
from tieru.tasks.failure_recovery import (
    StepFailureClassifier,
    compute_strategy_fingerprint,
)
from tieru.tasks.goal_verifier import DeterministicGoalVerifier, TaskGoalVerifier
from tieru.tasks.models import (
    BudgetResource,
    GoalVerificationStatus,
    PlanReviewDecision,
    PlanValidationError,
    ReplanLimitExceededError,
    StepClaimOutcome,
    StepContinuationDecision,
    StepExecution,
    StepExecutionKind,
    StepFailureDisposition,
    StepStatus,
    Task,
    TaskGoalVerification,
    TaskLimits,
    TaskRunResult,
    TaskStatus,
    TaskStep,
    TaskValidationError,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.protocol import (
    ExecutorErrorClass,
    ToolActivationMode,
    ToolChoicePolicy,
    compact_continuation_context,
    determine_tool_activation_mode,
    filter_compatible_tools,
    format_first_turn_scaffold,
    is_prose_promise,
)
from tieru.tasks.reviewer import TaskPlanReviewer
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import TaskVerifier, extract_step_evidence

Observer = Callable[[str, dict], None]


class TaskStepRunner(Protocol):
    def __call__(
        self, task: Task, step: TaskStep, context: str, observer: Observer | None
    ) -> StepExecution: ...


class TaskContextBuilder:
    def __init__(self, limits: TaskLimits) -> None:
        self.limits = limits

    def build(self, task: Task, steps: list[TaskStep], current: TaskStep) -> str:
        lines = [
            "You are executing a durable multi-step task inside Tieru.",
            f"Task Goal: {task.goal}",
        ]
        completed = [
            step
            for step in steps
            if step.position < current.position
            and step.status in {StepStatus.SUCCEEDED, StepStatus.SKIPPED}
        ]
        if completed:
            lines.extend(["", "Completed checkpoints:"])
            for step in completed:
                summary = step.verification_summary or step.result or step.status.value
                lines.append(f"{step.position}. {step.status.value.upper()} — {summary}")
        exec_kind_str = current.execution_kind.value if current.execution_kind else "reasoning"
        lines.extend(
            [
                "",
                "CURRENT STEP CONTRACT:",
                f"STEP OBJECTIVE: {current.position}. {current.title} — {current.instruction}",
                f"EXPECTED EXECUTION KIND: {exec_kind_str.upper()}",
            ]
        )
        if current.evidence_requirements:
            lines.append("REQUIRED EVIDENCE:")
            for req in current.evidence_requirements:
                lines.append(f"- [{req.kind}] {req.description}")
        elif current.verification_instruction:
            lines.extend(["REQUIRED EVIDENCE:", current.verification_instruction])
        lines.extend(
            [
                "",
                "EXECUTION DIRECTIVE: Do not merely state or summarize that the step succeeded. Produce the required observable evidence through permitted tools when tools are required. If expected execution kind is READ, WRITE, or COMMAND, you must invoke the appropriate tool.",
            ]
        )
        safe = redact_secrets("\n".join(lines))
        encoded = safe.encode("utf-8")
        if len(encoded) <= self.limits.max_context_bytes:
            return safe
        marker = "\n[CONTEXT TRUNCATED]"
        budget = self.limits.max_context_bytes - len(marker.encode("utf-8"))
        return encoded[:budget].decode("utf-8", errors="ignore") + marker


class TieruStepRunner:
    """Adapter around the normal Tieru turn; it never calls a tool directly."""

    def __init__(self, tieru) -> None:
        self.tieru = tieru

    def __call__(
        self, task: Task, step: TaskStep, context: str, observer: Observer | None,
        tool_choice_policy: Any = None,
    ) -> StepExecution:
        self.tieru.session.start_new(f"task:{task.task_id}:{step.step_id}")
        source = "schedule" if task.source == "scheduled" else "task"
        block = ContextBlock(
            source=source,
            trust=ContextTrust.DATA,
            content=context,
            metadata={"task_id": task.task_id, "step_id": step.step_id},
        )
        routing_query = f"{step.instruction} (Goal: {task.goal[:200]})"
        result = self.tieru.respond(
            task.goal, observer=observer, source="task", context_blocks=(block,),
            routing_query=routing_query, task_id=task.task_id, role="executor",
            tool_choice_policy=tool_choice_policy,
        )
        return StepExecution(
            result=redact_secrets(result.reply),
            run_id=result.run_id or "",
            tool_calls=tuple(result.tool_calls),
        )


class TaskExecutor:
    def __init__(
        self,
        store: TaskStore,
        runner: TaskStepRunner,
        verifier: TaskVerifier,
        *,
        reviewer: TaskPlanReviewer | None = None,
        goal_verifier: TaskGoalVerifier | None = None,
        controller: StepCompletionController | None = None,
        failure_classifier: StepFailureClassifier | None = None,
        replay=None,
        limits: TaskLimits | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.verifier = verifier
        self.reviewer = reviewer
        self.goal_verifier = goal_verifier or DeterministicGoalVerifier()
        self.replay = replay
        self.limits = limits or store.limits
        self.clock = clock or time.monotonic
        self.context_builder = TaskContextBuilder(self.limits)
        self.controller = controller or StepCompletionController(self.limits)
        self.failure_classifier = failure_classifier or StepFailureClassifier()

    def _get_failed_fingerprints(self, task_id: str) -> set[str]:
        steps = self.store.list_steps(task_id)
        return {
            compute_strategy_fingerprint(s)
            for s in steps
            if s.status is StepStatus.FAILED
        }

    def _resolve_visible_tools(self, task: Task, step: TaskStep) -> set[str] | None:
        if isinstance(self.runner, TieruStepRunner):
            tieru = getattr(self.runner, "tieru", None)
            if tieru is not None:
                tools = getattr(tieru, "tools", None)
                router = getattr(tieru, "capability_router", None)
                if router is not None and tools is not None:
                    routing_query = f"{step.instruction} (Goal: {task.goal[:200]})"
                    routing = router.route(routing_query, tools)
                    return set(routing.selected_tools)
                if isinstance(tools, dict):
                    return set(tools.keys())
        vt = getattr(self.runner, "visible_tools", None)
        if isinstance(vt, (set, list, tuple)):
            return set(vt)
        return None

    @staticmethod
    def _notify(observer: Observer | None, kind: str, task: Task, step: TaskStep | None, **extra) -> None:
        if observer is None:
            return
        payload = {"task_id": task.task_id, "status": task.status.value, **extra}
        if step is not None:
            payload.update(
                {"step_id": step.step_id, "position": step.position, "step_status": step.status.value}
            )
        observer(kind, payload)

    def _record(self, run_id: str, kind: str, task: Task, step: TaskStep, **extra) -> None:
        if not run_id or self.replay is None:
            return
        payload = {
            "task_id": task.task_id,
            "step_id": step.step_id,
            "position": step.position,
            "status": task.status.value,
            "step_status": step.status.value,
            **extra,
        }
        try:
            self.replay.record_event(run_id, kind, payload)
        except Exception:
            return

    def run_next(
        self,
        task_id: str,
        *,
        observer: Observer | None = None,
        recover_orphaned: bool = False,
    ) -> TaskRunResult:
        claim = self.store.claim_next_step(task_id)
        if (
            claim.outcome is StepClaimOutcome.IN_PROGRESS
            and recover_orphaned
            and claim.step is not None
            and self.store.is_step_stale(claim.step)
        ):
            task, step = self.store.block_running_step(
                task_id,
                "A persisted running step has no safe completion checkpoint; manual recovery is required.",
            )
            self._notify(observer, "task_step_blocked", task, step, code="orphaned_running_step")
            return TaskRunResult(task, step, "orphaned_running_step", False)
        if claim.outcome is not StepClaimOutcome.CLAIMED:
            if claim.code.startswith("budget_exhausted:"):
                resource_name = claim.code.split(":", 1)[1]
                budget = self.store.get_task_budget(task_id)
                usage = self.store.get_task_budget_usage(task_id)
                used_val = getattr(usage, f"{resource_name}_seconds", getattr(usage, resource_name, 0))
                limit_val = getattr(budget, f"max_{resource_name}_seconds", getattr(budget, f"max_{resource_name}", 0))
                self._notify(
                    observer,
                    "task_budget_exhausted",
                    claim.task,
                    claim.step,
                    resource=resource_name,
                    used=used_val,
                    limit=limit_val,
                )
            self._notify(observer, "task_blocked", claim.task, claim.step, code=claim.code)
            return TaskRunResult(claim.task, claim.step, claim.code, False)

        task = claim.task
        step = claim.step
        assert step is not None

        budget = self.store.get_task_budget(task_id)
        usage = self.store.get_task_budget_usage(task_id)
        if usage.active_runtime_seconds >= budget.max_active_runtime_seconds:
            task = self.store.block_task_budget_exhausted(
                task_id,
                BudgetResource.ACTIVE_RUNTIME,
                usage.active_runtime_seconds,
                budget.max_active_runtime_seconds,
            )
            self._notify(
                observer,
                "task_budget_exhausted",
                task,
                step,
                resource=BudgetResource.ACTIVE_RUNTIME.value,
                used=usage.active_runtime_seconds,
                limit=budget.max_active_runtime_seconds,
            )
            self._notify(observer, "task_blocked", task, step, code="budget_exhausted:active_runtime")
            return TaskRunResult(task, step, "budget_exhausted:active_runtime", False)

        self._notify(observer, "task_step_claimed", task, step)
        self._notify(observer, "task_step_started", task, step)
        context = self.context_builder.build(task, self.store.list_steps(task_id), step)
        max_turns = min(getattr(self.limits, "max_execution_turns_per_step", 3), 3)
        accumulated_tool_calls: list[dict] = []
        latest_result = ""
        last_run_id = ""
        current_context = context
        turn_number = 1
        visible_tools: set[str] | None = self._resolve_visible_tools(task, step)
        execution: StepExecution | None = None
        consecutive_no_progress = 0
        prev_satisfied_count = 0

        tieru = getattr(self.runner, "tieru", None) if isinstance(self.runner, TieruStepRunner) else None
        tool_registry = getattr(tieru, "tools", None) if tieru else None

        task_budget = self.store.get_task_budget(task_id)
        task_usage = self.store.get_task_budget_usage(task_id)
        rem_m = (task_budget.max_model_calls - task_usage.model_calls) if task_budget.max_model_calls is not None else None

        # Derive compatible tools (invariant: compatible_tools <= visible_tools)
        compatible_tools: tuple[str, ...] = filter_compatible_tools(
            step,
            visible_tools or (),
            missing_requirements=step.evidence_requirements,
            tool_registry=tool_registry,
        )

        activation_mode = determine_tool_activation_mode(
            step,
            missing_requirements=step.evidence_requirements,
            compatible_tools=compatible_tools,
            has_trust_block=False,
            has_uncertain_action=False,
            remaining_budget=rem_m,
        )

        tool_choice_policy = ToolChoicePolicy(
            mode=activation_mode,
            allowed_tools=compatible_tools,
        )

        if activation_mode is ToolActivationMode.REQUIRED:
            missing_descs = [r.description for r in step.evidence_requirements] if step.evidence_requirements else ()
            first_turn_scaffold = format_first_turn_scaffold(
                step,
                compatible_tools,
                missing_requirements=missing_descs,
            )
            current_context = context + "\n\n" + first_turn_scaffold
            self._notify(
                observer,
                "tool_activation_signaled",
                task,
                step,
                mode=activation_mode.value,
                compatible_tools=list(compatible_tools),
                compatible_count=len(compatible_tools),
            )

        while turn_number <= max_turns:
            t_active_start = self.clock()
            try:
                try:
                    turn_execution = self.runner(
                        task, step, current_context, observer, tool_choice_policy=tool_choice_policy
                    )
                except TypeError:
                    turn_execution = self.runner(task, step, current_context, observer)
            except Exception as exc:
                dur = max(0.0, self.clock() - t_active_start)
                self.store.record_budget_consumption(task_id, BudgetResource.ACTIVE_RUNTIME, dur)
                verification = VerificationResult(
                    VerificationStatus.FAIL,
                    f"Step runtime failed before a verifiable checkpoint: {type(exc).__name__}: {exc}.",
                )
                task, step = self.store.finish_step(
                    step.step_id, result="", verification=verification
                )
                self._notify(observer, "task_step_failed", task, step, code=type(exc).__name__)
                return TaskRunResult(task, step, "task_step_failed", True)

            dur = max(0.0, self.clock() - t_active_start)
            self.store.record_budget_consumption(task_id, BudgetResource.ACTIVE_RUNTIME, dur)

            accumulated_tool_calls.extend(turn_execution.tool_calls)
            latest_result = turn_execution.result or latest_result
            last_run_id = turn_execution.run_id or last_run_id

            execution = StepExecution(
                result=latest_result,
                run_id=last_run_id,
                tool_calls=tuple(accumulated_tool_calls),
            )

            # Record activation telemetry on turn 1
            if turn_number == 1 and activation_mode is ToolActivationMode.REQUIRED:
                self._record(
                    turn_execution.run_id,
                    "tool_activation_signaled",
                    task,
                    step,
                    mode=activation_mode.value,
                    compatible_tools=list(compatible_tools),
                    compatible_count=len(compatible_tools),
                )
                if turn_execution.tool_calls:
                    called_tool = str(turn_execution.tool_calls[0].get("tool") or "")
                    if called_tool in compatible_tools:
                        self._record(turn_execution.run_id, "first_turn_tool_activation_succeeded", task, step, tool=called_tool)
                        self._notify(observer, "first_turn_tool_activation_succeeded", task, step, tool=called_tool)
                    else:
                        self._record(turn_execution.run_id, "executor_wrong_tool", task, step, tool=called_tool)
                        self._notify(observer, "executor_wrong_tool", task, step, tool=called_tool)
                else:
                    self._record(turn_execution.run_id, "tool_activation_signal_ignored", task, step, turn_number=1)
                    self._notify(observer, "tool_activation_signal_ignored", task, step, turn_number=1)
                    self._record(turn_execution.run_id, "executor_protocol_error", task, step, error_class=ExecutorErrorClass.PREMATURE_FINAL.value)
                    self._notify(observer, "executor_protocol_error", task, step, error_class=ExecutorErrorClass.PREMATURE_FINAL.value)

            # Check for budget exhausted in execution first
            exhausted_resource = None
            if "budget_exhausted:" in execution.result:
                for part in execution.result.split():
                    if "budget_exhausted:" in part:
                        exhausted_resource = part.split("budget_exhausted:", 1)[1].rstrip(".,;:\"'")
                        break
            for tc in execution.tool_calls:
                err = str(tc.get("error_code") or "")
                if "budget_exhausted:" in err:
                    exhausted_resource = err.split("budget_exhausted:", 1)[1]
                    break

            if exhausted_resource:
                budget = self.store.get_task_budget(task_id)
                usage = self.store.get_task_budget_usage(task_id)
                try:
                    res_enum = BudgetResource(exhausted_resource)
                except ValueError:
                    res_enum = BudgetResource.MODEL_CALLS
                used_val = getattr(usage, f"{res_enum.value}_seconds", getattr(usage, res_enum.value, 0))
                limit_val = getattr(budget, f"max_{res_enum.value}_seconds", getattr(budget, f"max_{res_enum.value}", 0))
                task = self.store.block_task_budget_exhausted(
                    task_id,
                    res_enum,
                    used_val,
                    limit_val,
                )
                verification = VerificationResult(
                    VerificationStatus.BLOCKED,
                    f"budget_exhausted:{exhausted_resource}",
                )
                task, step = self.store.finish_step(
                    step.step_id,
                    result=execution.result,
                    verification=verification,
                    auto_complete=False,
                )
                self._record(
                    execution.run_id,
                    "task_budget_exhausted",
                    task,
                    step,
                    resource=exhausted_resource,
                    used=used_val,
                    limit=limit_val,
                )
                self._notify(
                    observer,
                    "task_budget_exhausted",
                    task,
                    step,
                    resource=exhausted_resource,
                    used=used_val,
                    limit=limit_val,
                )
                self._record(execution.run_id, "task_step_blocked", task, step)
                self._notify(observer, "task_step_blocked", task, step, verification_status=VerificationStatus.BLOCKED.value)
                self._record(execution.run_id, "task_blocked", task, step)
                self._notify(observer, "task_blocked", task, step, code=f"budget_exhausted:{exhausted_resource}")
                return TaskRunResult(task, step, f"budget_exhausted:{exhausted_resource}", True)

            # Controller Assessment
            assessment = self.controller.assess(
                task=task,
                step=step,
                execution=execution,
                visible_tools=visible_tools,
            )

            self._record(
                execution.run_id,
                "step_completion_assessed",
                task,
                step,
                turn_number=turn_number,
                decision=assessment.decision.value,
                satisfied_count=len(assessment.satisfied_requirements),
                missing_count=len(assessment.missing_requirements),
                reason=assessment.reason,
            )
            self._notify(
                observer,
                "step_completion_assessed",
                task,
                step,
                turn_number=turn_number,
                decision=assessment.decision.value,
                satisfied_count=len(assessment.satisfied_requirements),
                missing_count=len(assessment.missing_requirements),
                reason=assessment.reason,
            )

            if assessment.satisfied_requirements and assessment.missing_requirements:
                self._record(
                    execution.run_id,
                    "step_evidence_partial",
                    task,
                    step,
                    turn_number=turn_number,
                    satisfied=list(assessment.satisfied_requirements),
                    missing=list(assessment.missing_requirements),
                )
                self._notify(
                    observer,
                    "step_evidence_partial",
                    task,
                    step,
                    turn_number=turn_number,
                    satisfied=list(assessment.satisfied_requirements),
                    missing=list(assessment.missing_requirements),
                )

            if assessment.decision is StepContinuationDecision.READY_TO_VERIFY:
                self._record(
                    execution.run_id,
                    "step_evidence_complete",
                    task,
                    step,
                    turn_number=turn_number,
                    satisfied_count=len(assessment.satisfied_requirements),
                )
                self._notify(
                    observer,
                    "step_evidence_complete",
                    task,
                    step,
                    turn_number=turn_number,
                    satisfied_count=len(assessment.satisfied_requirements),
                )
                if turn_number > 1:
                    self._record(
                        execution.run_id,
                        "evidence_correction_completed",
                        task,
                        step,
                        tool_count=len(turn_execution.tool_calls),
                    )
                    self._notify(
                        observer,
                        "evidence_correction_completed",
                        task,
                        step,
                        tool_count=len(turn_execution.tool_calls),
                    )
                break

            if assessment.decision is StepContinuationDecision.BLOCKED:
                verification = VerificationResult(
                    VerificationStatus.BLOCKED,
                    assessment.reason,
                )
                task, step = self.store.finish_step(
                    step.step_id, result=execution.result, verification=verification, auto_complete=False
                )
                self._record(execution.run_id, "task_step_blocked", task, step)
                self._notify(observer, "task_step_blocked", task, step, verification_status=VerificationStatus.BLOCKED.value)
                self._record(execution.run_id, "task_blocked", task, step)
                self._notify(observer, "task_blocked", task, step, code=assessment.reason)
                return TaskRunResult(task, step, "task_step_blocked", True)

            if assessment.decision is StepContinuationDecision.BUDGET_EXHAUSTED:
                verification = VerificationResult(
                    VerificationStatus.BLOCKED,
                    assessment.reason,
                )
                task, step = self.store.finish_step(
                    step.step_id, result=execution.result, verification=verification, auto_complete=False
                )
                self._record(execution.run_id, "task_step_blocked", task, step)
                self._notify(observer, "task_step_blocked", task, step, verification_status=VerificationStatus.BLOCKED.value)
                self._record(execution.run_id, "task_blocked", task, step)
                self._notify(observer, "task_blocked", task, step, code=assessment.reason)
                return TaskRunResult(task, step, "task_step_blocked", True)

            if assessment.decision is StepContinuationDecision.REPLAN_REQUIRED:
                self._record(execution.run_id, "step_replan_required", task, step, reason=assessment.reason)
                self._notify(observer, "step_replan_required", task, step, reason=assessment.reason)
                break

            # Evaluate progress on this turn
            new_satisfied = len(assessment.satisfied_requirements) > prev_satisfied_count
            prev_satisfied_count = len(assessment.satisfied_requirements)
            turn_tools_succeeded = any(
                str(tc.get("output", "")).lower().find("error") == -1
                and str(tc.get("error_code") or "") == ""
                for tc in turn_execution.tool_calls
            )
            step_kind = step.execution_kind or StepExecutionKind.REASONING
            reasoning_progress = (
                step_kind is StepExecutionKind.REASONING
                and bool(turn_execution.result and len(turn_execution.result.strip()) >= 10)
            )

            is_promise = is_prose_promise(turn_execution.result)
            if new_satisfied or turn_tools_succeeded or reasoning_progress:
                consecutive_no_progress = 0
            elif is_promise:
                consecutive_no_progress += 1

            if consecutive_no_progress >= 2:
                self._record(
                    execution.run_id,
                    "step_no_progress_exhausted",
                    task,
                    step,
                    turn_number=turn_number,
                    consecutive_no_progress=consecutive_no_progress,
                    reason="consecutive_no_progress_limit_reached",
                )
                self._notify(
                    observer,
                    "step_no_progress_exhausted",
                    task,
                    step,
                    turn_number=turn_number,
                    consecutive_no_progress=consecutive_no_progress,
                    reason="consecutive_no_progress_limit_reached",
                )
                self._record(
                    execution.run_id,
                    "step_continuation_exhausted",
                    task,
                    step,
                    turn_number=turn_number,
                    missing_count=len(assessment.missing_requirements),
                    reason="consecutive_no_progress_limit_reached",
                )
                self._notify(
                    observer,
                    "step_continuation_exhausted",
                    task,
                    step,
                    turn_number=turn_number,
                    missing_count=len(assessment.missing_requirements),
                    reason="consecutive_no_progress_limit_reached",
                )
                break

            # Handle CONTINUE
            if turn_number >= max_turns:
                self._record(
                    execution.run_id,
                    "step_continuation_exhausted",
                    task,
                    step,
                    turn_number=turn_number,
                    missing_count=len(assessment.missing_requirements),
                    reason="max_execution_turns_per_step_reached",
                )
                self._notify(
                    observer,
                    "step_continuation_exhausted",
                    task,
                    step,
                    turn_number=turn_number,
                    missing_count=len(assessment.missing_requirements),
                    reason="max_execution_turns_per_step_reached",
                )
                break

            # Check budget availability for continuation
            task_b = self.store.get_task_budget(task.task_id)
            task_u = self.store.get_task_budget_usage(task.task_id)
            rem_m = (task_b.max_model_calls - task_u.model_calls) if task_b.max_model_calls is not None else None
            if rem_m is not None and rem_m < 1.0:
                self._record(execution.run_id, "step_continuation_exhausted", task, step, reason="budget_exhausted")
                self._notify(observer, "step_continuation_exhausted", task, step, reason="budget_exhausted")
                self._record(execution.run_id, "evidence_correction_blocked", task, step, reason="budget_exhausted")
                self._notify(observer, "evidence_correction_blocked", task, step, reason="budget_exhausted")
                break

            res_r = self.store.reserve_budget(task.task_id, BudgetResource.RETRIES, 1.0)
            if not res_r.allowed:
                self._record(execution.run_id, "step_continuation_exhausted", task, step, reason="budget_exhausted")
                self._notify(observer, "step_continuation_exhausted", task, step, reason="budget_exhausted")
                self._record(execution.run_id, "evidence_correction_blocked", task, step, reason="budget_exhausted")
                self._notify(observer, "evidence_correction_blocked", task, step, reason="budget_exhausted")
                break

            self._record(
                execution.run_id,
                "step_continuation_requested",
                task,
                step,
                turn_number=turn_number + 1,
                missing_requirements=list(assessment.missing_requirements),
            )
            self._notify(
                observer,
                "step_continuation_requested",
                task,
                step,
                turn_number=turn_number + 1,
                missing_requirements=list(assessment.missing_requirements),
            )
            self._record(execution.run_id, "evidence_correction_started", task, step, reason=assessment.reason)
            self._notify(observer, "evidence_correction_started", task, step, reason=assessment.reason)

            recent_summary = ""
            if turn_execution.tool_calls:
                last_tc = turn_execution.tool_calls[-1]
                recent_summary = f"Tool '{last_tc.get('tool')}' output: {str(last_tc.get('output'))[:400]}"
            elif turn_execution.result:
                recent_summary = f"Model response: {turn_execution.result[:300]}"

            continuation_prompt = compact_continuation_context(
                step=step,
                missing_requirements=list(assessment.missing_requirements),
                satisfied_requirements=list(assessment.satisfied_requirements),
                recent_tool_summary=recent_summary,
            )
            current_context = context + "\n\n" + continuation_prompt
            turn_number += 1

        if execution is None:
            execution = StepExecution(result="", run_id="", tool_calls=())

        step = self.store.attach_run_id(step.step_id, execution.run_id)
        self._record(execution.run_id, "task_step_claimed", task, step)
        self._record(execution.run_id, "task_step_started", task, step)

        # M35 & M37: Extract and persist execution checkpoints
        step_evidence = extract_step_evidence(step, execution)
        for cp in step_evidence.checkpoints:
            try:
                self.store.persist_checkpoint(cp)
            except Exception:
                pass

        try:
            verification = self.verifier.verify(task, step, execution)
        except Exception as exc:
            verification = VerificationResult(
                VerificationStatus.UNKNOWN,
                f"Verifier failed conservatively with {type(exc).__name__}.",
            )
        if verification.status is VerificationStatus.FAIL:
            replan_count = self.store.count_revisions(task.task_id, exclude_initial=True)
            task_budget = self.store.get_task_budget(task.task_id)
            effective_max_replans = min(self.limits.max_replans_per_task, task_budget.max_replans)
            task_usage = self.store.get_task_budget_usage(task.task_id)
            rem_model_calls = (
                max(0.0, task_budget.max_model_calls - task_usage.model_calls)
                if task_budget.max_model_calls is not None
                else None
            )
            prev_failed_prints = self._get_failed_fingerprints(task.task_id)

            assessment = self.failure_classifier.classify(
                task=task,
                step=step,
                execution=execution,
                verification=verification,
                replan_count=replan_count,
                effective_max_replans=effective_max_replans,
                remaining_model_calls=rem_model_calls,
                previous_failed_fingerprints=prev_failed_prints,
            )
            self._record(
                execution.run_id,
                "step_failure_classified",
                task,
                step,
                disposition=assessment.disposition.value,
                reason_code=assessment.reason_code,
                is_recoverable=assessment.is_recoverable,
                evidence_summary=assessment.evidence_summary,
                strategy_fingerprint=assessment.strategy_fingerprint,
            )
            self._notify(
                observer,
                "step_failure_classified",
                task,
                step,
                disposition=assessment.disposition.value,
                reason_code=assessment.reason_code,
                is_recoverable=assessment.is_recoverable,
                evidence_summary=assessment.evidence_summary,
                strategy_fingerprint=assessment.strategy_fingerprint,
            )
            self._record(
                execution.run_id,
                "task_verification",
                task,
                step,
                verification_status=verification.status.value,
            )

            if assessment.disposition is StepFailureDisposition.REPLAN and self.reviewer is not None:
                task, step = self.store.finish_step(
                    step.step_id,
                    result=execution.result,
                    verification=verification,
                    auto_complete=False,
                    task_status=TaskStatus.RUNNING,
                )
                self._record(execution.run_id, "task_step_failed", task, step, verification_status=verification.status.value)
                self._notify(observer, "task_step_failed", task, step, verification_status=verification.status.value)

                existing_rev = self.store.get_revision_by_trigger(task.task_id, step.step_id)
                if existing_rev is not None:
                    return TaskRunResult(task, step, "task_recovery_replan_applied", True)

                if rem_model_calls is not None and rem_model_calls < 1.0:
                    task = self.store.block_task(task.task_id, summary="budget_exhausted:model_calls")
                    self._record(execution.run_id, "task_recovery_exhausted", task, step, reason="budget_exhausted:model_calls")
                    self._notify(observer, "task_recovery_exhausted", task, step, reason="budget_exhausted:model_calls")
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step, code="budget_exhausted:model_calls")
                    return TaskRunResult(task, step, "budget_exhausted:model_calls", True)

                self._record(execution.run_id, "task_recovery_replan_requested", task, step, reason_code=assessment.reason_code)
                self._notify(observer, "task_recovery_replan_requested", task, step, reason_code=assessment.reason_code)
                self._record(execution.run_id, "task_replan_started", task, step)
                self._notify(observer, "task_replan_started", task, step)
                all_steps = self.store.list_steps(task.task_id)
                try:
                    review = self.reviewer.review(task, step, execution, verification, all_steps)
                except Exception as exc:
                    task = self.store.transition_task(task.task_id, TaskStatus.FAILED)
                    self._record(execution.run_id, "task_recovery_exhausted", task, step, reason=f"replan_error: {exc}")
                    self._notify(observer, "task_recovery_exhausted", task, step, reason=f"replan_error: {exc}")
                    self._record(execution.run_id, "task_failed", task, step)
                    self._notify(observer, "task_failed", task, step)
                    return TaskRunResult(task, step, "task_failed", True)

                if review.decision is PlanReviewDecision.REVISE_REMAINING and review.remaining_steps:
                    is_repeated = bool(
                        review.remaining_steps
                        and compute_strategy_fingerprint(review.remaining_steps[0]) == assessment.strategy_fingerprint
                    )
                    if is_repeated:
                        self._record(execution.run_id, "task_recovery_strategy_rejected", task, step, reason="repeated_failed_strategy")
                        self._notify(observer, "task_recovery_strategy_rejected", task, step, reason="repeated_failed_strategy")
                        task = self.store.transition_task(task.task_id, TaskStatus.FAILED)
                        self._record(execution.run_id, "task_failed", task, step)
                        self._notify(observer, "task_failed", task, step)
                        return TaskRunResult(task, step, "task_recovery_strategy_rejected", True)

                    task, revision, new_steps, superseded_steps = self.store.apply_plan_revision(
                        task.task_id,
                        trigger_step_id=step.step_id,
                        reason=f"Recovery from step failure: {review.reason or assessment.reason_code}",
                        remaining_steps=list(review.remaining_steps),
                    )
                    for s_step in superseded_steps:
                        self._record(execution.run_id, "task_step_superseded", task, s_step, superseded_by_revision=revision.revision_id)
                        self._notify(observer, "task_step_superseded", task, s_step, superseded_by_revision=revision.revision_id)
                    self._record(
                        execution.run_id,
                        "task_recovery_replan_applied",
                        task,
                        step,
                        revision_id=revision.revision_id,
                        revision_number=revision.revision_number,
                        old_remaining_count=len(superseded_steps),
                        new_remaining_count=len(new_steps),
                        reason=revision.reason,
                    )
                    self._notify(
                        observer,
                        "task_recovery_replan_applied",
                        task,
                        step,
                        revision_id=revision.revision_id,
                        revision_number=revision.revision_number,
                        old_remaining_count=len(superseded_steps),
                        new_remaining_count=len(new_steps),
                        reason=revision.reason,
                    )
                    self._record(
                        execution.run_id,
                        "task_plan_revised",
                        task,
                        step,
                        revision_id=revision.revision_id,
                        revision_number=revision.revision_number,
                        old_remaining_count=len(superseded_steps),
                        new_remaining_count=len(new_steps),
                        reason=revision.reason,
                    )
                    self._notify(
                        observer,
                        "task_plan_revised",
                        task,
                        step,
                        revision_id=revision.revision_id,
                        revision_number=revision.revision_number,
                        old_remaining_count=len(superseded_steps),
                        new_remaining_count=len(new_steps),
                        reason=revision.reason,
                    )
                    return TaskRunResult(task, step, "task_recovery_replan_applied", True)
                elif review.decision is PlanReviewDecision.BLOCK or review.reason == "budget_exhausted:model_calls":
                    task = self.store.block_task(task.task_id, summary=review.reason)
                    self._record(execution.run_id, "task_recovery_exhausted", task, step, reason=review.reason)
                    self._notify(observer, "task_recovery_exhausted", task, step, reason=review.reason)
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step, code=review.reason)
                    return TaskRunResult(task, step, "task_blocked", True)
                else:
                    task = self.store.transition_task(task.task_id, TaskStatus.FAILED)
                    self._record(execution.run_id, "task_recovery_exhausted", task, step, reason=review.reason or "recovery_exhausted")
                    self._notify(observer, "task_recovery_exhausted", task, step, reason=review.reason or "recovery_exhausted")
                    self._record(execution.run_id, "task_failed", task, step)
                    self._notify(observer, "task_failed", task, step)
                    return TaskRunResult(task, step, "task_failed", True)
            elif assessment.disposition is StepFailureDisposition.BLOCK:
                task, step = self.store.finish_step(
                    step.step_id,
                    result=execution.result,
                    verification=verification,
                    auto_complete=False,
                    task_status=TaskStatus.BLOCKED,
                )
                self._record(execution.run_id, "task_step_blocked", task, step)
                self._notify(observer, "task_step_blocked", task, step, verification_status=verification.status.value)
                self._record(execution.run_id, "task_blocked", task, step)
                self._notify(observer, "task_blocked", task, step, code=assessment.reason_code)
                return TaskRunResult(task, step, assessment.reason_code, True)
            else:
                task, step = self.store.finish_step(
                    step.step_id,
                    result=execution.result,
                    verification=verification,
                    auto_complete=False,
                    task_status=TaskStatus.FAILED,
                )
                self._record(execution.run_id, "task_step_failed", task, step)
                self._notify(observer, "task_step_failed", task, step, verification_status=verification.status.value)
                self._record(execution.run_id, "task_failed", task, step)
                self._notify(observer, "task_failed", task, step)
                return TaskRunResult(task, step, "task_failed", True)

        task, step = self.store.finish_step(
            step.step_id,
            result=execution.result,
            verification=verification,
            auto_complete=False,
        )
        if verification.status in {VerificationStatus.PASS, VerificationStatus.SKIPPED}:
            for cp in step_evidence.checkpoints:
                try:
                    self.store.mark_checkpoint_consumed(cp.checkpoint_id)
                except Exception:
                    pass
        self._record(
            execution.run_id,
            "task_verification",
            task,
            step,
            verification_status=verification.status.value,
        )
        terminal_event = {
            VerificationStatus.PASS: "task_step_succeeded",
            VerificationStatus.FAIL: "task_step_failed",
            VerificationStatus.BLOCKED: "task_step_blocked",
            VerificationStatus.UNKNOWN: "task_step_blocked",
            VerificationStatus.SKIPPED: "task_step_succeeded",
        }[verification.status]
        self._record(execution.run_id, terminal_event, task, step)
        self._notify(
            observer,
            terminal_event,
            task,
            step,
            verification_status=verification.status.value,
        )
        remaining_count = self.store.count_remaining_steps(task.task_id)
        if (
            verification.status is VerificationStatus.PASS
            and task.status is TaskStatus.RUNNING
            and remaining_count > 0
            and self.reviewer is not None
            and self.store.get_revision_by_trigger(task.task_id, step.step_id) is None
        ):
            usage = self.store.get_task_budget_usage(task.task_id)
            budget = self.store.get_task_budget(task.task_id)
            rem_model_calls = max(0.0, budget.max_model_calls - usage.model_calls)
            contract = self.store.get_goal_contract(task.task_id)
            required_headroom = float(remaining_count + (1 if contract is not None else 0))
            if rem_model_calls < (1.0 + required_headroom):
                self._record(
                    execution.run_id,
                    "task_plan_reviewed",
                    task,
                    step,
                    decision="keep",
                    reason="preserved_existing_plan_low_review_budget",
                )
                self._notify(
                    observer,
                    "task_plan_reviewed",
                    task,
                    step,
                    decision="keep",
                    reason="preserved_existing_plan_low_review_budget",
                )
                review = None
            else:
                self._record(execution.run_id, "task_replan_started", task, step)
                self._notify(observer, "task_replan_started", task, step)
                try:
                    all_steps = self.store.list_steps(task.task_id)
                    review = self.reviewer.review(
                        task, step, execution, verification, all_steps
                    )
                except Exception as exc:
                    task = self.store.block_task(
                        task.task_id, summary=f"plan_review_failed: {type(exc).__name__}"
                    )
                    self._record(
                        execution.run_id,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="plan_review_failed",
                    )
                    self._notify(
                        observer,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="plan_review_failed",
                    )
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step)
                    return TaskRunResult(task, step, "task_replan_blocked", True)

            if review is None:
                pass
            elif review.decision is PlanReviewDecision.KEEP:
                self._record(
                    execution.run_id,
                    "task_plan_reviewed",
                    task,
                    step,
                    decision="keep",
                    reason=review.reason,
                )
                self._notify(
                    observer,
                    "task_plan_reviewed",
                    task,
                    step,
                    decision="keep",
                    reason=review.reason,
                )
            elif review.decision is PlanReviewDecision.BLOCK:
                if review.reason == "budget_exhausted:model_calls":
                    self._record(
                        execution.run_id,
                        "task_plan_reviewed",
                        task,
                        step,
                        decision="keep",
                        reason="preserved_existing_plan_low_review_budget",
                    )
                    self._notify(
                        observer,
                        "task_plan_reviewed",
                        task,
                        step,
                        decision="keep",
                        reason="preserved_existing_plan_low_review_budget",
                    )
                else:
                    task = self.store.block_task(task.task_id, summary=review.reason)
                    self._record(
                        execution.run_id,
                        "task_replan_blocked",
                        task,
                        step,
                        reason=review.reason,
                    )
                    self._notify(
                        observer,
                        "task_replan_blocked",
                        task,
                        step,
                        reason=review.reason,
                    )
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step)
                    return TaskRunResult(task, step, "task_replan_blocked", True)
            elif review.decision is PlanReviewDecision.REVISE_REMAINING:
                replan_count = self.store.count_revisions(
                    task.task_id, exclude_initial=True
                )
                task_budget = self.store.get_task_budget(task.task_id)
                effective_max_replans = min(self.limits.max_replans_per_task, task_budget.max_replans)
                if replan_count >= effective_max_replans:
                    task = self.store.block_task(
                        task.task_id, summary="replan_limit_exceeded"
                    )
                    self._record(
                        execution.run_id,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="replan_limit_exceeded",
                    )
                    self._notify(
                        observer,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="replan_limit_exceeded",
                    )
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step)
                    return TaskRunResult(task, step, "task_replan_blocked", True)

                try:
                    task, revision, new_steps, superseded_steps = (
                        self.store.apply_plan_revision(
                            task.task_id,
                            trigger_step_id=step.step_id,
                            reason=review.reason,
                            remaining_steps=list(review.remaining_steps),
                        )
                    )
                except ReplanLimitExceededError:
                    task = self.store.block_task(
                        task.task_id, summary="replan_limit_exceeded"
                    )
                    self._record(
                        execution.run_id,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="replan_limit_exceeded",
                    )
                    self._notify(
                        observer,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="replan_limit_exceeded",
                    )
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step)
                    return TaskRunResult(task, step, "task_replan_blocked", True)
                except (PlanValidationError, TaskValidationError) as exc:
                    task = self.store.block_task(
                        task.task_id, summary=f"invalid_plan_revision: {exc}"
                    )
                    self._record(
                        execution.run_id,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="invalid_plan_revision",
                    )
                    self._notify(
                        observer,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="invalid_plan_revision",
                    )
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step)
                    return TaskRunResult(task, step, "task_replan_blocked", True)
                except Exception as exc:
                    task = self.store.block_task(
                        task.task_id, summary=f"plan_review_failed: {type(exc).__name__}"
                    )
                    self._record(
                        execution.run_id,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="plan_review_failed",
                    )
                    self._notify(
                        observer,
                        "task_replan_blocked",
                        task,
                        step,
                        reason="plan_review_failed",
                    )
                    self._record(execution.run_id, "task_blocked", task, step)
                    self._notify(observer, "task_blocked", task, step)
                    return TaskRunResult(task, step, "task_replan_blocked", True)

                for s_step in superseded_steps:
                    self._record(
                        execution.run_id,
                        "task_step_superseded",
                        task,
                        s_step,
                        superseded_by_revision=revision.revision_id,
                    )
                    self._notify(
                        observer,
                        "task_step_superseded",
                        task,
                        s_step,
                        superseded_by_revision=revision.revision_id,
                    )
                self._record(
                    execution.run_id,
                    "task_plan_revised",
                    task,
                    step,
                    revision_id=revision.revision_id,
                    revision_number=revision.revision_number,
                    old_remaining_count=len(superseded_steps),
                    new_remaining_count=len(new_steps),
                    reason=revision.reason,
                )
                self._notify(
                    observer,
                    "task_plan_revised",
                    task,
                    step,
                    revision_id=revision.revision_id,
                    revision_number=revision.revision_number,
                    old_remaining_count=len(superseded_steps),
                    new_remaining_count=len(new_steps),
                    reason=revision.reason,
                )

        if task.status is TaskStatus.RUNNING:
            remaining_count = self.store.count_remaining_steps(task.task_id)
            if remaining_count == 0:
                contract = self.store.get_goal_contract(task.task_id)
                all_steps = self.store.list_steps(task.task_id)
                self._record(execution.run_id, "goal_verification_started", task, step)
                self._notify(observer, "goal_verification_started", task, step)

                evidence = {
                    "step_count": len(all_steps),
                    "completed_steps": [
                        {
                            "position": s.position,
                            "title": s.title,
                            "status": s.status.value,
                            "verification_status": s.verification_status.value if s.verification_status else None,
                            "summary": s.verification_summary,
                        }
                        for s in all_steps
                    ],
                    "final_step_result": execution.result,
                    "tool_calls": list(execution.tool_calls),
                }
                latest_rev = self.store.list_revisions(task.task_id)
                rev_num = latest_rev[-1].revision_number if latest_rev else 0
                evidence["recovered_step_ids"] = [
                    r.trigger_step_id for r in latest_rev if r.trigger_step_id
                ]

                goal_ver = self.goal_verifier.verify(
                    task, contract, all_steps, execution_evidence=evidence
                )
                goal_ver = TaskGoalVerification(
                    verification_id=goal_ver.verification_id,
                    task_id=task.task_id,
                    status=goal_ver.status,
                    criterion_results=goal_ver.criterion_results,
                    summary=goal_ver.summary,
                    plan_revision_number=rev_num,
                    evidence_hash=goal_ver.evidence_hash,
                    created_at=goal_ver.created_at,
                )
                self.store.record_goal_verification(task.task_id, goal_ver)

                for cr in goal_ver.criterion_results:
                    self._record(
                        execution.run_id,
                        "goal_criterion_evaluated",
                        task,
                        step,
                        criterion_id=cr.criterion_id,
                        criterion_status=cr.status.value,
                        summary=cr.evidence_summary,
                    )
                    self._notify(
                        observer,
                        "goal_criterion_evaluated",
                        task,
                        step,
                        criterion_id=cr.criterion_id,
                        criterion_status=cr.status.value,
                        summary=cr.evidence_summary,
                    )

                if goal_ver.status is GoalVerificationStatus.PASS:
                    task = self.store.complete_task(task.task_id)
                    self._record(
                        execution.run_id,
                        "goal_verification_passed",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                    self._notify(
                        observer,
                        "goal_verification_passed",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                elif goal_ver.status is GoalVerificationStatus.FAIL_REPLANABLE:
                    self._record(
                        execution.run_id,
                        "goal_verification_failed",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                    self._notify(
                        observer,
                        "goal_verification_failed",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                    replan_count = self.store.count_revisions(task.task_id, exclude_initial=True)
                    if self.reviewer is not None and replan_count < self.limits.max_replans_per_task:
                        try:
                            review = self.reviewer.review(
                                task, step, execution, verification, all_steps
                            )
                            if review.decision is PlanReviewDecision.REVISE_REMAINING and review.remaining_steps:
                                task, revision, new_steps, superseded_steps = (
                                    self.store.apply_plan_revision(
                                        task.task_id,
                                        trigger_step_id=step.step_id,
                                        reason=f"Goal verification required replan: {goal_ver.summary}",
                                        remaining_steps=list(review.remaining_steps),
                                    )
                                )
                                self._record(
                                    execution.run_id,
                                    "goal_verification_triggered_replan",
                                    task,
                                    step,
                                    revision_id=revision.revision_id,
                                    revision_number=revision.revision_number,
                                    new_remaining_count=len(new_steps),
                                )
                                self._notify(
                                    observer,
                                    "goal_verification_triggered_replan",
                                    task,
                                    step,
                                    revision_id=revision.revision_id,
                                    revision_number=revision.revision_number,
                                    new_remaining_count=len(new_steps),
                                )
                            else:
                                task = self.store.block_task(
                                    task.task_id, summary=f"goal_verification_failed: {goal_ver.summary}"
                                )
                                self._record(execution.run_id, "goal_verification_blocked", task, step)
                                self._notify(observer, "goal_verification_blocked", task, step)
                        except Exception as exc:
                            task = self.store.block_task(
                                task.task_id, summary=f"goal_verification_replan_failed: {type(exc).__name__}"
                            )
                            self._record(execution.run_id, "goal_verification_blocked", task, step)
                            self._notify(observer, "goal_verification_blocked", task, step)
                    else:
                        task = self.store.block_task(
                            task.task_id, summary="goal_not_verified_replan_limit"
                        )
                        self._record(
                            execution.run_id,
                            "goal_verification_blocked",
                            task,
                            step,
                            reason="goal_not_verified_replan_limit",
                        )
                        self._notify(
                            observer,
                            "goal_verification_blocked",
                            task,
                            step,
                            reason="goal_not_verified_replan_limit",
                        )
                elif goal_ver.status is GoalVerificationStatus.FAIL_TERMINAL:
                    task = self.store.fail_task(task.task_id, summary=goal_ver.summary)
                    self._record(
                        execution.run_id,
                        "goal_verification_failed",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                    self._notify(
                        observer,
                        "goal_verification_failed",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                else:  # BLOCKED or UNKNOWN
                    task = self.store.block_task(task.task_id, summary=goal_ver.summary)
                    self._record(
                        execution.run_id,
                        "goal_verification_blocked",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )
                    self._notify(
                        observer,
                        "goal_verification_blocked",
                        task,
                        step,
                        verification_id=goal_ver.verification_id,
                        summary=goal_ver.summary,
                    )

        if task.status is TaskStatus.COMPLETED:
            self._record(execution.run_id, "task_completed", task, step)
            self._notify(observer, "task_completed", task, step)
        elif task.status is TaskStatus.BLOCKED:
            self._record(execution.run_id, "task_blocked", task, step)
            self._notify(observer, "task_blocked", task, step)
        elif task.status is TaskStatus.FAILED:
            self._record(execution.run_id, "task_failed", task, step)
            self._notify(observer, "task_failed", task, step)
        return TaskRunResult(task, step, terminal_event, True)

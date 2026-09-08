"""Deterministic verification suite for M31 — Evidence-Driven Failure Recovery & Replanning Activation.

Covers:
- Suite 1: Failure Classification (10 tests)
- Suite 2: Strategy Fingerprinting & Rejection (10 tests)
- Suite 3: History Immutability & Status Semantics (10 tests)
- Suite 4: Recovery Replanning Execution & Bounded Evidence (10 tests)
- Suite 5: Completion Query & Low-Budget Policy Audit (10 tests)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from tieru.tasks.executor import TaskExecutor
from tieru.tasks.failure_recovery import (
    StepFailureClassifier,
    compute_strategy_fingerprint,
    extract_bounded_failure_evidence,
)
from tieru.tasks.goal_verifier import DeterministicGoalVerifier
from tieru.tasks.models import (
    BudgetResource,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    PlanValidationError,
    StepEvidenceRequirement,
    StepExecution,
    StepExecutionKind,
    StepFailureDisposition,
    StepStatus,
    TaskBudget,
    TaskLimits,
    TaskStateError,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.reviewer import CallablePlanReviewer, parse_plan_review_output
from tieru.tasks.store import TaskStore, initialize_task_schema


class MockReplay:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def record_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((run_id, kind, dict(payload)))


def _init_store(conn: sqlite3.Connection, limits: TaskLimits | None = None) -> TaskStore:
    initialize_task_schema(conn)
    return TaskStore(conn, limits=limits or TaskLimits())


def _make_step(
    position: int = 1,
    title: str = "Test Step",
    instruction: str = "Do something in math_lib.py",
    kind: StepExecutionKind = StepExecutionKind.COMMAND,
    requirements: tuple[StepEvidenceRequirement, ...] = (),
    verification_instruction: str = "Exit code 0",
) -> TaskStep:
    return TaskStep(
        step_id=f"step_{position}",
        task_id="task_1",
        position=position,
        title=title,
        instruction=instruction,
        verification_instruction=verification_instruction,
        status=StepStatus.RUNNING,
        attempt_count=0,
        max_attempts=1,
        result=None,
        result_size=0,
        result_truncated=False,
        verification_status=None,
        verification_summary=None,
        execution_run_id=None,
        started_at=None,
        completed_at=None,
        updated_at="2026-09-03T00:00:00Z",
        execution_kind=kind,
        evidence_requirements=requirements,
    )


class MockRunner:
    def __init__(self, execution: StepExecution) -> None:
        self.execution = execution
        self.calls: list[TaskStep] = []

    def __call__(self, task, step, context, observer=None) -> StepExecution:
        self.calls.append(step)
        return self.execution


class MockVerifier:
    def __init__(self, result: VerificationResult) -> None:
        self.result = result

    def verify(self, task, step, execution) -> VerificationResult:
        return self.result


# ==============================================================================
# Suite 1: Failure Classification (10 tests)
# ==============================================================================


def test_01_successful_step_not_classified_as_failure():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Inspect code", kind=StepExecutionKind.READ)
    execution = StepExecution(result="File read ok", run_id="run_1")
    verification = VerificationResult(VerificationStatus.PASS, "Step verified ok")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
    )
    assert assessment.disposition is not StepFailureDisposition.REPLAN
    assert assessment.is_recoverable is False


def test_02_command_exit_nonzero_with_budget_classified_replan():
    classifier = StepFailureClassifier()
    step = _make_step(2, title="Run tests", instruction="pytest test_math.py", kind=StepExecutionKind.COMMAND)
    execution = StepExecution(
        result="FAIL test_math.py::test_add",
        run_id="run_1",
        tool_calls=({"tool": "run_command", "result": {"exit_code": 1}},),
    )
    verification = VerificationResult(VerificationStatus.FAIL, "command_exit_zero: test exited with code 1")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
    )
    assert assessment.disposition is StepFailureDisposition.REPLAN
    assert assessment.is_recoverable is True
    assert assessment.reason_code == "command_test_failure"


def test_03_command_test_failure_classified_replan():
    classifier = StepFailureClassifier()
    step = _make_step(2, title="Execute pytest", instruction="Run test suite", kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="AssertionError: 2 != 3", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "Required observable evidence missing: command_exit_zero")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=2.0,
    )
    assert assessment.disposition is StepFailureDisposition.REPLAN
    assert assessment.is_recoverable is True


def test_04_artifact_missing_classified_replan():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Write output", instruction="Write to config.ini", kind=StepExecutionKind.WRITE)
    execution = StepExecution(result="Wrote nothing", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "Required artifact_changed missing for config.ini")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=2.0,
    )
    assert assessment.disposition is StepFailureDisposition.REPLAN
    assert assessment.is_recoverable is True
    assert assessment.reason_code == "artifact_verification_failure"


def test_05_hard_trust_denial_classified_block():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Delete files", instruction="rm -rf /")
    execution = StepExecution(result="Denied by trust", run_id="run_1")
    verification = VerificationResult(VerificationStatus.BLOCKED, "Hard Trust policy denial cannot be bypassed.")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
    )
    assert assessment.disposition is StepFailureDisposition.BLOCK
    assert assessment.is_recoverable is False
    assert assessment.reason_code == "hard_trust_denial"


def test_06_action_ledger_uncertain_classified_block():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Call API", kind=StepExecutionKind.EXTERNAL_ACTION)
    execution = StepExecution(
        result="External call pending",
        run_id="run_1",
        tool_calls=({"tool": "deploy", "error_code": "action_ledger_uncertain"},),
    )
    verification = VerificationResult(VerificationStatus.FAIL, "External state uncertain")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
    )
    assert assessment.disposition is StepFailureDisposition.BLOCK
    assert assessment.is_recoverable is False
    assert assessment.reason_code == "action_ledger_uncertain"


def test_07_budget_exhausted_classified_block():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Read file", kind=StepExecutionKind.READ)
    execution = StepExecution(result="budget_exhausted:model_calls", run_id="run_1")
    verification = VerificationResult(VerificationStatus.BLOCKED, "budget_exhausted:model_calls")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=0.0,
    )
    assert assessment.disposition is StepFailureDisposition.BLOCK
    assert assessment.is_recoverable is False
    assert assessment.reason_code == "budget_exhausted"


def test_08_replan_budget_exhausted_classified_fail():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Retry test", kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="Failed again", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=2,
        effective_max_replans=2,
        remaining_model_calls=5.0,
    )
    assert assessment.disposition is StepFailureDisposition.FAIL
    assert assessment.is_recoverable is False
    assert assessment.reason_code == "replan_budget_exhausted"


def test_09_model_call_budget_exhausted_classified_block():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Execute step", kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="Test failed", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=0.5,
    )
    assert assessment.disposition is StepFailureDisposition.BLOCK
    assert assessment.is_recoverable is False
    assert assessment.reason_code == "budget_exhausted:model_calls"


def test_10_immutable_constraint_violation_classified_fail():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Modify base", instruction="touch base.txt", kind=StepExecutionKind.WRITE)
    execution = StepExecution(result="Modified base.txt", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "constraint_violated: base.txt was modified")
    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
    )
    assert assessment.disposition is StepFailureDisposition.FAIL
    assert assessment.is_recoverable is False
    assert assessment.reason_code == "constraint_violation"


# ==============================================================================
# Suite 2: Strategy Fingerprinting & Rejection (10 tests)
# ==============================================================================


def test_11_fingerprint_computation_captures_kind_and_targets():
    step = PlanStep("Run test", "pytest test_math.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)
    fp = compute_strategy_fingerprint(step)
    assert "command" in fp
    assert "pytest" in fp
    assert "test_math.py" in fp


def test_12_identical_strategy_fingerprint_detected():
    classifier = StepFailureClassifier()
    step = _make_step(1, title="Run math test", instruction="pytest test_math.py", kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="Failed", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")
    fp = compute_strategy_fingerprint(step)

    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
        previous_failed_fingerprints={fp},
    )
    assert assessment.disposition is StepFailureDisposition.FAIL
    assert assessment.reason_code == "repeated_failed_strategy"


def test_13_different_strategy_fingerprint_allowed():
    classifier = StepFailureClassifier()
    step = _make_step(2, title="Edit math_lib", instruction="Fix function in math_lib.py", kind=StepExecutionKind.WRITE)
    execution = StepExecution(result="Failed", run_id="run_1")
    verification = VerificationResult(VerificationStatus.FAIL, "artifact_changed missing")

    assessment = classifier.classify(
        task=None,
        step=step,
        execution=execution,
        verification=verification,
        replan_count=0,
        effective_max_replans=2,
        remaining_model_calls=3.0,
        previous_failed_fingerprints={"command:pytest,test_math.py"},
    )
    assert assessment.disposition is StepFailureDisposition.REPLAN
    assert assessment.is_recoverable is True


def test_14_repeated_strategy_rejected_with_replay_event():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()

    task = store.create_task(
        "Fix math bug",
        [PlanStep("Run test", "pytest test_math.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)],
        budget=TaskBudget(max_model_calls=5, max_steps=4, max_replans=2),
    )

    # Reviewer proposes the exact same command step that failed
    def bad_review(task, step, execution, verification, all_steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Retrying exact same step",
            remaining_steps=(
                PlanStep("Run test", "pytest test_math.py", "exit 0", execution_kind=StepExecutionKind.COMMAND),
            ),
        )

    runner = MockRunner(StepExecution(result="Failed", run_id="run_1"))
    verifier = MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing"))

    executor = TaskExecutor(
        store,
        runner,
        verifier,
        reviewer=CallablePlanReviewer(bad_review),
        replay=replay,
    )
    res = executor.run_next(task.task_id)
    assert res.code == "task_recovery_strategy_rejected"
    assert res.task.status is TaskStatus.FAILED

    events = [e[1] for e in replay.events]
    assert "task_recovery_strategy_rejected" in events
    assert "task_failed" in events


def test_15_repeated_failed_strategy_terminates_task():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task(
        "Fix defect",
        [PlanStep("Edit file", "edit parser.py", "changed", execution_kind=StepExecutionKind.WRITE)],
        budget=TaskBudget(max_model_calls=5, max_steps=4, max_replans=2),
    )

    def bad_review(t, s, e, v, steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Same write",
            remaining_steps=(PlanStep("Edit file", "edit parser.py", "changed", execution_kind=StepExecutionKind.WRITE),),
        )

    executor = TaskExecutor(
        store,
        MockRunner(StepExecution(result="Write fail", run_id="run_1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "artifact_changed missing")),
        reviewer=CallablePlanReviewer(bad_review),
    )
    res = executor.run_next(task.task_id)
    assert res.task.status is TaskStatus.FAILED
    assert store.get_task(task.task_id).status is TaskStatus.FAILED


def test_16_fingerprint_normalization_ignores_casing_and_punctuation():
    step1 = PlanStep("Run test!", "pytest `test_math.py`;", "exit 0", execution_kind=StepExecutionKind.COMMAND)
    step2 = PlanStep("run TEST", "PYTEST test_math.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)
    assert compute_strategy_fingerprint(step1) == compute_strategy_fingerprint(step2)


def test_17_multiple_failed_strategies_tracked_across_revisions():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("Step 1", "python -m pytest test_1.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)],
    )
    claim = store.claim_next_step(task.task_id)
    # Finish step 1 as failed with task running
    store.finish_step(claim.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)

    executor = TaskExecutor(store, MockRunner(StepExecution("", "")), MockVerifier(VerificationResult(VerificationStatus.PASS, "")))
    fingerprints = executor._get_failed_fingerprints(task.task_id)
    assert any("test_1.py" in fp for fp in fingerprints)


def test_18_strategy_rejection_does_not_consume_additional_replan():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("Step 1", "pytest test.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)],
        budget=TaskBudget(max_replans=2),
    )
    def bad_review(t, s, e, v, steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Repeat",
            remaining_steps=(PlanStep("Step 1", "pytest test.py", "exit 0", execution_kind=StepExecutionKind.COMMAND),),
        )
    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("fail", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "fail")),
        reviewer=CallablePlanReviewer(bad_review),
    )
    executor.run_next(task.task_id)
    assert store.count_revisions(task.task_id, exclude_initial=True) == 0


def test_19_coarse_fingerprint_handles_reasoning_steps_without_files():
    step = PlanStep("Reason about architecture", "Analyze tradeoffs", "Conclusion", execution_kind=StepExecutionKind.REASONING)
    fp = compute_strategy_fingerprint(step)
    assert fp.startswith("reasoning:")


def test_20_fingerprint_handles_command_execution_with_scripts():
    step = PlanStep("Run bash script", "bash deploy.sh", "success", execution_kind=StepExecutionKind.COMMAND)
    fp = compute_strategy_fingerprint(step)
    assert "command" in fp
    assert "deploy.sh" in fp


# ==============================================================================
# Suite 3: History Immutability & Status Semantics (10 tests)
# ==============================================================================


def test_21_failed_step_remains_failed_in_store():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Test goal", [PlanStep("S1", "I1", "V1")])
    claim = store.claim_next_step(task.task_id)
    task, step = store.finish_step(
        claim.step.step_id,
        result="Failure output",
        verification=VerificationResult(VerificationStatus.FAIL, "Missing evidence"),
        task_status=TaskStatus.RUNNING,
    )
    assert step.status is StepStatus.FAILED
    assert store.get_step(step.step_id).status is StepStatus.FAILED


def test_22_failed_step_result_and_verification_preserved_in_sqlite():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Test goal", [PlanStep("S1", "I1", "V1")])
    claim = store.claim_next_step(task.task_id)
    store.finish_step(
        claim.step.step_id,
        result="Specific error message 404",
        verification=VerificationResult(VerificationStatus.FAIL, "Verification failure detail"),
        task_status=TaskStatus.RUNNING,
    )
    step = store.get_step(claim.step.step_id)
    assert step.result == "Specific error message 404"
    assert step.verification_summary == "Verification failure detail"
    assert step.verification_status == "fail"


def test_23_task_remains_running_when_recoverable_failure_occurs():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Test goal", [PlanStep("S1", "I1", "V1")])
    claim = store.claim_next_step(task.task_id)
    task, _ = store.finish_step(
        claim.step.step_id,
        result="Fail",
        verification=VerificationResult(VerificationStatus.FAIL, "Fail"),
        task_status=TaskStatus.RUNNING,
    )
    assert task.status is TaskStatus.RUNNING
    assert store.get_task(task.task_id).status is TaskStatus.RUNNING


def test_24_task_does_not_transition_to_failed_before_replan():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("Step 1", "pytest test_a.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)],
        budget=TaskBudget(max_replans=1),
    )
    def ok_review(t, s, e, v, steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Fix code first",
            remaining_steps=(
                PlanStep("Edit code", "edit lib.py", "changed", execution_kind=StepExecutionKind.WRITE),
                PlanStep("Re-test", "pytest test_a.py", "exit 0", execution_kind=StepExecutionKind.COMMAND),
            ),
        )
    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("Exit 1", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")),
        reviewer=CallablePlanReviewer(ok_review),
    )
    res = executor.run_next(task.task_id)
    assert res.task.status is TaskStatus.RUNNING
    assert res.code == "task_recovery_replan_applied"


def test_25_executed_failed_step_cannot_be_marked_superseded():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    claim = store.claim_next_step(task.task_id)
    _, failed_step = store.finish_step(
        claim.step.step_id,
        result="err",
        verification=VerificationResult(VerificationStatus.FAIL, "err"),
        task_status=TaskStatus.RUNNING,
    )
    store.apply_plan_revision(
        task.task_id,
        trigger_step_id=failed_step.step_id,
        reason="Recovery revision",
        remaining_steps=[PlanStep("S2", "I2", "V2")],
    )
    # The failed step MUST remain StepStatus.FAILED, not SUPERSEDED
    assert store.get_step(failed_step.step_id).status is StepStatus.FAILED


def test_26_only_pending_unexecuted_steps_become_superseded():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task(
        "Goal",
        [
            PlanStep("S1", "I1", "V1", execution_kind=StepExecutionKind.COMMAND),
            PlanStep("S2", "I2", "V2", execution_kind=StepExecutionKind.READ),
        ],
    )
    claim1 = store.claim_next_step(task.task_id)
    store.finish_step(
        claim1.step.step_id,
        result="err",
        verification=VerificationResult(VerificationStatus.FAIL, "fail"),
        task_status=TaskStatus.RUNNING,
    )
    task, _rev, _new_steps, superseded = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=claim1.step.step_id,
        reason="Recover from S1",
        remaining_steps=[PlanStep("S3", "I3", "V3", execution_kind=StepExecutionKind.WRITE)],
    )
    assert len(superseded) == 1
    assert superseded[0].title == "S2"
    assert store.get_step(superseded[0].step_id).status is StepStatus.SUPERSEDED
    assert store.get_step(claim1.step.step_id).status is StepStatus.FAILED


def test_27_attempt_count_and_run_id_preserved_on_failed_step():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    claim = store.claim_next_step(task.task_id)
    store.attach_run_id(claim.step.step_id, "run_xyz_123")
    store.finish_step(
        claim.step.step_id,
        result="fail",
        verification=VerificationResult(VerificationStatus.FAIL, "fail"),
        task_status=TaskStatus.RUNNING,
    )
    step = store.get_step(claim.step.step_id)
    assert step.attempt_count == 1
    assert step.execution_run_id == "run_xyz_123"


def test_28_list_steps_reflects_full_chronological_history():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    claim = store.claim_next_step(task.task_id)
    store.finish_step(claim.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    store.apply_plan_revision(task.task_id, trigger_step_id=claim.step.step_id, reason="Recover", remaining_steps=[PlanStep("S2", "I2", "V2")])

    all_steps = store.list_steps(task.task_id)
    assert len(all_steps) == 2
    assert all_steps[0].status is StepStatus.FAILED
    assert all_steps[1].status is StepStatus.PENDING


def test_29_replay_events_contain_truthful_step_failed_event():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1", execution_kind=StepExecutionKind.COMMAND)])

    def ok_review(t, s, e, v, steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Recover",
            remaining_steps=(PlanStep("S2", "I2", "V2", execution_kind=StepExecutionKind.WRITE),),
        )

    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("Exit 1", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")),
        reviewer=CallablePlanReviewer(ok_review),
        replay=replay,
    )
    executor.run_next(task.task_id)
    event_types = [e[1] for e in replay.events]
    assert "task_step_failed" in event_types
    assert "task_recovery_replan_requested" in event_types
    assert "task_recovery_replan_applied" in event_types


def test_30_step_failure_classified_event_recorded():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1", execution_kind=StepExecutionKind.COMMAND)])
    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("fail", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")),
        replay=replay,
    )
    executor.run_next(task.task_id)
    classified_events = [e for e in replay.events if e[1] == "step_failure_classified"]
    assert len(classified_events) == 1
    assert classified_events[0][2]["is_recoverable"] is True
    assert classified_events[0][2]["disposition"] == "replan"


# ==============================================================================
# Suite 4: Recovery Replanning Execution & Bounded Evidence (10 tests)
# ==============================================================================


def test_31_bounded_failure_evidence_does_not_leak_secrets():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("goal", [PlanStep("S1", "I1", "V1")])
    step = _make_step(1, title="Step 1")
    execution = StepExecution(result="secret token is bearer_tok_abcdef123456", run_id="r1")
    verification = VerificationResult(VerificationStatus.FAIL, "Failed with key api_key_xyz987654")

    ev = extract_bounded_failure_evidence(task, step, execution, verification)
    ev_str = json.dumps(ev)
    assert "bearer_tok_abcdef123456" not in ev_str
    assert "api_key_xyz987654" not in ev_str


def test_32_bounded_failure_evidence_truncates_large_output():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("goal", [PlanStep("S1", "I1", "V1")])
    step = _make_step(1, title="Step 1")
    large_result = "A" * 5000
    large_summary = "B" * 3000
    execution = StepExecution(result=large_result, run_id="r1")
    verification = VerificationResult(VerificationStatus.FAIL, large_summary)

    ev = extract_bounded_failure_evidence(task, step, execution, verification)
    assert len(ev["result_preview"].encode("utf-8")) <= 2048
    assert len(ev["verification_summary"].encode("utf-8")) <= 1024


def test_33_reviewer_receives_failed_step_context_and_reason():
    received: list[dict[str, Any]] = []
    def custom_review(task, step, execution, verification, all_steps):
        received.append({
            "step_id": step.step_id,
            "v_status": verification.status,
            "v_summary": verification.summary,
        })
        return PlanReviewResult(PlanReviewDecision.KEEP, "Keep", ())

    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1", execution_kind=StepExecutionKind.COMMAND)])
    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("Exit 1", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")),
        reviewer=CallablePlanReviewer(custom_review),
    )
    executor.run_next(task.task_id)
    assert len(received) == 1
    assert received[0]["v_status"] is VerificationStatus.FAIL
    assert "command_exit_zero" in received[0]["v_summary"]


def test_34_reviewer_parses_replacement_steps_with_m29_evidence():
    json_out = json.dumps({
        "decision": "revise_remaining",
        "reason": "Alternative fix path",
        "remaining_steps": [
            {
                "title": "Fix bug in code",
                "instruction": "Update math_lib.py",
                "verification": "artifact_changed in math_lib.py",
                "execution_kind": "write",
                "evidence_requirements": [
                    {"kind": "artifact_changed", "description": "math_lib.py modified", "required": True}
                ],
            }
        ],
    })
    res = parse_plan_review_output(json_out)
    assert res.decision is PlanReviewDecision.REVISE_REMAINING
    assert len(res.remaining_steps) == 1
    s = res.remaining_steps[0]
    assert s.execution_kind is StepExecutionKind.WRITE
    assert len(s.evidence_requirements) == 1
    assert s.evidence_requirements[0].kind == "artifact_changed"


def test_35_replan_revision_applied_atomically():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1", execution_kind=StepExecutionKind.COMMAND)])
    claim = store.claim_next_step(task.task_id)
    store.finish_step(claim.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)

    task, rev, new_steps, superseded = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=claim.step.step_id,
        reason="Recovery revision 1",
        remaining_steps=[
            PlanStep("S2", "I2", "V2", execution_kind=StepExecutionKind.WRITE),
            PlanStep("S3", "I3", "V3", execution_kind=StepExecutionKind.COMMAND),
        ],
    )
    assert rev.revision_number == 1
    assert len(new_steps) == 2
    assert len(superseded) == 0


def test_36_revision_number_increments_monotonically():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")], budget=TaskBudget(max_replans=3))
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    _, rev1, _, _ = store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Rev 1", remaining_steps=[PlanStep("S2", "I2", "V2")])
    assert rev1.revision_number == 1

    c2 = store.claim_next_step(task.task_id)
    store.finish_step(c2.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    _, rev2, _, _ = store.apply_plan_revision(task.task_id, trigger_step_id=c2.step.step_id, reason="Rev 2", remaining_steps=[PlanStep("S3", "I3", "V3")])
    assert rev2.revision_number == 2


def test_37_replans_budget_decremented_on_recovery():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")], budget=TaskBudget(max_replans=2))
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Rev 1", remaining_steps=[PlanStep("S2", "I2", "V2")])

    usage = store.get_task_budget_usage(task.task_id)
    assert usage.replans == 1


def test_38_recovery_replan_applied_event_emitted():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1", execution_kind=StepExecutionKind.COMMAND)])

    def ok_review(t, s, e, v, steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Recovery rev",
            remaining_steps=(
                PlanStep("S2", "I2", "V2", execution_kind=StepExecutionKind.WRITE),
                PlanStep("S3", "I3", "V3", execution_kind=StepExecutionKind.COMMAND),
            ),
        )

    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("Exit 1", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")),
        reviewer=CallablePlanReviewer(ok_review),
        replay=replay,
    )
    executor.run_next(task.task_id)
    applied_events = [e for e in replay.events if e[1] == "task_recovery_replan_applied"]
    assert len(applied_events) == 1
    assert applied_events[0][2]["new_remaining_count"] == 2


def test_39_executor_continues_to_replacement_step():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "pytest test_a.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)])

    def ok_review(t, s, e, v, steps):
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Recover",
            remaining_steps=(PlanStep("S2", "patch lib.py", "changed", execution_kind=StepExecutionKind.WRITE),),
        )

    runner_calls: list[str] = []
    def custom_runner(task, step, context, observer=None):
        runner_calls.append(step.title)
        return StepExecution("ok", "r1")

    executor = TaskExecutor(
        store,
        custom_runner,
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "fail")),
        reviewer=CallablePlanReviewer(ok_review),
        limits=TaskLimits(max_execution_turns_per_step=1),
    )
    # Turn 1: S1 fails and replans
    res1 = executor.run_next(task.task_id)
    assert res1.code == "task_recovery_replan_applied"

    # Turn 2: S2 executes
    executor.verifier = MockVerifier(VerificationResult(VerificationStatus.PASS, "pass"))
    _ = executor.run_next(task.task_id)
    assert len(runner_calls) == 2
    assert runner_calls[1] == "S2"


def test_40_idempotency_trigger_does_not_duplicate_revision():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)

    _t1, r1, _n1, _ = store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Rev 1", remaining_steps=[PlanStep("S2", "I2", "V2")])
    _t2, r2, _n2, _ = store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Rev 1 duplicate", remaining_steps=[PlanStep("S3", "I3", "V3")])
    assert r1.revision_id == r2.revision_id
    assert store.count_revisions(task.task_id, exclude_initial=True) == 1


# ==============================================================================
# Suite 5: Completion Query & Low-Budget Policy Audit (10 tests)
# ==============================================================================


def test_41_count_remaining_steps_ignores_historical_failed_steps():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Rev 1", remaining_steps=[PlanStep("S2", "I2", "V2")])

    c2 = store.claim_next_step(task.task_id)
    store.finish_step(c2.step.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "pass"))

    # S1 is failed, S2 is succeeded. Remaining pending/running steps is 0!
    assert store.count_remaining_steps(task.task_id) == 0


def test_42_task_completes_when_replacement_steps_succeed():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Rev 1", remaining_steps=[PlanStep("S2", "I2", "V2")])

    c2 = store.claim_next_step(task.task_id)
    store.finish_step(c2.step.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "pass"))

    assert store.get_task(task.task_id).status is TaskStatus.COMPLETED


def test_43_goal_verification_evaluates_history_including_recovered_failure():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="fail 1", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Recover", remaining_steps=[PlanStep("S2", "I2", "V2")])
    c2 = store.claim_next_step(task.task_id)
    store.finish_step(c2.step.step_id, result="pass 2", verification=VerificationResult(VerificationStatus.PASS, "pass"), task_status=TaskStatus.RUNNING)

    all_steps = store.list_steps(task.task_id)
    assert len(all_steps) == 2
    assert all_steps[0].status is StepStatus.FAILED
    assert all_steps[1].status is StepStatus.SUCCEEDED


def test_44_goal_false_pass_prevented_if_replacement_steps_fail():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)
    store.apply_plan_revision(task.task_id, trigger_step_id=c1.step.step_id, reason="Recover", remaining_steps=[PlanStep("S2", "I2", "V2")])
    c2 = store.claim_next_step(task.task_id)
    store.finish_step(c2.step.step_id, result="err2", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.FAILED)

    with pytest.raises(TaskStateError):
        store.complete_task(task.task_id)


def test_45_failed_step_under_low_model_budget_blocks_not_keeps():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()
    task = store.create_task(
        "Goal",
        [PlanStep("S1", "pytest test_a.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)],
        budget=TaskBudget(max_model_calls=1, max_replans=1),
    )
    # Model call consumed by step execution
    store.record_budget_consumption(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("Exit 1", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "command_exit_zero missing")),
        reviewer=CallablePlanReviewer(lambda *args: PlanReviewResult(PlanReviewDecision.KEEP, "keep", ())),
        replay=replay,
    )
    res = executor.run_next(task.task_id)
    # Must block on budget, NEVER blindly keep the failed plan
    assert res.task.status is TaskStatus.BLOCKED
    assert res.code == "budget_exhausted:model_calls"


def test_46_successful_step_under_low_model_budget_keeps_plan():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()
    task = store.create_task(
        "Goal",
        [
            PlanStep("S1", "read a.py", "read", execution_kind=StepExecutionKind.READ),
            PlanStep("S2", "read b.py", "read", execution_kind=StepExecutionKind.READ),
        ],
        budget=TaskBudget(max_model_calls=1),
    )
    store.record_budget_consumption(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("content", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.PASS, "pass")),
        reviewer=CallablePlanReviewer(lambda *args: PlanReviewResult(PlanReviewDecision.KEEP, "keep", ())),
        replay=replay,
    )
    res = executor.run_next(task.task_id)
    assert res.task.status is TaskStatus.RUNNING
    reviewed_events = [e for e in replay.events if e[1] == "task_plan_reviewed"]
    assert len(reviewed_events) == 1
    assert reviewed_events[0][2]["reason"] == "preserved_existing_plan_low_review_budget"


def test_47_unrecoverable_terminal_step_failure_fails_task():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    # No reviewer configured -> unrecoverable
    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("err", "r1")),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "fail")),
        reviewer=None,
    )
    res = executor.run_next(task.task_id)
    assert res.task.status is TaskStatus.FAILED


def test_48_blocked_task_transitions_to_running_on_replan():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.BLOCKED)

    assert store.get_task(task.task_id).status is TaskStatus.BLOCKED
    updated_task, _rev, _, _ = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=c1.step.step_id,
        reason="Recovery from blocked",
        remaining_steps=[PlanStep("S2", "I2", "V2")],
    )
    assert updated_task.status is TaskStatus.RUNNING
    assert store.get_task(task.task_id).status is TaskStatus.RUNNING


def test_49_max_total_task_steps_enforced_including_failed_steps():
    conn = sqlite3.connect(":memory:")
    limits = TaskLimits(max_steps_per_task=3)
    store = _init_store(conn, limits=limits)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1"), PlanStep("S2", "I2", "V2")])
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(c1.step.step_id, result="err", verification=VerificationResult(VerificationStatus.FAIL, "fail"), task_status=TaskStatus.RUNNING)

    # 2 existing steps + 2 new steps = 4 > 3 -> PlanValidationError
    with pytest.raises(PlanValidationError):
        store.apply_plan_revision(
            task.task_id,
            trigger_step_id=c1.step.step_id,
            reason="Exceed steps limit",
            remaining_steps=[PlanStep("S3", "I3", "V3"), PlanStep("S4", "I4", "V4")],
        )


def test_50_complete_recovery_cycle():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    replay = MockReplay()
    task = store.create_task(
        "Fix math bug so test_math.py passes",
        [PlanStep("Run test", "pytest test_math.py", "exit 0", execution_kind=StepExecutionKind.COMMAND)],
        budget=TaskBudget(max_model_calls=5, max_steps=4, max_replans=1),
    )

    # Reviewer proposes fixing math_lib.py and re-running test
    def recovery_review(t, s, e, v, steps):
        if v.status is VerificationStatus.PASS:
            return PlanReviewResult(PlanReviewDecision.KEEP, "Keep on pass", ())
        return PlanReviewResult(
            decision=PlanReviewDecision.REVISE_REMAINING,
            reason="Fix subtraction to addition in math_lib.py then re-test",
            remaining_steps=(
                PlanStep("Edit math_lib.py", "Change - to + in add()", "artifact_changed in math_lib.py", execution_kind=StepExecutionKind.WRITE),
                PlanStep("Re-run test_math.py", "pytest test_math.py", "exit 0", execution_kind=StepExecutionKind.COMMAND),
            ),
        )

    # Invocation 1: Step 1 (Run test) fails verification
    executor = TaskExecutor(
        store,
        MockRunner(StepExecution("Exit code 1: 1 failed", "run_1", ({"tool": "run_command", "result": {"exit_code": 1}},))),
        MockVerifier(VerificationResult(VerificationStatus.FAIL, "Required observable evidence missing: command_exit_zero")),
        reviewer=CallablePlanReviewer(recovery_review),
        goal_verifier=DeterministicGoalVerifier(),
        replay=replay,
    )
    outcome1 = executor.run_next(task.task_id)
    assert outcome1.code == "task_recovery_replan_applied"
    assert outcome1.task.status is TaskStatus.RUNNING

    # Invocation 2: Step 2 (Edit math_lib.py) succeeds
    executor.runner = MockRunner(StepExecution("Modified math_lib.py", "run_2", ({"tool": "filesystem_write"},)))
    executor.verifier = MockVerifier(VerificationResult(VerificationStatus.PASS, "Observable state change in math_lib.py"))
    outcome2 = executor.run_next(task.task_id)
    assert outcome2.task.status is TaskStatus.RUNNING
    assert outcome2.step.title == "Edit math_lib.py"

    # Invocation 3: Step 3 (Re-run test_math.py) succeeds and completes task
    executor.runner = MockRunner(StepExecution("Exit code 0: 1 passed", "run_3", ({"tool": "run_command", "result": {"exit_code": 0}},)))
    executor.verifier = MockVerifier(VerificationResult(VerificationStatus.PASS, "command_exit_zero satisfied"))
    outcome3 = executor.run_next(task.task_id)
    assert outcome3.task.status is TaskStatus.COMPLETED

    all_steps = store.list_steps(task.task_id)
    assert len(all_steps) == 3
    assert all_steps[0].status is StepStatus.FAILED
    assert all_steps[1].status is StepStatus.SUCCEEDED
    assert all_steps[2].status is StepStatus.SUCCEEDED

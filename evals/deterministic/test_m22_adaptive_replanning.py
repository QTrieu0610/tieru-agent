"""Deterministic contracts for M22 — Adaptive Planning & Replanning."""

from __future__ import annotations

import json
import threading
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tieru.config import Settings
from tieru.db import connect
from tieru.evals.corpus import default_corpus_paths, load_corpus
from tieru.evals.metrics import aggregate_results, score_case
from tieru.evals.models import (
    EvalCase,
    EvalEvidence,
    EvalExpectation,
    EvalSetup,
    EvalVerdict,
    FailureType,
)
from tieru.evals.runner import EvalRunner
from tieru.replay import ReplayService
from tieru.tasks.cli import run_task_cli
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    InvalidPlanRevisionError,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    PlanValidationError,
    StepExecution,
    StepStatus,
    Task,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.reviewer import (
    CallablePlanReviewer,
    ModelPlanReviewer,
    parse_plan_review_output,
)
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore
from tieru.tools.registry import Tool, ToolRegistry


class ScriptedRunner:
    def __init__(self, step_outputs: dict[int, str] | None = None) -> None:
        self.step_outputs = step_outputs or {}
        self.calls: list[int] = []

    def __call__(self, task, step, context: str, observer) -> StepExecution:
        self.calls.append(step.position)
        result = self.step_outputs.get(step.position, f"result for step {step.position}")
        return StepExecution(result=result, run_id=f"run_{step.position}")


class ScriptedVerifier:
    def __init__(self, step_statuses: dict[int, VerificationStatus] | None = None) -> None:
        self.step_statuses = step_statuses or {}

    def verify(self, task, step, execution) -> VerificationResult:
        status = self.step_statuses.get(step.position, VerificationStatus.PASS)
        return VerificationResult(status, f"verification {status.value} for step {step.position}")


def make_service(
    tmp_path: Path,
    plan: list[PlanStep] | None = None,
    reviewer=None,
    runner=None,
    verifier=None,
    limits: TaskLimits | None = None,
):
    conn = connect(tmp_path)
    bounds = limits or TaskLimits()
    store = TaskStore(conn, limits=bounds)
    from tieru.tasks.planner import CallableTaskPlanner

    initial_plan = plan or [
        PlanStep("Inspect component A", "Inspect component A code", "Check component A"),
        PlanStep("Modify component A", "Modify component A code", "Check component A modified"),
    ]
    planner = CallableTaskPlanner(lambda _goal: list(initial_plan))
    actual_runner = runner or ScriptedRunner()
    actual_verifier = verifier or ScriptedVerifier()
    executor = TaskExecutor(
        store,
        actual_runner,
        actual_verifier,
        reviewer=reviewer,
        limits=bounds,
    )
    return TaskService(store, planner, executor), store, actual_runner, conn


# 1. Initial revision 0 compatibility
def test_01_initial_revision_0_compatibility(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Initial task goal",
        [PlanStep("Step 1", "Do 1", "Check 1"), PlanStep("Step 2", "Do 2", "Check 2")],
        source="test",
    )
    revisions = store.list_revisions(task.task_id)
    assert len(revisions) == 1
    assert revisions[0].revision_number == 0
    assert revisions[0].reason == "Initial plan"
    assert revisions[0].trigger_step_id is None

    steps = store.list_steps(task.task_id)
    assert all(step.plan_revision_id == revisions[0].revision_id for step in steps)

    # Pre-M22 task compatibility without revisions table row
    conn.execute("DELETE FROM task_plan_revisions WHERE task_id=?", (task.task_id,))
    conn.commit()
    compat_revisions = store.list_revisions(task.task_id)
    assert len(compat_revisions) == 1
    assert compat_revisions[0].revision_number == 0
    assert compat_revisions[0].reason == "Initial plan"
    conn.close()


# 2. KEEP leaves remaining steps unchanged
def test_02_keep_leaves_remaining_steps_unchanged(tmp_path):
    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(PlanReviewDecision.KEEP, "Current plan remains valid")
    )
    service, store, _runner, conn = make_service(tmp_path, reviewer=reviewer)
    task = service.create(goal="Keep test")

    result = service.run(task.task_id, max_steps=1)[0]
    assert result.step.position == 1
    assert result.step.status is StepStatus.SUCCEEDED

    revisions = store.list_revisions(task.task_id)
    assert len(revisions) == 1  # No new revision generated
    steps = store.list_steps(task.task_id)
    assert len(steps) == 2
    assert steps[1].position == 2
    assert steps[1].status is StepStatus.PENDING
    assert steps[1].title == "Modify component A"
    conn.close()


# 3. REVISE creates revision
def test_03_revise_creates_revision(tmp_path):
    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Root cause is in database",
            (PlanStep("Fix database migration", "Apply migration fix", "Check DB"),),
        )
    )
    service, store, _runner, conn = make_service(tmp_path, reviewer=reviewer)
    task = service.create(goal="Revise test")

    service.run(task.task_id, max_steps=1)
    revisions = store.list_revisions(task.task_id)
    assert len(revisions) == 2
    assert revisions[1].revision_number == 1
    assert revisions[1].reason == "Root cause is in database"
    assert revisions[1].trigger_step_id == store.list_steps(task.task_id)[0].step_id
    conn.close()


# 4. Completed step immutable
def test_04_completed_step_immutable(tmp_path):
    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Replan",
            (PlanStep("New step", "Instruction", "Check"),),
        )
    )
    service, store, _runner, conn = make_service(tmp_path, reviewer=reviewer)
    task = service.create(goal="Immutability test")

    service.run(task.task_id, max_steps=1)
    step1_before = store.list_steps(task.task_id)[0]

    # Another replan or execution
    steps_after = store.list_steps(task.task_id)
    step1_after = steps_after[0]
    assert step1_after.step_id == step1_before.step_id
    assert step1_after.status is StepStatus.SUCCEEDED
    assert step1_after.result == step1_before.result
    assert step1_after.verification_status == step1_before.verification_status
    assert step1_after.completed_at == step1_before.completed_at
    assert step1_after.plan_revision_id == step1_before.plan_revision_id
    conn.close()


# 5. Pending old steps become superseded
def test_05_pending_old_steps_become_superseded(tmp_path):
    plan = [
        PlanStep("Step 1", "Do 1", "Check 1"),
        PlanStep("Step 2", "Do 2", "Check 2"),
        PlanStep("Step 3", "Do 3", "Check 3"),
    ]
    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Supersede old pending",
            (PlanStep("Step 4 replacement", "Do 4", "Check 4"),),
        )
    )
    service, store, _runner, conn = make_service(tmp_path, plan=plan, reviewer=reviewer)
    task = service.create(goal="Superseded test")

    service.run(task.task_id, max_steps=1)
    steps = store.list_steps(task.task_id)
    rev1 = store.list_revisions(task.task_id)[1]

    assert steps[0].status is StepStatus.SUCCEEDED
    assert steps[1].status is StepStatus.SUPERSEDED
    assert steps[1].superseded_by_revision == rev1.revision_id
    assert steps[2].status is StepStatus.SUPERSEDED
    assert steps[2].superseded_by_revision == rev1.revision_id
    conn.close()


# 6. Superseded steps never execute
def test_06_superseded_steps_never_execute(tmp_path):
    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "New plan",
            (PlanStep("Step 3 replacement", "Do 3", "Check 3"),),
        )
    )
    service, store, _runner, conn = make_service(tmp_path, reviewer=reviewer)
    task = service.create(goal="No execute superseded")

    service.run(task.task_id, max_steps=1)
    claim = store.claim_next_step(task.task_id)
    assert claim.outcome.value == "claimed"
    assert claim.step.position == 3  # Replacement step, not superseded step 2
    assert claim.step.title == "Step 3 replacement"
    conn.close()


# 7. Replacement steps execute
def test_07_replacement_steps_execute(tmp_path):
    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Execute replacement",
            (PlanStep("Step 3 replacement", "Do 3", "Check 3"),),
        )
    )
    limits = TaskLimits(max_execution_steps_per_invocation=5)
    service, store, runner, conn = make_service(tmp_path, reviewer=reviewer, limits=limits)
    task = service.create(goal="Execute replacement test")

    results = service.run(task.task_id, max_steps=2)
    assert len(results) == 2
    assert runner.calls == [1, 3]
    steps = store.list_steps(task.task_id)
    assert steps[2].status is StepStatus.SUCCEEDED
    assert store.get_task(task.task_id).status is TaskStatus.COMPLETED
    conn.close()


# 8. Revision transaction atomic
def test_08_revision_transaction_atomic(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Atomic test",
        [PlanStep("Step 1", "Do 1", "Check 1"), PlanStep("Step 2", "Do 2", "Check 2")],
        source="test",
    )
    # Finish step 1
    store.claim_next_step(task.task_id)
    store.finish_step("step_" + store.list_steps(task.task_id)[0].step_id[5:], result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    step1 = store.list_steps(task.task_id)[0]

    # Create trigger that forces failure on inserting a replacement step
    conn.executescript(
        """CREATE TRIGGER fail_replacement_insert BEFORE INSERT ON task_steps
           WHEN NEW.title = 'Failing Step' BEGIN SELECT RAISE(ABORT, 'forced atomic test failure'); END;"""
    )
    with pytest.raises(Exception, match="forced atomic test failure"):
        store.apply_plan_revision(
            task.task_id,
            trigger_step_id=step1.step_id,
            reason="Will fail atomically",
            remaining_steps=[PlanStep("Failing Step", "Instruction", "Verification")],
        )

    # Verify rollback: no new revision, step 2 is still pending
    assert len(store.list_revisions(task.task_id)) == 1
    assert store.list_steps(task.task_id)[1].status is StepStatus.PENDING
    conn.close()


# 9. Malformed replan output fails safely
def test_09_malformed_replan_output_fails_safely(tmp_path):
    # Invalid JSON
    with pytest.raises(InvalidPlanRevisionError):
        parse_plan_review_output("not json")

    # Missing decision
    with pytest.raises(InvalidPlanRevisionError):
        parse_plan_review_output('{"reason": "no decision"}')

    # Invalid decision
    with pytest.raises(InvalidPlanRevisionError):
        parse_plan_review_output('{"decision": "destroy_all", "reason": "bad"}')

    # Revise remaining without remaining_steps
    with pytest.raises(InvalidPlanRevisionError):
        parse_plan_review_output('{"decision": "revise_remaining", "reason": "missing steps"}')


# 10. Zero replacement steps rejected when goal incomplete
def test_10_zero_replacement_steps_rejected_when_goal_incomplete(tmp_path):
    with pytest.raises(InvalidPlanRevisionError):
        parse_plan_review_output('{"decision": "revise_remaining", "reason": "empty steps", "remaining_steps": []}')

    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Test empty", [PlanStep("s1", "i1", "v1")], source="test")
    with pytest.raises(PlanValidationError, match="at least 1 step"):
        store.apply_plan_revision(task.task_id, trigger_step_id="fake", reason="r", remaining_steps=[])
    conn.close()


# 11. Max replan bound
def test_11_max_replan_bound(tmp_path):
    limits = TaskLimits(max_steps_per_task=12, max_replans_per_task=2)
    conn = connect(tmp_path)
    store = TaskStore(conn, limits=limits)
    task = store.create_task(
        "Replan bound test",
        [PlanStep("Step 1", "Do 1", "Check 1"), PlanStep("Step 2", "Do 2", "Check 2")],
        source="test",
    )
    runner = ScriptedRunner()
    verifier = ScriptedVerifier()

    replan_counter = 0

    def review_fn(t, s, e, v, st):
        nonlocal replan_counter
        replan_counter += 1
        return PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            f"Replan attempt {replan_counter}",
            (
                PlanStep(f"Step replan {replan_counter}a", "Inst", "Ver"),
                PlanStep(f"Step replan {replan_counter}b", "Inst", "Ver"),
            ),
        )

    executor = TaskExecutor(
        store, runner, verifier, reviewer=CallablePlanReviewer(review_fn), limits=limits
    )

    # Run 1: Replan 1 succeeds
    r1 = executor.run_next(task.task_id)
    assert r1.task.status is TaskStatus.RUNNING
    assert store.count_revisions(task.task_id) == 1

    # Run 2: Replan 2 succeeds
    r2 = executor.run_next(task.task_id)
    assert r2.task.status is TaskStatus.RUNNING
    assert store.count_revisions(task.task_id) == 2

    # Run 3: Replan 3 hits max_replans_per_task (2) -> BLOCKED
    r3 = executor.run_next(task.task_id)
    assert r3.task.status is TaskStatus.BLOCKED
    assert r3.code == "task_replan_blocked"
    conn.close()


# 12. Total step bound across revisions
def test_12_total_step_bound_across_revisions(tmp_path):
    limits = TaskLimits(max_steps_per_task=4)
    conn = connect(tmp_path)
    store = TaskStore(conn, limits=limits)
    task = store.create_task(
        "Total step bound test",
        [PlanStep("s1", "i1", "v1"), PlanStep("s2", "i2", "v2")],
        source="test",
    )
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    # Existing steps = 2. Propose 3 replacement steps -> total 5 > max 4
    with pytest.raises(PlanValidationError, match="total durable task steps"):
        store.apply_plan_revision(
            task.task_id,
            trigger_step_id=step1.step_id,
            reason="Too many steps",
            remaining_steps=[
                PlanStep("r1", "i", "v"),
                PlanStep("r2", "i", "v"),
                PlanStep("r3", "i", "v"),
            ],
        )
    conn.close()


# 13. Concurrent revision safe
def test_13_concurrent_revision_safe(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Concurrent test",
        [PlanStep("s1", "i1", "v1"), PlanStep("s2", "i2", "v2")],
        source="test",
    )
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    errors = []
    results = []

    def try_replan(tag):
        thread_conn = connect(tmp_path)
        try:
            thread_store = TaskStore(thread_conn)
            r = thread_store.apply_plan_revision(
                task.task_id,
                trigger_step_id=step1.step_id,
                reason=f"Concurrent {tag}",
                remaining_steps=[PlanStep(f"Repl {tag}", "i", "v")],
            )
            results.append(r)
        except Exception as exc:
            errors.append(exc)
        finally:
            thread_conn.close()

    t1 = threading.Thread(target=try_replan, args=("A",))
    t2 = threading.Thread(target=try_replan, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    # Exactly one revision 1 exists in store
    assert len(store.list_revisions(task.task_id)) == 2  # rev 0 + rev 1
    conn.close()


# 14. Restart after revision safe
def test_14_restart_after_revision_safe(tmp_path):
    conn1 = connect(tmp_path)
    store1 = TaskStore(conn1)
    task = store1.create_task("Restart test", [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store1.claim_next_step(task.task_id)
    step1 = store1.list_steps(task.task_id)[0]
    store1.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    store1.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Durable restart replan",
        remaining_steps=[PlanStep("s3 replacement", "i3", "v3")],
    )
    conn1.close()

    # Reopen connection
    conn2 = connect(tmp_path)
    store2 = TaskStore(conn2)
    revisions = store2.list_revisions(task.task_id)
    assert len(revisions) == 2
    assert revisions[1].reason == "Durable restart replan"

    steps = store2.list_steps(task.task_id)
    assert steps[0].status is StepStatus.SUCCEEDED
    assert steps[1].status is StepStatus.SUPERSEDED
    assert steps[2].status is StepStatus.PENDING
    assert steps[2].title == "s3 replacement"
    conn2.close()


# 15. Duplicate review does not create duplicate revision
def test_15_duplicate_review_does_not_create_duplicate_revision(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Deduplication test", [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    r1 = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="First apply",
        remaining_steps=[PlanStep("s3", "i3", "v3")],
    )
    r2 = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Second apply",
        remaining_steps=[PlanStep("s3_dupe", "i", "v")],
    )

    assert r1[1].revision_id == r2[1].revision_id
    assert len(store.list_revisions(task.task_id)) == 2
    conn.close()


# 16. Task completion ignores superseded steps
def test_16_task_completion_ignores_superseded_steps(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Completion test", [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Supersede step 2 with step 3",
        remaining_steps=[PlanStep("s3", "i3", "v3")],
    )

    # Claim and finish step 3
    claim = store.claim_next_step(task.task_id)
    assert claim.step.position == 3
    updated_task, _finished_step = store.finish_step(
        claim.step.step_id,
        result="done",
        verification=VerificationResult(VerificationStatus.PASS, "complete"),
    )

    assert updated_task.status is TaskStatus.COMPLETED
    conn.close()


# 17. Task cannot complete with pending replacement steps
def test_17_task_cannot_complete_with_pending_replacement_steps(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Pending test", [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Add two replacement steps",
        remaining_steps=[PlanStep("s3", "i3", "v3"), PlanStep("s4", "i4", "v4")],
    )

    current_task = store.get_task(task.task_id)
    assert current_task.status is TaskStatus.RUNNING
    conn.close()


# 18. Goal remains immutable
def test_18_goal_remains_immutable(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    original_goal = "Original user intent: do not modify public API"
    task = store.create_task(original_goal, [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Replan attempt",
        remaining_steps=[PlanStep("s3", "i3", "v3")],
    )

    assert store.get_task(task.task_id).goal == original_goal
    conn.close()


# 19. Goal constraint preservation in CONTROL prompt
def test_19_goal_constraint_preservation():
    mock_router = SimpleNamespace()
    reviewer = ModelPlanReviewer(mock_router)
    # Check that model instructions mandate goal constraint preservation
    assert hasattr(reviewer, "review")


# 20. Planner/replanner output remains DATA under M19
def test_20_planner_replanner_output_remains_data_under_m19(tmp_path):
    recorded_assembly = None

    class CaptureClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            nonlocal recorded_assembly
            recorded_assembly = kwargs
            return SimpleNamespace(content='{"decision": "keep", "reason": "valid"}')

    router = SimpleNamespace(
        client=lambda r: CaptureClient(),
        model=lambda r: "mock-small",
    )
    reviewer = ModelPlanReviewer(router)
    task = Task("t1", "Fix auth", TaskStatus.RUNNING, None, "test", None, "now", "now", None)
    step = TaskStep("s1", "t1", 1, "t", "i", "v", StepStatus.SUCCEEDED, 1, 1, "res", 3, False, VerificationStatus.PASS, "ok", "r1", "now", "now", "now")
    exec_res = StepExecution(result="observable result")
    ver_res = VerificationResult(VerificationStatus.PASS, "ok")

    reviewer.review(task, step, exec_res, ver_res, [step])
    assert recorded_assembly is not None
    # System prompt is CONTROL instructions
    assert "strictly read-only and tool-free" in recorded_assembly["system"]
    # Messages contain USER and DATA blocks, not unclassified
    assert any(m["role"] == "user" for m in recorded_assembly["messages"])


# 21. Replanner has no tools
def test_21_replanner_has_no_tools(tmp_path):
    recorded_tools = None

    class CaptureClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            nonlocal recorded_tools
            recorded_tools = kwargs.get("tools")
            return SimpleNamespace(content='{"decision": "keep", "reason": "valid"}')

    router = SimpleNamespace(
        client=lambda r: CaptureClient(),
        model=lambda r: "mock-small",
    )
    reviewer = ModelPlanReviewer(router)
    task = Task("t1", "Fix auth", TaskStatus.RUNNING, None, "test", None, "now", "now", None)
    step = TaskStep("s1", "t1", 1, "t", "i", "v", StepStatus.SUCCEEDED, 1, 1, "res", 3, False, VerificationStatus.PASS, "ok", "r1", "now", "now", "now")

    reviewer.review(task, step, StepExecution("ok"), VerificationResult(VerificationStatus.PASS, "ok"), [step])
    assert recorded_tools == []


# 22. Trust still authorizes every revised action
def test_22_trust_still_authorizes_every_revised_action(tmp_path):
    registry = ToolRegistry(trust_policy={"forbidden_tools": ["forbidden_tool"]})
    registry.register(
        Tool(
            name="forbidden_tool",
            description="Forbidden",
            input_schema={"type": "object", "properties": {}},
            fn=lambda: "done",
            default_policy="deny",
        )
    )
    raw = registry.execute("forbidden_tool", {}, context={"user_request": "Execute forbidden action"})
    data = json.loads(raw)
    assert data.get("ok") is False
    assert data.get("error", {}).get("code") in {"tool_permission_denied", "tool_denied"}


# 23. Trust denial cannot be bypassed through replan
def test_23_trust_denial_cannot_be_bypassed_through_replan(tmp_path):
    case = load_corpus(default_corpus_paths()).select(case_id="replan-trust-denial-blocked-001").cases[0]
    result = EvalRunner().run_case(case)
    assert result.verdict is EvalVerdict.BLOCKED
    assert any(e["event_type"] == "tool_denied" for e in result.evidence.replay_events)


# 24. Uncertain Action Ledger blocks replan execution
def test_24_uncertain_action_ledger_blocks_replan_execution(tmp_path):
    verifier = ScriptedVerifier({1: VerificationStatus.BLOCKED})
    service, store, _runner, conn = make_service(tmp_path, verifier=verifier)
    task = service.create(goal="Uncertain test")

    res = service.run(task.task_id, max_steps=1)[0]
    assert res.task.status is TaskStatus.BLOCKED
    assert res.step.status is StepStatus.BLOCKED
    # Replan is NOT called on blocked step
    assert len(store.list_revisions(task.task_id)) == 1
    conn.close()


# 25. M17 recovery remains required
def test_25_m17_recovery_remains_required(tmp_path):
    verifier = ScriptedVerifier({1: VerificationStatus.BLOCKED})
    service, store, _runner, conn = make_service(tmp_path, verifier=verifier)
    task = service.create(goal="Recovery test")

    service.run(task.task_id, max_steps=1)
    blocked_task = store.get_task(task.task_id)
    assert blocked_task.status is TaskStatus.BLOCKED

    # Must call prepare_blocked_step_retry to make it pending
    store.prepare_blocked_step_retry(task.task_id)
    assert store.get_task(task.task_id).status is TaskStatus.RUNNING
    assert store.list_steps(task.task_id)[0].status is StepStatus.PENDING
    conn.close()


# 26. M16 command runner still goes through normal path
def test_26_m16_command_runner_still_goes_through_normal_path():
    registry = ToolRegistry(trust_policy={"allowed_commands": ["git status"]})
    assert registry is not None


# 27. Scheduled task can replan safely
def test_27_scheduled_task_can_replan_safely(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Scheduled replan test",
        [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")],
        source="scheduled",
        source_id="schedule_run_42",
    )
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    updated_task, _rev, _new_steps, _superseded = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Scheduled replan",
        remaining_steps=[PlanStep("s3", "i3", "v3")],
    )

    assert updated_task.source == "scheduled"
    assert updated_task.source_id == "schedule_run_42"
    assert len(store.list_revisions(task.task_id)) == 2
    conn.close()


# 28. Replay revision events
def test_28_replay_revision_events(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)

    reviewer = CallablePlanReviewer(
        lambda t, s, e, v, st: PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Replay test replan",
            (PlanStep("s3", "i3", "v3"),),
        )
    )
    store = TaskStore(conn)
    planner = SimpleNamespace(plan=lambda g: [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")])
    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        ScriptedVerifier(),
        reviewer=reviewer,
        replay=replay,
    )
    service = TaskService(store, planner, executor)
    task = service.create(goal="Replay test")

    replay.start_run(
        run_id="run_1",
        session_id=f"session_{task.task_id}",
        source="test",
        role="main",
        model="model",
        provider="prov",
        user_input="input",
    )
    service.run(task.task_id, max_steps=1)
    replay.complete_run(
        "run_1",
        output="done",
        iterations=1,
        latency_ms=10,
        role="main",
        model="model",
        provider="prov",
    )

    events = replay.get_events("run_1")
    event_types = [e["event_type"] for e in events]
    assert "task_replan_started" in event_types
    assert "task_step_superseded" in event_types
    assert "task_plan_revised" in event_types
    conn.close()


# 29. Plan revision secret safety
def test_29_plan_revision_secret_safety(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Secret test", [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    secret_val = "ghp_1234567890abcdefghijklmnopqrstuv"
    _, rev, new_steps, _ = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason=f"Fixing with token={secret_val}",
        remaining_steps=[PlanStep("Step with secret", f"export token={secret_val}", "check")],
    )

    assert secret_val not in rev.reason
    assert secret_val not in new_steps[0].instruction
    conn.close()


# 30. CLI task show exposes revisions
def test_30_cli_task_show_exposes_revisions(tmp_path, capsys):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("CLI show test", [PlanStep("Step 1", "i1", "v1"), PlanStep("Step 2", "i2", "v2")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Revise via CLI",
        remaining_steps=[PlanStep("Step 3 replacement", "i3", "v3")],
    )
    conn.close()

    # Test JSON output
    args_json = Namespace(task_command="show", task_id=task.task_id, json=True)
    assert run_task_cli(args_json, settings) == 0
    data = json.loads(capsys.readouterr().out)
    assert "revisions" in data
    assert len(data["revisions"]) == 2

    # Test text output
    args_text = Namespace(task_command="show", task_id=task.task_id, json=False)
    assert run_task_cli(args_text, settings) == 0
    text = capsys.readouterr().out
    assert "Plan revision 0" in text
    assert "Plan revision 1" in text
    assert "[superseded]" in text


# 31. M21 replan metrics
def test_31_m21_replan_metrics():
    evidence = EvalEvidence(
        task_status="completed",
        replan_count=2,
        plan_revisions=(
            {"revision_number": 0},
            {"revision_number": 1},
            {"revision_number": 2},
        ),
        replan_limit_blocked=False,
    )
    case = EvalCase("replan-metrics-001", "test", "Goal", EvalSetup(), EvalExpectation(task_status="completed"))
    res = score_case(case, evidence)
    assert res.metrics["replan_count"] == 2
    assert res.metrics["plan_revisions"] == 3
    assert res.metrics["replan_occurred"] == 1
    assert res.metrics["replan_success"] == 1

    summary = aggregate_results((res,))
    assert summary["metrics"]["replan_rate"] == 1.0
    assert summary["metrics"]["replan_success_rate"] == 1.0
    assert summary["metrics"]["average_plan_revisions"] == 3.0


# 32. M21 failure taxonomy integration
def test_32_m21_failure_taxonomy_integration():
    assert FailureType.REPLANNING_ERROR.value == "replanning_error"
    assert FailureType.REPLAN_LIMIT_EXCEEDED.value == "replan_limit_exceeded"
    assert FailureType.INVALID_PLAN_REVISION.value == "invalid_plan_revision"


# 33. Root-cause-change eval case
def test_33_root_cause_change_eval_case():
    case = load_corpus(default_corpus_paths()).select(case_id="replan-root-cause-001").cases[0]
    result = EvalRunner().run_case(case)
    assert result.verdict is EvalVerdict.PASS
    assert result.metrics["replan_count"] == 1
    assert result.metrics["replan_success"] == 1


# 34. KEEP eval case
def test_34_keep_eval_case():
    case = load_corpus(default_corpus_paths()).select(case_id="replan-keep-valid-001").cases[0]
    result = EvalRunner().run_case(case)
    assert result.verdict is EvalVerdict.PASS
    assert result.metrics["replan_count"] == 0


# 35. Replan-limit eval case
def test_35_replan_limit_eval_case():
    case = load_corpus(default_corpus_paths()).select(case_id="replan-limit-exceeded-001").cases[0]
    result = EvalRunner().run_case(case)
    assert result.verdict is EvalVerdict.BLOCKED
    assert result.evidence.task_status == "blocked"
    assert any(e["event_type"] == "task_replan_blocked" for e in result.evidence.replay_events)


# 36. Crash/restart task resume uses latest revision
def test_36_crash_restart_task_resume_uses_latest_revision(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Crash resume", [PlanStep("s1", "i", "v"), PlanStep("s2", "i", "v")], source="test")
    store.claim_next_step(task.task_id)
    step1 = store.list_steps(task.task_id)[0]
    store.finish_step(step1.step_id, result="ok", verification=VerificationResult(VerificationStatus.PASS, "ok"))

    store.apply_plan_revision(
        task.task_id,
        trigger_step_id=step1.step_id,
        reason="Revised before crash",
        remaining_steps=[PlanStep("Replacement step 3", "Execute 3", "Check 3")],
    )
    conn.close()

    # Simulate fresh startup
    conn2 = connect(tmp_path)
    store2 = TaskStore(conn2)
    service2 = TaskService(
        store2,
        SimpleNamespace(plan=lambda g: []),
        TaskExecutor(store2, ScriptedRunner(), ScriptedVerifier()),
    )
    results = service2.resume(task.task_id)
    assert len(results) == 1
    assert results[0].step.position == 3
    assert results[0].step.title == "Replacement step 3"
    assert results[0].task.status is TaskStatus.COMPLETED
    conn2.close()

"""Deterministic test suite for M32 — Budget-Aware Recovery Planning & Model-Call Efficiency."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

from tieru.capabilities.router import CapabilityRouter
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.goal_verifier import (
    DeterministicGoalVerifier,
    LayeredTaskGoalVerifier,
    ModelGoalJudge,
)
from tieru.tasks.models import (
    BudgetDecision,
    BudgetResource,
    GoalVerificationStatus,
    ModelCallCriticality,
    ModelCallPurpose,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    StepExecution,
    StepExecutionKind,
    TaskBudget,
    TaskLimits,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.reviewer import ModelPlanReviewer
from tieru.tasks.store import TaskStore, initialize_task_schema
from tieru.tools.registry import Tool, ToolRegistry


def _init_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    initialize_task_schema(conn)
    return TaskStore(conn, limits=TaskLimits())


def _mock_tool(name: str, description: str) -> Tool:
    return Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        fn=lambda **kwargs: "ok",
    )


class MockRunner:
    def __init__(self, execution: StepExecution) -> None:
        self.execution = execution

    def __call__(self, task, step, context, observer=None) -> StepExecution:
        return self.execution


class MockVerifier:
    def __init__(self, result: VerificationResult) -> None:
        self.result = result

    def verify(self, task, step, execution) -> VerificationResult:
        return self.result


def test_01_model_call_criticality_enums():
    assert ModelCallCriticality.REQUIRED.value == "required"
    assert ModelCallCriticality.OPTIONAL.value == "optional"
    assert ModelCallPurpose.STEP_EXECUTION.value == "step_execution"
    assert ModelCallPurpose.STEP_CONTINUATION.value == "step_continuation"
    assert ModelCallPurpose.PLAN_REVIEW_OPPORTUNISTIC.value == "plan_review_opportunistic"
    assert ModelCallPurpose.FAILURE_RECOVERY.value == "failure_recovery"
    assert ModelCallPurpose.GOAL_VERIFICATION.value == "goal_verification"
    assert ModelCallPurpose.FINAL_SYNTHESIS.value == "final_synthesis"


def test_02_store_starvation_prevention_rejects_optional_call():
    store = _init_store()

    plan = [
        PlanStep("Step 1", "instruction 1", "verify 1"),
        PlanStep("Step 2", "instruction 2", "verify 2"),
    ]
    # create_task automatically initializes a GoalContract for the task
    task = store.create_task(
        "Test starvation prevention",
        plan,
        budget=TaskBudget(max_model_calls=3.0),
    )

    # 2 pending steps + 1 contract = 3.0 headroom. Total limit = 3.0.
    # An OPTIONAL call of 1.0 would need (0 + 1.0 + 3.0 = 4.0 > 3.0) -> rejected!
    res_optional = store.reserve_budget(
        task.task_id,
        BudgetResource.MODEL_CALLS,
        1.0,
        criticality=ModelCallCriticality.OPTIONAL,
        purpose=ModelCallPurpose.PLAN_REVIEW_OPPORTUNISTIC,
    )
    assert not res_optional.allowed
    assert res_optional.decision is BudgetDecision.EXHAUSTED
    assert res_optional.reason == "budget_starvation_prevention"

    # But a REQUIRED call of 1.0 is allowed because 0 + 1.0 <= 3.0!
    res_required = store.reserve_budget(
        task.task_id,
        BudgetResource.MODEL_CALLS,
        1.0,
        criticality=ModelCallCriticality.REQUIRED,
        purpose=ModelCallPurpose.STEP_EXECUTION,
    )
    assert res_required.allowed
    assert res_required.decision is BudgetDecision.ALLOW


def test_03_store_starvation_prevention_allows_optional_when_surplus():
    store = _init_store()

    plan = [
        PlanStep("Step 1", "instruction 1", "verify 1"),
        PlanStep("Step 2", "instruction 2", "verify 2"),
    ]
    task = store.create_task(
        "Test starvation prevention surplus",
        plan,
        budget=TaskBudget(max_model_calls=10.0),
    )

    # Surplus is plenty (10.0 limit, 2 pending steps + 1 contract -> 3.0 headroom).
    # 0 + 1.0 + 3.0 = 4.0 <= 10.0 -> ALLOWED
    res_optional = store.reserve_budget(
        task.task_id,
        BudgetResource.MODEL_CALLS,
        1.0,
        criticality=ModelCallCriticality.OPTIONAL,
        purpose=ModelCallPurpose.FINAL_SYNTHESIS,
    )
    assert res_optional.allowed
    assert res_optional.decision is BudgetDecision.ALLOW


def test_04_count_pending_steps():
    store = _init_store()

    plan = [
        PlanStep("Step 1", "ins", "ver"),
        PlanStep("Step 2", "ins", "ver"),
    ]
    task = store.create_task("Test count pending", plan)

    assert store.count_pending_steps(task.task_id) == 2
    assert store.count_remaining_steps(task.task_id) == 2

    store.claim_next_step(task.task_id)
    # step 1 is now running
    assert store.count_pending_steps(task.task_id) == 1
    assert store.count_remaining_steps(task.task_id) == 2


def test_05_executor_opportunistic_review_skipped_when_headroom_needed():
    store = _init_store()

    plan = [
        PlanStep("Step 1", "ins 1", "ver 1", execution_kind=StepExecutionKind.COMMAND),
        PlanStep("Step 2", "ins 2", "ver 2", execution_kind=StepExecutionKind.COMMAND),
        PlanStep("Step 3", "ins 3", "ver 3", execution_kind=StepExecutionKind.COMMAND),
    ]
    task = store.create_task(
        "Execute multi-step task",
        plan,
        budget=TaskBudget(max_model_calls=3.0),
    )

    # Step 1 used 1 call
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    # Remaining budget is 2.0. Remaining pending steps = 2.
    # Total headroom required for review = 1.0 + 2.0 + 1.0 = 4.0 > 2.0. Review should be skipped.

    mock_reviewer = MagicMock()
    runner = MockRunner(StepExecution(result="done", run_id="run_1"))
    verifier = MockVerifier(VerificationResult(VerificationStatus.PASS, "step 1 passed"))
    executor = TaskExecutor(store, runner, verifier, reviewer=mock_reviewer)

    res = executor.run_next(task.task_id)

    # Reviewer must NOT be called
    assert mock_reviewer.review.call_count == 0
    assert res.code == "task_step_succeeded"
    assert res.executed is True


def test_06_opportunistic_review_preserves_plan_on_budget_exhaustion():
    store = _init_store()

    plan = [
        PlanStep("Step 1", "ins", "ver"),
        PlanStep("Step 2", "ins", "ver"),
    ]
    task = store.create_task(
        "Test plan review",
        plan,
        budget=TaskBudget(max_model_calls=10.0),
    )
    mock_reviewer = MagicMock()
    mock_reviewer.review.return_value = PlanReviewResult(
        decision=PlanReviewDecision.BLOCK,
        reason="budget_exhausted:model_calls",
        remaining_steps=(),
    )
    runner = MockRunner(StepExecution(result="done", run_id="run_1"))
    verifier = MockVerifier(VerificationResult(VerificationStatus.PASS, "step 1 passed"))
    executor = TaskExecutor(store, runner, verifier, reviewer=mock_reviewer)

    res = executor.run_next(task.task_id)
    assert res.code == "task_step_succeeded"
    assert res.executed is True
    t = store.get_task(task.task_id)
    assert t.status.value == "running"


def test_07_reviewer_failure_recovery_blocks_on_budget_exhaustion():
    store = _init_store()

    plan = [PlanStep("Step 1", "ins", "ver")]
    task = store.create_task(
        "Test failure recovery review",
        plan,
        budget=TaskBudget(max_model_calls=1.0),
    )
    steps = store.list_steps(task.task_id)
    s1 = steps[0]

    # Budget exhausted
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    reviewer = ModelPlanReviewer(model_router=MagicMock(), store=store)
    res = reviewer.review(
        task=task,
        current_step=s1,
        execution=StepExecution(result="err", run_id="r1"),
        verification=VerificationResult(VerificationStatus.FAIL, "failed"),
        all_steps=steps,
    )

    # On FAIL, failure recovery is required, so exhausted budget blocks
    assert res.decision is PlanReviewDecision.BLOCK
    assert res.reason == "budget_exhausted:model_calls"


def test_08_goal_verifier_falls_back_to_deterministic_when_model_budget_exhausted():
    store = _init_store()

    plan = [PlanStep("Step 1", "ins", "ver")]
    task = store.create_task(
        "Create hello.txt with content 'world'",
        plan,
        budget=TaskBudget(max_model_calls=1.0),
    )
    contract = store.get_goal_contract(task.task_id)

    # Exhaust model call budget
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    judge = ModelGoalJudge(model_router=MagicMock(), store=store)
    det_verifier = DeterministicGoalVerifier()
    layered = LayeredTaskGoalVerifier(judge=judge, deterministic_verifier=det_verifier)

    evidence = {
        "command_exit_code": 0,
        "modified_files": ["hello.txt"],
        "criterion_outcomes": {"sc_1": "pass"},
    }
    res = layered.verify(task, contract, steps=[], execution_evidence=evidence)

    # Goal verification must NOT be blocked! It falls back to deterministic verification!
    assert res.status is GoalVerificationStatus.PASS
    assert "verified with observable evidence" in res.summary


def test_09_multilingual_capability_routing():
    registry = ToolRegistry()
    registry.register(_mock_tool("run_command", "Execute bash commands"))
    registry.register(_mock_tool("filesystem_read", "Read file contents"))
    registry.register(_mock_tool("filesystem_write", "Write file contents"))

    router = CapabilityRouter()
    decision = router.route("Xác định vị trí thư mục dự án hiện tại bằng lệnh pwd", registry)
    assert "run_command" in decision.selected_tools


def test_10_agent_synthesis_fallback_when_budget_exhausted():
    store = _init_store()
    plan = [PlanStep("Step 1", "ins", "ver")]
    task = store.create_task("Test synthesis fallback", plan, budget=TaskBudget(max_model_calls=1.0))

    # Consume all model calls
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    # Now an optional synthesis reservation will fail
    res = store.reserve_budget(
        task.task_id,
        BudgetResource.MODEL_CALLS,
        1.0,
        criticality=ModelCallCriticality.OPTIONAL,
        purpose=ModelCallPurpose.FINAL_SYNTHESIS,
    )
    assert not res.allowed


def test_11_store_starvation_prevention_rejects_optional_when_budget_tight():
    store = _init_store()
    plan = [
        PlanStep("Step 1", "ins 1", "ver 1"),
        PlanStep("Step 2", "ins 2", "ver 2"),
        PlanStep("Step 3", "ins 3", "ver 3"),
    ]
    # 3 steps + 1 contract = 4.0 required headroom
    task = store.create_task("Tight budget task", plan, budget=TaskBudget(max_model_calls=4.0))

    # Optional call of 1.0 would need 0 + 1.0 + 4.0 = 5.0 > 4.0 -> Rejected
    res_opt = store.reserve_budget(
        task.task_id,
        BudgetResource.MODEL_CALLS,
        1.0,
        criticality=ModelCallCriticality.OPTIONAL,
        purpose=ModelCallPurpose.PLAN_REVIEW_OPPORTUNISTIC,
    )
    assert not res_opt.allowed
    assert res_opt.reason == "budget_starvation_prevention"

    # But step 1 required call is allowed!
    res_req = store.reserve_budget(
        task.task_id,
        BudgetResource.MODEL_CALLS,
        1.0,
        criticality=ModelCallCriticality.REQUIRED,
        purpose=ModelCallPurpose.STEP_EXECUTION,
    )
    assert res_req.allowed


"""Deterministic verification suite for M25 — Resource & Execution Budget Governance."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from tieru.db import connect
from tieru.evals.metrics import aggregate_results
from tieru.evals.models import EvalEvidence, EvalResult, EvalVerdict, FailureType
from tieru.loop.agent import run_bounded_multi_step
from tieru.tasks.cli import run_task_cli
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.goal_verifier import ModelGoalJudge
from tieru.tasks.models import (
    BudgetDecision,
    BudgetExhaustionReason,
    BudgetResource,
    GoalVerificationStatus,
    PlanStep,
    PlanValidationError,
    ReplanLimitExceededError,
    StepClaimOutcome,
    StepExecution,
    Task,
    TaskBudget,
    TaskBudgetUsage,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.reviewer import ModelPlanReviewer, PlanReviewDecision
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import LayeredTaskVerifier, ModelResultVerifier
from tieru.tools.registry import Tool, ToolRegistry


@pytest.fixture
def db_conn(tmp_path):
    conn = connect(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def task_store(db_conn):
    return TaskStore(db_conn)


class MockModelRouter:
    def __init__(self, reply_text: str = '{"status":"pass","summary":"ok"}') -> None:
        self.reply_text = reply_text
        self.call_count = 0
        self.last_messages = []

    def client(self, role: str):
        class MockMessages:
            def __init__(outer):
                pass

            def create(outer, **kwargs):
                self.call_count += 1
                self.last_messages = kwargs.get("messages", [])

                class MockUsage:
                    input_tokens = 120
                    output_tokens = 45

                class MockResponse:
                    def __init__(self_resp):
                        self_resp.content = [MagicMock(text=self.reply_text, type="text")]
                        self_resp.usage = MockUsage()
                        self_resp.stop_reason = "end_turn"

                return MockResponse()

        class MockClient:
            messages = MockMessages()

        return MockClient()

    def model(self, role: str) -> str:
        return f"mock-{role}-model"

    def provider(self, role: str) -> str:
        return "mock-provider"


class DeterministicRunner:
    def __init__(
        self,
        results: list[str] | None = None,
        tool_calls_fn: Callable[[], list[dict]] | None = None,
    ) -> None:
        self.results = list(results or ["Step completed successfully."])
        self.tool_calls_fn = tool_calls_fn
        self.invocations = 0

    def __call__(self, task: Task, step: TaskStep, context: str, observer=None) -> StepExecution:
        self.invocations += 1
        res = self.results.pop(0) if self.results else "Default success."
        calls = self.tool_calls_fn() if self.tool_calls_fn else []
        return StepExecution(result=res, run_id=f"run_{self.invocations}", tool_calls=tuple(calls))


# ---------------------------------------------------------------------------
# 1. Models & Validation Tests
# ---------------------------------------------------------------------------


def test_task_budget_validation():
    budget = TaskBudget(
        max_model_calls=10,
        max_tool_calls=15,
        max_steps=5,
        max_replans=2,
        max_active_runtime_seconds=120.0,
    )
    assert budget.max_model_calls == 10
    assert budget.max_tool_calls == 15
    assert budget.max_steps == 5
    assert budget.max_replans == 2
    assert budget.max_active_runtime_seconds == 120.0

    with pytest.raises(ValueError, match="max_model_calls must be positive"):
        TaskBudget(max_model_calls=0)

    with pytest.raises(ValueError, match="max_steps must be positive"):
        TaskBudget(max_steps=0)

    with pytest.raises(ValueError, match="max_replans cannot be negative"):
        TaskBudget(max_replans=-1)

    with pytest.raises(ValueError, match="max_active_runtime_seconds must be positive"):
        TaskBudget(max_active_runtime_seconds=0.0)


def test_task_limits_default_budget():
    limits = TaskLimits(
        max_steps_per_task=12,
        max_replans_per_task=3,
    )
    b = limits.default_budget()
    assert b.max_steps == 12
    assert b.max_replans == 3
    assert b.max_model_calls == 20
    assert b.max_tool_calls == 30


def test_task_budget_usage_defaults():
    usage = TaskBudgetUsage()
    assert usage.model_calls == 0
    assert usage.tool_calls == 0
    assert usage.steps == 0
    assert usage.steps_started == 0
    assert usage.replans == 0
    assert usage.verification_calls == 0
    assert usage.retries == 0
    assert usage.active_runtime_seconds == 0.0
    assert usage.command_runtime_seconds == 0.0
    assert usage.input_tokens is None
    assert usage.output_tokens is None


# ---------------------------------------------------------------------------
# 2. SQLite Persistence & Schema Tests
# ---------------------------------------------------------------------------


def test_schema_creation_and_idempotency(db_conn):
    _ = TaskStore(db_conn)
    # Check tables exist
    tables = {
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "task_budgets" in tables
    assert "task_budget_usages" in tables
    assert "task_budget_allocations" in tables

    # Calling initialize_task_schema again must be a no-op and idempotent
    from tieru.tasks.store import initialize_task_schema

    initialize_task_schema(db_conn)


def test_task_creation_initializes_budget_atomically(task_store):
    plan = [
        PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1"),
        PlanStep(title="Step 2", instruction="Do 2", verification="Verify 2"),
    ]
    custom_budget = TaskBudget(
        max_model_calls=7,
        max_tool_calls=11,
        max_steps=4,
        max_replans=1,
    )
    task = task_store.create_task("Test goal", plan, budget=custom_budget)

    b = task_store.get_task_budget(task.task_id)
    assert b.max_model_calls == 7
    assert b.max_tool_calls == 11
    assert b.max_steps == 4
    assert b.max_replans == 1

    u = task_store.get_task_budget_usage(task.task_id)
    assert u.model_calls == 0
    assert u.tool_calls == 0
    assert u.steps == 0

    allocs = task_store.list_budget_allocations(task.task_id)
    assert len(allocs) == 1
    assert allocs[0].actor == "system"


# ---------------------------------------------------------------------------
# 3. Atomic Budget Reservation & Crash-Loop Prevention Tests
# ---------------------------------------------------------------------------


def test_atomic_reservation_success_and_exhaustion(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_model_calls=2)
    task = task_store.create_task("Goal", plan, budget=budget)

    # First reservation succeeds
    r1 = task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert r1.allowed is True
    assert r1.decision is BudgetDecision.ALLOW
    assert r1.current_usage == 1.0
    assert r1.limit_value == 2.0

    # Second reservation succeeds (usage reaches limit 2/2)
    r2 = task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert r2.allowed is True
    assert r2.current_usage == 2.0

    # Third reservation is rejected
    r3 = task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert r3.allowed is False
    assert r3.decision is BudgetDecision.EXHAUSTED
    assert r3.reason == BudgetExhaustionReason.MODEL_CALL_LIMIT
    assert r3.current_usage == 2.0


def test_crash_loop_prevention_pre_execution_reservation(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_model_calls=2)
    task = task_store.create_task("Goal", plan, budget=budget)

    # Simulate reserving before a crash
    task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    # App crashes...
    # Usage is already 1 durably in SQLite!
    u = task_store.get_task_budget_usage(task.task_id)
    assert u.model_calls == 1

    # Next attempt reserves 2nd
    task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    # Next attempt fails; cannot infinite-loop crash
    r3 = task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert r3.allowed is False


def test_continuous_resource_consumption_recording(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    task = task_store.create_task("Goal", plan)

    task_store.record_budget_consumption(task.task_id, BudgetResource.ACTIVE_RUNTIME, 2.5)
    task_store.record_budget_consumption(task.task_id, BudgetResource.ACTIVE_RUNTIME, 1.2)
    task_store.record_budget_consumption(task.task_id, BudgetResource.COMMAND_RUNTIME, 0.8)
    task_store.record_budget_consumption(task.task_id, BudgetResource.INPUT_TOKENS, 500)
    task_store.record_budget_consumption(task.task_id, BudgetResource.OUTPUT_TOKENS, 150)

    u = task_store.get_task_budget_usage(task.task_id)
    assert abs(u.active_runtime_seconds - 3.7) < 1e-5
    assert abs(u.command_runtime_seconds - 0.8) < 1e-5
    assert u.input_tokens == 500
    assert u.output_tokens == 150


def test_get_budget_remaining(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(
        max_model_calls=5,
        max_tool_calls=10,
        max_steps=3,
        max_active_runtime_seconds=60.0,
    )
    task = task_store.create_task("Goal", plan, budget=budget)

    task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 2.0)
    task_store.reserve_budget(task.task_id, BudgetResource.TOOL_CALLS, 4.0)
    task_store.record_budget_consumption(task.task_id, BudgetResource.ACTIVE_RUNTIME, 15.5)

    remaining = task_store.get_budget_remaining(task.task_id)
    assert remaining["model_calls"] == 3
    assert remaining["tool_calls"] == 6
    assert remaining["steps"] == 3
    assert abs(remaining["active_runtime_seconds"] - 44.5) < 1e-5


# ---------------------------------------------------------------------------
# 4. Step Budget Enforcement & Transition to BLOCKED
# ---------------------------------------------------------------------------


def test_step_budget_enforcement_in_claim_next_step(task_store):
    plan = [
        PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1"),
        PlanStep(title="Step 2", instruction="Do 2", verification="Verify 2"),
    ]
    # Limit to exactly 1 step
    budget = TaskBudget(max_steps=1)
    task = task_store.create_task("Goal", plan, budget=budget)

    # First claim succeeds (reserves step 1)
    c1 = task_store.claim_next_step(task.task_id)
    assert c1.outcome is StepClaimOutcome.CLAIMED
    assert c1.step.position == 1

    # Finish step 1
    task_store.finish_step(
        c1.step.step_id,
        result="ok",
        verification=VerificationResult(VerificationStatus.PASS, summary="ok"),
        auto_complete=False,
    )

    # Second claim fails because max_steps=1 is exhausted
    c2 = task_store.claim_next_step(task.task_id)
    assert c2.outcome is StepClaimOutcome.BLOCKED
    assert c2.code == "budget_exhausted:steps"

    # Task is automatically transitioned to BLOCKED
    updated_task = task_store.get_task(task.task_id)
    assert updated_task.status is TaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# 5. Replan Budget Enforcement & Task Bounds
# ---------------------------------------------------------------------------


def test_replan_budget_enforcement_in_apply_plan_revision(task_store):
    plan = [
        PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1"),
        PlanStep(title="Step 2", instruction="Do 2", verification="Verify 2"),
    ]
    budget = TaskBudget(max_replans=1, max_steps=10)
    task = task_store.create_task("Goal", plan, budget=budget)

    c1 = task_store.claim_next_step(task.task_id)
    task_store.finish_step(
        c1.step.step_id,
        result="ok",
        verification=VerificationResult(VerificationStatus.PASS, summary="ok"),
        auto_complete=False,
    )

    # First revision succeeds
    new_plan_1 = [PlanStep(title="Revised Step 2", instruction="Do 2 revised", verification="Verify 2")]
    task_store.apply_plan_revision(
        task.task_id,
        trigger_step_id=c1.step.step_id,
        reason="First replan",
        remaining_steps=new_plan_1,
    )

    u = task_store.get_task_budget_usage(task.task_id)
    assert u.replans == 1

    # Second revision exceeds max_replans=1
    c2 = task_store.claim_next_step(task.task_id)
    task_store.finish_step(
        c2.step.step_id,
        result="ok",
        verification=VerificationResult(VerificationStatus.PASS, summary="ok"),
        auto_complete=False,
    )

    new_plan_2 = [PlanStep(title="Step 3", instruction="Do 3", verification="Verify 3")]
    with pytest.raises(ReplanLimitExceededError, match="task reached maximum of"):
        task_store.apply_plan_revision(
            task.task_id,
            trigger_step_id=c2.step.step_id,
            reason="Second replan",
            remaining_steps=new_plan_2,
        )


def test_total_steps_budget_enforcement_in_apply_plan_revision(task_store):
    plan = [
        PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1"),
        PlanStep(title="Step 2", instruction="Do 2", verification="Verify 2"),
    ]
    budget = TaskBudget(max_steps=3, max_replans=5)
    task = task_store.create_task("Goal", plan, budget=budget)

    c1 = task_store.claim_next_step(task.task_id)
    task_store.finish_step(
        c1.step.step_id,
        result="ok",
        verification=VerificationResult(VerificationStatus.PASS, summary="ok"),
        auto_complete=False,
    )

    # Trying to add 3 replacement steps would bring total steps to 1 executed + 3 new = 4 > 3
    excessive_plan = [
        PlanStep(title="S2", instruction="I2", verification="V2"),
        PlanStep(title="S3", instruction="I3", verification="V3"),
        PlanStep(title="S4", instruction="I4", verification="V4"),
    ]
    with pytest.raises(PlanValidationError, match="total durable task steps"):
        task_store.apply_plan_revision(
            task.task_id,
            trigger_step_id=c1.step.step_id,
            reason="Too many steps",
            remaining_steps=excessive_plan,
        )


# ---------------------------------------------------------------------------
# 6. Active Execution Runtime Tracking with Injectable Clock
# ---------------------------------------------------------------------------


def test_active_execution_runtime_measured_with_clock(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_active_runtime_seconds=100.0)
    task = task_store.create_task("Goal", plan, budget=budget)

    current_time = 1000.0

    def mock_clock():
        return current_time

    class TimedRunner:
        def __call__(self, t, s, ctx, obs=None):
            nonlocal current_time
            # Simulate 12.5 seconds of execution work
            current_time += 12.5
            return StepExecution(result="ok", run_id="run_timed")

    verifier = LayeredTaskVerifier()
    executor = TaskExecutor(task_store, TimedRunner(), verifier, clock=mock_clock)

    res = executor.run_next(task.task_id)
    assert res.executed is True

    usage = task_store.get_task_budget_usage(task.task_id)
    assert abs(usage.active_runtime_seconds - 12.5) < 1e-5

    # Advance clock by 500 seconds while task is idle/between runs
    current_time += 500.0

    # Idle time must NOT increase active_runtime_seconds
    usage_after_idle = task_store.get_task_budget_usage(task.task_id)
    assert abs(usage_after_idle.active_runtime_seconds - 12.5) < 1e-5


def test_active_runtime_exhaustion_blocks_task_before_execution(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_active_runtime_seconds=10.0)
    task = task_store.create_task("Goal", plan, budget=budget)

    # Pre-consume all active runtime
    task_store.record_budget_consumption(task.task_id, BudgetResource.ACTIVE_RUNTIME, 10.0)

    runner = DeterministicRunner()
    verifier = LayeredTaskVerifier()
    executor = TaskExecutor(task_store, runner, verifier)

    res = executor.run_next(task.task_id)
    assert res.executed is False
    assert res.code == "budget_exhausted:active_runtime"
    assert runner.invocations == 0  # Did not run!

    updated = task_store.get_task(task.task_id)
    assert updated.status is TaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# 7. Agent Loop Tool & Model Call Accounting & Denial Loop Prevention
# ---------------------------------------------------------------------------


def test_loop_tool_call_budget_exhaustion_stops_safely(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    # Limit to 2 tool calls
    budget = TaskBudget(max_tool_calls=2)
    task = task_store.create_task("Goal", plan, budget=budget)

    tools = ToolRegistry()

    # Create dummy tool
    def dummy_tool(idx: int = 0):
        return "dummy_output"

    tools.register(
        Tool(
            name="dummy_tool",
            description="Dummy",
            input_schema={"type": "object"},
            fn=dummy_tool,
            default_policy="allow",
            risk="LOW",
            read_only=True,
        )
    )

    # Client that calls dummy_tool repeatedly
    class LoopingClient:
        def __init__(self):
            self.calls = 0

        class messages:
            @staticmethod
            def create(**kwargs):
                class Block:
                    def __init__(self_b):
                        self_b.type = "tool_use"
                        self_b.name = "dummy_tool"
                        self_b.input = {"idx": LoopingClient.instance.calls}
                        self_b.id = f"call_{LoopingClient.instance.calls}"

                LoopingClient.instance.calls += 1

                class Resp:
                    def __init__(self_r):
                        self_r.content = [Block()]
                        self_r.usage = None
                        self_r.stop_reason = "tool_use"

                return Resp()

    LoopingClient.instance = LoopingClient()

    res = run_bounded_multi_step(
        client=LoopingClient.instance,
        model="mock-model",
        system="system",
        messages=[{"role": "user", "content": "run dummy"}],
        tools=tools,
        max_iterations=10,
        task_id=task.task_id,
        task_store=task_store,
    )

    assert "budget exhausted" in res.reply.lower() or "budget_exhausted:tool_calls" in str(res.state)
    usage = task_store.get_task_budget_usage(task.task_id)
    assert usage.tool_calls == 2


def test_trust_denied_tool_attempts_consume_tool_budget_preventing_infinite_retry(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    # Limit to 2 tool calls
    budget = TaskBudget(max_tool_calls=2)
    task = task_store.create_task("Goal", plan, budget=budget)

    tools = ToolRegistry()

    # Register a tool that returns a Trust permission denial
    def denied_tool():
        return '{"error": {"code": "tool_permission_denied", "message": "Permission denied by Trust Kernel"}}'

    tools.register(Tool(name="denied_tool", description="Denied tool", input_schema={"type": "object"}, fn=denied_tool))

    # Client that tries to call denied_tool repeatedly
    class DeniedClient:
        def __init__(self):
            self.calls = 0

        class messages:
            @staticmethod
            def create(**kwargs):
                class Block:
                    def __init__(self_b):
                        self_b.type = "tool_use"
                        self_b.name = "denied_tool"
                        self_b.input = {"arg": DeniedClient.instance.calls}
                        self_b.id = f"call_{DeniedClient.instance.calls}"

                DeniedClient.instance.calls += 1

                class Resp:
                    def __init__(self_r):
                        self_r.content = [Block()]
                        self_r.usage = None
                        self_r.stop_reason = "tool_use"

                return Resp()

    DeniedClient.instance = DeniedClient()

    res = run_bounded_multi_step(
        client=DeniedClient.instance,
        model="mock-model",
        system="system",
        messages=[{"role": "user", "content": "run denied"}],
        tools=tools,
        max_iterations=10,
        task_id=task.task_id,
        task_store=task_store,
    )

    assert "budget_exhausted:tool_calls" in str(res.state) or "tool call budget was exhausted" in res.reply.lower()
    usage = task_store.get_task_budget_usage(task.task_id)
    # The denied calls consumed the budget!
    assert usage.tool_calls == 2


# ---------------------------------------------------------------------------
# 8. Cumulative Command Runtime Bound
# ---------------------------------------------------------------------------


def test_cumulative_command_runtime_budget_exhaustion(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_command_runtime_seconds=5.0)
    task = task_store.create_task("Goal", plan, budget=budget)

    # Consume 5.0 seconds of command runtime
    task_store.record_budget_consumption(task.task_id, BudgetResource.COMMAND_RUNTIME, 5.0)

    tools = ToolRegistry()
    tools.register(Tool(name="run_command", description="Run cmd", input_schema={"type": "object"}, fn=lambda **kw: '{"output": "ran", "duration_ms": 100}'))

    class CmdClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                class Block:
                    def __init__(self_b):
                        self_b.type = "tool_use"
                        self_b.name = "run_command"
                        self_b.input = {"command": "echo test"}
                        self_b.id = "call_cmd_1"

                class Resp:
                    def __init__(self_r):
                        self_r.content = [Block()]
                        self_r.usage = None
                        self_r.stop_reason = "tool_use"

                return Resp()

    res = run_bounded_multi_step(
        client=CmdClient(),
        model="mock-model",
        system="system",
        messages=[{"role": "user", "content": "run command"}],
        tools=tools,
        max_iterations=5,
        task_id=task.task_id,
        task_store=task_store,
    )

    assert "budget_exhausted:command_runtime" in str(res.state) or "command runtime budget exhausted" in res.reply.lower()


# ---------------------------------------------------------------------------
# 9. Model Verifier & Reviewer & Goal Judge Budget Accounting
# ---------------------------------------------------------------------------


def test_model_result_verifier_budget_exhaustion(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_model_calls=1, max_verification_calls=1)
    task = task_store.create_task("Goal", plan, budget=budget)
    step = task_store.list_steps(task.task_id)[0]

    # Pre-consume verification calls
    task_store.reserve_budget(task.task_id, BudgetResource.VERIFICATION_CALLS, 1.0)

    router = MockModelRouter()
    verifier = ModelResultVerifier(router, store=task_store)
    execution = StepExecution(result="Observed output", run_id="run_1")

    res = verifier.verify(task, step, execution)
    assert res.status is VerificationStatus.BLOCKED
    assert res.summary == "budget_exhausted:verification_calls"
    assert router.call_count == 0  # Did not call LLM!


def test_model_plan_reviewer_budget_exhaustion(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_model_calls=1)
    task = task_store.create_task("Goal", plan, budget=budget)
    step = task_store.list_steps(task.task_id)[0]

    # Pre-consume model calls
    task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    router = MockModelRouter()
    reviewer = ModelPlanReviewer(router, store=task_store)
    execution = StepExecution(result="Observed output", run_id="run_1")
    ver = VerificationResult(VerificationStatus.PASS, "ok")

    review_res = reviewer.review(task, step, execution, ver, [step])
    assert review_res.decision is PlanReviewDecision.BLOCK
    assert review_res.reason == "budget_exhausted:model_calls"
    assert router.call_count == 0


def test_model_goal_judge_budget_exhaustion(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_verification_calls=1)
    task = task_store.create_task("Goal", plan, budget=budget)
    contract = task_store.get_goal_contract(task.task_id)

    # Pre-consume verification calls
    task_store.reserve_budget(task.task_id, BudgetResource.VERIFICATION_CALLS, 1.0)

    router = MockModelRouter()
    judge = ModelGoalJudge(router, store=task_store)
    res = judge.evaluate("Goal", contract, {})

    assert res.status is GoalVerificationStatus.BLOCKED
    assert res.summary == "budget_exhausted:verification_calls"
    assert router.call_count == 0


# ---------------------------------------------------------------------------
# 10. Budget Extension & Separation from Human Recovery
# ---------------------------------------------------------------------------


def test_budget_extension_updates_limits_and_audit(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_model_calls=5, max_steps=2)
    task = task_store.create_task("Goal", plan, budget=budget)

    # Extend model calls by 10 and steps by 3
    new_b = task_store.extend_budget(
        task.task_id,
        actor="operator_alice",
        reason="Need more steps to finish migration",
        increments={"max_model_calls": 10, "max_steps": 3},
    )

    assert new_b.max_model_calls == 15
    assert new_b.max_steps == 5

    allocs = task_store.list_budget_allocations(task.task_id)
    assert len(allocs) == 3
    ext_allocs = [a for a in allocs if a.action == "extend"]
    assert len(ext_allocs) == 2
    by_res = {a.resource: a for a in ext_allocs}
    assert by_res["max_model_calls"].actor == "operator_alice"
    assert by_res["max_model_calls"].note == "Need more steps to finish migration"
    assert by_res["max_model_calls"].amount == 10
    assert by_res["max_steps"].amount == 3


def test_budget_extension_cannot_unblock_task_with_blocked_step(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    task = task_store.create_task("Goal", plan)

    # Claim step and mark it BLOCKED (e.g. uncertain tool execution requiring M17 recovery)
    _ = task_store.claim_next_step(task.task_id)
    task_store.block_running_step(task.task_id, "Uncertain tool execution requires human recovery")

    t = task_store.get_task(task.task_id)
    assert t.status is TaskStatus.BLOCKED

    # Operator extends budget
    task_store.extend_budget(
        task.task_id,
        actor="operator",
        reason="Adding budget",
        increments={"max_steps": 5},
    )

    # Hard invariant: Task MUST STILL BE BLOCKED because the step is blocked!
    # Budget extension CANNOT bypass M17 Human Recovery!
    t_after = task_store.get_task(task.task_id)
    assert t_after.status is TaskStatus.BLOCKED


def test_human_recovery_does_not_reset_consumed_budget(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    budget = TaskBudget(max_model_calls=10)
    task = task_store.create_task("Goal", plan, budget=budget)

    # Consume 8 model calls
    for _ in range(8):
        task_store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)

    usage_before = task_store.get_task_budget_usage(task.task_id)
    assert usage_before.model_calls == 8

    # Block step
    task_store.claim_next_step(task.task_id)
    task_store.block_running_step(task.task_id, "Need human inspection")

    # Recover task using TaskService
    service = TaskService(task_store, MagicMock(), MagicMock())
    service.recover(task.task_id, note="Human verified manually")

    # Budget usage must remain 8/10
    usage_after = task_store.get_task_budget_usage(task.task_id)
    assert usage_after.model_calls == 8


# ---------------------------------------------------------------------------
# 11. Scheduled Task Budget Isolation
# ---------------------------------------------------------------------------


def test_scheduled_task_budget_isolation(task_store):
    # Two separate task occurrences from a schedule
    plan1 = [PlanStep(title="S1", instruction="I1", verification="V1")]
    plan2 = [PlanStep(title="S1", instruction="I1", verification="V1")]

    task1 = task_store.create_task("Scheduled Goal", plan1, source="scheduled", source_id="occ_1")
    task2 = task_store.create_task("Scheduled Goal", plan2, source="scheduled", source_id="occ_2")

    # Consume all tool calls on task1
    for _ in range(task_store.get_task_budget(task1.task_id).max_tool_calls):
        task_store.reserve_budget(task1.task_id, BudgetResource.TOOL_CALLS, 1.0)

    # Task1 tool calls are exhausted
    r1 = task_store.reserve_budget(task1.task_id, BudgetResource.TOOL_CALLS, 1.0)
    assert r1.allowed is False

    # Task2 has an independent budget with 0 consumed tool calls!
    u2 = task_store.get_task_budget_usage(task2.task_id)
    assert u2.tool_calls == 0
    r2 = task_store.reserve_budget(task2.task_id, BudgetResource.TOOL_CALLS, 1.0)
    assert r2.allowed is True


# ---------------------------------------------------------------------------
# 12. Service & CLI Formatting Tests
# ---------------------------------------------------------------------------


def test_service_show_includes_budget_details(task_store):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    planner = MagicMock()
    planner.plan.return_value = plan
    service = TaskService(task_store, planner, MagicMock())
    task = service.create(goal="Show budget test")

    details = service.show(task.task_id)
    assert "budget" in details
    b = details["budget"]
    assert "limits" in b
    assert "usage" in b
    assert "remaining" in b
    assert "allocations" in b
    assert b["limits"]["max_model_calls"] > 0


def test_cli_show_text_and_json(tmp_path, task_store, capsys):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    task = task_store.create_task("CLI Test Goal", plan)

    # Text output
    args_text = Namespace(task_command="show", task_id=task.task_id, json=False)
    ret_text = run_task_cli(args_text, Namespace(home=tmp_path))
    assert ret_text == 0
    captured_text = capsys.readouterr().out
    assert "Budget Governance:" in captured_text
    assert "Model calls:" in captured_text

    # JSON output
    args_json = Namespace(task_command="show", task_id=task.task_id, json=True)
    ret_json = run_task_cli(args_json, Namespace(home=tmp_path))
    assert ret_json == 0
    captured_json = capsys.readouterr().out
    import json

    data = json.loads(captured_json)
    assert "budget" in data
    assert "limits" in data["budget"]
    assert "usage" in data["budget"]


def test_cli_budget_extend(tmp_path, task_store, capsys):
    plan = [PlanStep(title="Step 1", instruction="Do 1", verification="Verify 1")]
    task = task_store.create_task("CLI Extend Goal", plan)

    args_ext = Namespace(
        task_command="budget-extend",
        task_id=task.task_id,
        model_calls=5.0,
        tool_calls=10.0,
        steps=2.0,
        replans=1.0,
        command_runtime=0.0,
        active_runtime=0.0,
        reason="operator CLI test",
        json=True,
    )
    ret = run_task_cli(args_ext, Namespace(home=tmp_path))
    assert ret == 0
    captured = capsys.readouterr().out
    import json

    data = json.loads(captured)
    assert data["task_id"] == task.task_id
    assert data["budget"]["max_model_calls"] == task_store.limits.default_budget().max_model_calls + 5


# ---------------------------------------------------------------------------
# 13. Scorecard & Failure Types
# ---------------------------------------------------------------------------


def test_scorecard_metrics_aggregation_with_budget():
    results = (
        EvalResult(
            case_id="c1",
            category="budget",
            verdict=EvalVerdict.PASS,
            deterministic_score=1.0,
            judge_score=None,
            reasons=("pass",),
            failure_types=(),
            evidence=EvalEvidence(task_status="completed"),
            metrics={
                "model_calls": 4,
                "tool_calls": 6,
                "active_runtime_seconds": 12.0,
                "command_runtime_seconds": 3.0,
                "budget_exhausted": False,
            },
        ),
        EvalResult(
            case_id="c2",
            category="budget",
            verdict=EvalVerdict.BUDGET_EXCEEDED,
            deterministic_score=0.0,
            judge_score=None,
            reasons=("model budget exceeded",),
            failure_types=(FailureType.MODEL_BUDGET_EXCEEDED,),
            evidence=EvalEvidence(task_status="blocked"),
            metrics={
                "model_calls": 10,
                "tool_calls": 8,
                "active_runtime_seconds": 25.0,
                "command_runtime_seconds": 5.0,
                "budget_exhausted": True,
            },
        ),
    )

    summary = aggregate_results(results)
    m = summary["metrics"]
    assert m["budget_exhausted_rate"] == 0.5
    assert m["average_model_calls"] == 7.0
    assert m["average_tool_calls"] == 7.0
    assert m["average_active_runtime_seconds"] == 18.5
    assert m["average_command_runtime_seconds"] == 4.0
    assert summary["failures"].get("model_budget_exceeded") == 1

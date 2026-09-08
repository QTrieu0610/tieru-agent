"""Deterministic verification suite for M30 — Runtime-Owned Step Completion Controller & Evidence Realization.

Covers:
- Controller initialization, bounds, TaskLimits
- Decision transitions: READY_TO_VERIFY, CONTINUE, REPLAN_REQUIRED, BLOCKED, BUDGET_EXHAUSTED
- Model finish_reason / stop / prose vs runtime authority
- Structured evidence requirements realization
- Evidence monotonicity and partial evidence tracking
- Continuation prompt formatting and context directives
- TaskExecutor bounded continuation turn integration
- Budget accounting for continuation turns (MODEL_CALLS, RETRIES)
- Plan review preservation under low budget
"""

import json
import sqlite3

import pytest

from tieru.tasks.controller import (
    StepCompletionAssessment,
    StepCompletionController,
    StepContinuationDecision,
)
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    BudgetResource,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    StepEvidenceRequirement,
    StepExecution,
    StepExecutionKind,
    StepStatus,
    TaskBudget,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.reviewer import CallablePlanReviewer
from tieru.tasks.store import TaskStore, initialize_task_schema


def _init_store(conn: sqlite3.Connection, limits: TaskLimits | None = None) -> TaskStore:
    initialize_task_schema(conn)
    return TaskStore(conn, limits=limits or TaskLimits())


def _make_step(
    position: int = 1,
    title: str = "Test Step",
    instruction: str = "Do something",
    kind: StepExecutionKind = StepExecutionKind.REASONING,
    requirements: tuple[StepEvidenceRequirement, ...] = (),
    verification_instruction: str = "Check result",
) -> TaskStep:
    return TaskStep(
        step_id=f"step_{position}",
        task_id="task_test",
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


class FixedVerifier:
    def __init__(self, status: VerificationStatus = VerificationStatus.PASS):
        self.status = status

    def verify(self, _task, _step, _execution):
        return VerificationResult(self.status, f"verification {self.status.value}")


# ==============================================================================
# 1-6: Controller Initialization & Pure Cognitive REASONING Steps
# ==============================================================================

def test_01_controller_initialization_default_limits():
    ctrl = StepCompletionController()
    assert ctrl.limits.max_execution_turns_per_step == 3


def test_02_controller_initialization_custom_limits():
    limits = TaskLimits(max_execution_turns_per_step=5)
    ctrl = StepCompletionController(limits=limits)
    assert ctrl.limits.max_execution_turns_per_step == 5


def test_03_reasoning_step_with_substantive_answer():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.REASONING)
    execution = StepExecution(result="The calculated total is 42.", run_id="r1")
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.READY_TO_VERIFY
    assert "semantic_answer" in assessment.satisfied_requirements


def test_04_reasoning_step_with_empty_answer():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.REASONING)
    execution = StepExecution(result="", run_id="r1")
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert "substantive_answer" in assessment.missing_requirements


def test_05_reasoning_step_with_whitespace_only():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.REASONING)
    execution = StepExecution(result="    \n\t  ", run_id="r1")
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


def test_06_reasoning_step_with_error_output():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.REASONING)
    execution = StepExecution(result="Error: failed to generate answer", run_id="r1")
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


# ==============================================================================
# 7-15: Tool-Required Steps: READ, WRITE, COMMAND
# ==============================================================================

def test_07_read_step_with_successful_read_tool():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.READ)
    execution = StepExecution(
        result="Read file content",
        run_id="r1",
        tool_calls=({"tool": "filesystem_read", "output": json.dumps({"content": "data", "status": "ok"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.READY_TO_VERIFY


def test_08_read_step_with_tool_omission():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.READ)
    execution = StepExecution(result="I read the file and it has data.", run_id="r1", tool_calls=())
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert len(assessment.missing_requirements) > 0


def test_09_read_step_with_wrong_tool():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.READ)
    execution = StepExecution(
        result="wrote something",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "output": json.dumps({"status": "ok"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


def test_10_write_step_with_successful_write_tool():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(
        result="File written",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "output": json.dumps({"status": "ok", "path": "out.txt"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.READY_TO_VERIFY


def test_11_write_step_with_tool_omission():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(result="I have successfully updated the file.", run_id="r1", tool_calls=())
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert any("artifact_changed" in req for req in assessment.missing_requirements)


def test_12_write_step_with_read_only_tool():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(
        result="inspected file",
        run_id="r1",
        tool_calls=({"tool": "code_read", "output": json.dumps({"content": "def add(): ..."})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


def test_13_command_step_with_successful_run_command():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.COMMAND)
    execution = StepExecution(
        result="Ran test",
        run_id="r1",
        tool_calls=({"tool": "run_command", "output": json.dumps({"exit_code": 0, "stdout": "1 passed"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.READY_TO_VERIFY


def test_14_command_step_with_tool_omission():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="The command ran cleanly and passed.", run_id="r1", tool_calls=())
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert any("command_exit_zero" in req for req in assessment.missing_requirements)


def test_15_command_step_with_failing_exit_code():
    ctrl = StepCompletionController()
    step = _make_step(
        kind=StepExecutionKind.COMMAND,
        requirements=(StepEvidenceRequirement(kind="command_exit_zero", description="Test exit code 0"),),
    )
    execution = StepExecution(
        result="Ran test failed",
        run_id="r1",
        tool_calls=({"tool": "run_command", "output": json.dumps({"exit_code": 1, "stderr": "FAILED"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


# ==============================================================================
# 16-23: Hard Trust Denials, Action Ledger Uncertainty, & Budget Exhaustion
# ==============================================================================

def test_16_action_ledger_uncertainty_in_error_code():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(
        result="Sent external request",
        run_id="r1",
        tool_calls=({"tool": "send_email", "error_code": "tool_execution_uncertain"},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BLOCKED
    assert "human recovery" in assessment.reason.lower()


def test_17_action_ledger_uncertainty_in_output():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.EXTERNAL_ACTION)
    execution = StepExecution(
        result="Action state indeterminate",
        run_id="r1",
        tool_calls=({"tool": "bank_transfer", "output": "tool_execution_uncertain: timeout"},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BLOCKED


def test_18_hard_trust_denial_in_error_code():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(
        result="Denied",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "error_code": "tool_permission_denied"},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BLOCKED
    assert "hard trust policy denial" in assessment.reason.lower()


def test_19_hard_trust_denial_in_output():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(
        result="Denied",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "output": "tool_permission_denied: protected path"},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BLOCKED


def test_20_budget_exhausted_in_tool_calls():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.READ)
    execution = StepExecution(
        result="budget limit",
        run_id="r1",
        tool_calls=({"tool": "filesystem_read", "error_code": "budget_exhausted:tool_calls"},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BUDGET_EXHAUSTED


def test_21_budget_exhausted_in_result_text():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.REASONING)
    execution = StepExecution(result="budget_exhausted:model_calls", run_id="r1")
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BUDGET_EXHAUSTED


def test_22_budget_exhausted_command_runtime():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.COMMAND)
    execution = StepExecution(
        result="timeout",
        run_id="r1",
        tool_calls=({"tool": "run_command", "error_code": "budget_exhausted:command_runtime"},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BUDGET_EXHAUSTED


def test_23_budget_exhausted_active_runtime():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.READ)
    execution = StepExecution(result="budget_exhausted:active_runtime", run_id="r1")
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.BUDGET_EXHAUSTED


# ==============================================================================
# 24-26: Plan Capability Mismatch
# ==============================================================================

def test_24_capability_mismatch_command_step():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="cannot run", run_id="r1")
    visible = {"filesystem_read", "filesystem_write"}
    assessment = ctrl.assess(None, step, execution, visible_tools=visible)
    assert assessment.decision is StepContinuationDecision.REPLAN_REQUIRED
    assert "no visible tool available" in assessment.reason.lower()


def test_25_capability_mismatch_write_step():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(result="cannot write", run_id="r1")
    visible = {"filesystem_read", "code_search"}
    assessment = ctrl.assess(None, step, execution, visible_tools=visible)
    assert assessment.decision is StepContinuationDecision.REPLAN_REQUIRED


def test_26_capability_mismatch_read_step():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.READ)
    execution = StepExecution(result="cannot read", run_id="r1")
    visible = {"run_command"}
    assessment = ctrl.assess(None, step, execution, visible_tools=visible)
    assert assessment.decision is StepContinuationDecision.REPLAN_REQUIRED


# ==============================================================================
# 27-31: Explicit Evidence Requirements Realization & Monotonicity
# ==============================================================================

def test_27_explicit_evidence_requirements_all_satisfied():
    ctrl = StepCompletionController()
    reqs = (
        StepEvidenceRequirement(kind="artifact_changed", description="math_lib.py modified"),
        StepEvidenceRequirement(kind="command_exit_zero", description="pytest passed"),
    )
    step = _make_step(kind=StepExecutionKind.MIXED, requirements=reqs)
    execution = StepExecution(
        result="Fixed and tested",
        run_id="r1",
        tool_calls=(
            {"tool": "filesystem_write", "output": json.dumps({"status": "ok"})},
            {"tool": "run_command", "output": json.dumps({"exit_code": 0, "stdout": "1 passed"})},
        ),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.READY_TO_VERIFY
    assert len(assessment.satisfied_requirements) == 2
    assert len(assessment.missing_requirements) == 0


def test_28_explicit_evidence_requirements_partial_satisfied():
    ctrl = StepCompletionController()
    reqs = (
        StepEvidenceRequirement(kind="artifact_changed", description="math_lib.py modified"),
        StepEvidenceRequirement(kind="command_exit_zero", description="pytest passed"),
    )
    step = _make_step(kind=StepExecutionKind.MIXED, requirements=reqs)
    execution = StepExecution(
        result="Fixed only",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "output": json.dumps({"status": "ok"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert len(assessment.satisfied_requirements) == 1
    assert len(assessment.missing_requirements) == 1
    assert "command_exit_zero" in assessment.missing_requirements[0]


def test_29_partial_evidence_satisfied_list_accurate():
    ctrl = StepCompletionController()
    reqs = (
        StepEvidenceRequirement(kind="artifact_changed", description="created file"),
        StepEvidenceRequirement(kind="tool_success", description="verified tool"),
    )
    step = _make_step(kind=StepExecutionKind.WRITE, requirements=reqs)
    execution = StepExecution(
        result="done",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "output": json.dumps({"status": "ok"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert any("artifact_changed" in r for r in assessment.satisfied_requirements)


def test_30_partial_evidence_missing_list_accurate():
    ctrl = StepCompletionController()
    reqs = (
        StepEvidenceRequirement(kind="artifact_changed", description="created file"),
        StepEvidenceRequirement(kind="command_exit_zero", description="tested"),
    )
    step = _make_step(kind=StepExecutionKind.WRITE, requirements=reqs)
    execution = StepExecution(
        result="done",
        run_id="r1",
        tool_calls=({"tool": "filesystem_write", "output": json.dumps({"status": "ok"})},),
    )
    assessment = ctrl.assess(None, step, execution)
    assert any("command_exit_zero" in r for r in assessment.missing_requirements)


def test_31_evidence_monotonicity_across_cumulative_turns():
    ctrl = StepCompletionController()
    reqs = (
        StepEvidenceRequirement(kind="artifact_changed", description="created file"),
        StepEvidenceRequirement(kind="command_exit_zero", description="tested"),
    )
    step = _make_step(kind=StepExecutionKind.WRITE, requirements=reqs)
    
    # Turn 1
    t1_calls = ({"tool": "filesystem_write", "output": json.dumps({"status": "ok"})},)
    ass1 = ctrl.assess(None, step, StepExecution("wrote", "r1", t1_calls))
    assert ass1.decision is StepContinuationDecision.CONTINUE
    
    # Turn 2 cumulative execution
    t2_calls = t1_calls + ({"tool": "run_command", "output": json.dumps({"exit_code": 0})},)
    ass2 = ctrl.assess(None, step, StepExecution("tested", "r1", t2_calls))
    assert ass2.decision is StepContinuationDecision.READY_TO_VERIFY


# ==============================================================================
# 32-38: Model Prose vs Runtime Authority & Continuation Prompts
# ==============================================================================

def test_32_model_claiming_completion_does_not_satisfy_write_step():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.WRITE)
    execution = StepExecution(result="The step is complete and file is created.", run_id="r1", tool_calls=())
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


def test_33_model_claiming_completion_does_not_satisfy_command_step():
    ctrl = StepCompletionController()
    step = _make_step(kind=StepExecutionKind.COMMAND)
    execution = StepExecution(result="I have executed the tests and all passed.", run_id="r1", tool_calls=())
    assessment = ctrl.assess(None, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE


def test_34_continuation_prompt_contains_objective():
    ctrl = StepCompletionController()
    step = _make_step(position=2, title="Fix Bug", instruction="Fix subtraction in math_lib.py")
    assessment = StepCompletionAssessment(
        decision=StepContinuationDecision.CONTINUE,
        satisfied_requirements=(),
        missing_requirements=("[artifact_changed] Modified math_lib.py",),
        reason="Missing artifact",
    )
    prompt = ctrl.format_continuation_prompt(step, assessment)
    assert "Fix Bug" in prompt
    assert "Fix subtraction in math_lib.py" in prompt


def test_35_continuation_prompt_lists_missing_requirements():
    ctrl = StepCompletionController()
    step = _make_step(position=1, title="Test", instruction="Run test")
    assessment = StepCompletionAssessment(
        decision=StepContinuationDecision.CONTINUE,
        satisfied_requirements=(),
        missing_requirements=("[command_exit_zero] exit code 0",),
        reason="Missing command exit",
    )
    prompt = ctrl.format_continuation_prompt(step, assessment)
    assert "[command_exit_zero] exit code 0" in prompt


def test_36_continuation_prompt_lists_satisfied_requirements():
    ctrl = StepCompletionController()
    step = _make_step(position=1, title="Edit and Test", instruction="Edit and test")
    assessment = StepCompletionAssessment(
        decision=StepContinuationDecision.CONTINUE,
        satisfied_requirements=("[artifact_changed] math_lib.py",),
        missing_requirements=("[command_exit_zero] pytest",),
        reason="Missing command",
    )
    prompt = ctrl.format_continuation_prompt(step, assessment)
    assert "[artifact_changed] math_lib.py" in prompt
    assert "Already satisfied evidence" in prompt


def test_37_continuation_prompt_instructs_to_stay_on_current_step():
    ctrl = StepCompletionController()
    step = _make_step(position=1, title="Current Step", instruction="Do work")
    assessment = StepCompletionAssessment(
        decision=StepContinuationDecision.CONTINUE,
        satisfied_requirements=(),
        missing_requirements=("some_req",),
        reason="missing",
    )
    prompt = ctrl.format_continuation_prompt(step, assessment)
    assert "Stay on this current step. Do not begin subsequent steps." in prompt


def test_38_continuation_prompt_redacts_secrets():
    ctrl = StepCompletionController()
    step = _make_step(position=1, title="Auth Step", instruction="Use sk-ant-api03-abcdef123456 to login")
    assessment = StepCompletionAssessment(
        decision=StepContinuationDecision.CONTINUE,
        satisfied_requirements=(),
        missing_requirements=("auth_req",),
        reason="missing",
    )
    prompt = ctrl.format_continuation_prompt(step, assessment)
    assert "sk-ant-api03-abcdef123456" not in prompt
    assert "[REDACTED" in prompt


# ==============================================================================
# 39-48: TaskExecutor Bounded Continuation Integration & Event Emission
# ==============================================================================

def test_39_executor_single_turn_when_ready_to_verify(tmp_path):
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    plan = [PlanStep("Step 1", "Reasoning step", "Verify 1")]
    task = store.create_task("Goal", plan)
    
    runner_calls = 0
    def mock_runner(_task, _step, _context, _obs):
        nonlocal runner_calls
        runner_calls += 1
        return StepExecution("Valid substantive reasoning answer.", "run_1")

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    result = executor.run_next(task.task_id)
    assert runner_calls == 1
    assert result.step.status in {StepStatus.SUCCEEDED, StepStatus.RUNNING}


def test_40_executor_two_turns_when_first_turn_needs_continuation(tmp_path):
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan)
    
    turn = 0
    def mock_runner(_task, _step, _context, _obs):
        nonlocal turn
        turn += 1
        if turn == 1:
            return StepExecution("I wrote the code.", "run_1", ())
        return StepExecution("Code written", "run_2", ({"tool": "filesystem_write", "output": '{"status":"ok"}'},))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    result = executor.run_next(task.task_id)
    assert turn == 2
    assert result.step.status is StepStatus.SUCCEEDED


def test_41_executor_continuation_exhausted_respects_max_turns(tmp_path):
    conn = sqlite3.connect(":memory:")
    limits = TaskLimits(max_execution_turns_per_step=3)
    store = _init_store(conn, limits=limits)
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan)
    
    turn = 0
    def mock_runner(_task, _step, _context, _obs):
        nonlocal turn
        turn += 1
        return StepExecution(f"Turn {turn} prose only", f"run_{turn}", ())

    recorded_events = []
    def obs(kind, payload):
        recorded_events.append((kind, payload))

    executor = TaskExecutor(store, mock_runner, FixedVerifier(VerificationStatus.FAIL), limits=limits)
    result = executor.run_next(task.task_id, observer=obs)
    assert turn == 3
    assert any(kind == "step_continuation_exhausted" for kind, _ in recorded_events)
    assert result.step.status is StepStatus.FAILED


def test_42_executor_reserves_model_calls_and_retries_for_continuation(tmp_path):
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    budget = TaskBudget(max_model_calls=3, max_retries=2)
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan, budget=budget)
    
    turn = 0
    def mock_runner(_task, _step, _context, _obs):
        nonlocal turn
        turn += 1
        store.record_budget_consumption(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
        if turn == 1:
            return StepExecution("Turn 1 prose", "r1", ())
        return StepExecution("Turn 2 written", "r2", ({"tool": "filesystem_write", "output": '{"status":"ok"}'},))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id)
    usage = store.get_task_budget_usage(task.task_id)
    assert usage.model_calls >= 1
    assert usage.retries >= 1


def test_43_executor_blocks_continuation_when_model_calls_exhausted(tmp_path):
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    budget = TaskBudget(max_model_calls=1)
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan, budget=budget)
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    
    turn = 0
    def mock_runner(_task, _step, _context, _obs):
        nonlocal turn
        turn += 1
        return StepExecution("prose only", "r1", ())

    recorded_events = []
    def obs(kind, payload):
        recorded_events.append((kind, payload))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id, observer=obs)
    assert turn == 1
    assert any(kind == "step_continuation_exhausted" and p.get("reason") == "budget_exhausted" for kind, p in recorded_events)


def test_44_executor_blocks_continuation_when_retries_exhausted(tmp_path):
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    budget = TaskBudget(max_model_calls=5, max_retries=0)
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan, budget=budget)
    
    turn = 0
    def mock_runner(_task, _step, _context, _obs):
        nonlocal turn
        turn += 1
        return StepExecution("prose only", "r1", ())

    recorded_events = []
    def obs(kind, payload):
        recorded_events.append((kind, payload))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id, observer=obs)
    assert turn == 1
    assert any(kind == "step_continuation_exhausted" for kind, _ in recorded_events)


def test_45_executor_emits_step_completion_assessed_event():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    plan = [PlanStep("Step 1", "Reasoning", "Verify")]
    task = store.create_task("Goal", plan)
    
    events = []
    def obs(kind, payload):
        events.append((kind, payload))

    def mock_runner(_t, _s, _c, _o):
        return StepExecution("Substantive answer here.", "r1")

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id, observer=obs)
    assessed = [p for k, p in events if k == "step_completion_assessed"]
    assert len(assessed) == 1
    assert assessed[0]["decision"] == "ready_to_verify"


def test_46_executor_emits_step_continuation_requested_event():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    plan = [PlanStep("Step 1", "Write file", "Verify file", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan)

    events = []
    def obs(kind, payload):
        events.append((kind, payload))

    turn = 0
    def mock_runner(_t, _s, _c, _o):
        nonlocal turn
        turn += 1
        if turn == 1:
            return StepExecution("No tools", "r1", ())
        return StepExecution("Written", "r2", ({"tool": "filesystem_write", "output": '{"status":"ok"}'},))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id, observer=obs)
    requested = [p for k, p in events if k == "step_continuation_requested"]
    assert len(requested) == 1
    assert requested[0]["turn_number"] == 2


def test_47_executor_emits_step_evidence_partial_event():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    reqs = (
        StepEvidenceRequirement(kind="artifact_changed", description="math_lib.py"),
        StepEvidenceRequirement(kind="command_exit_zero", description="pytest"),
    )
    plan = [PlanStep("Step 1", "Modify and test", "Verify", execution_kind=StepExecutionKind.MIXED, evidence_requirements=reqs)]
    task = store.create_task("Goal", plan)

    events = []
    def obs(kind, payload):
        events.append((kind, payload))

    turn = 0
    def mock_runner(_t, _s, _c, _o):
        nonlocal turn
        turn += 1
        if turn == 1:
            return StepExecution("Wrote file only", "r1", ({"tool": "filesystem_write", "output": '{"status":"ok"}'},))
        return StepExecution("Tested", "r2", ({"tool": "run_command", "output": '{"exit_code":0}'},))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id, observer=obs)
    partial_events = [p for k, p in events if k == "step_evidence_partial"]
    assert len(partial_events) >= 1


def test_48_executor_emits_step_evidence_complete_event():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    plan = [PlanStep("Step 1", "Write code", "Verify", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan)

    events = []
    def obs(kind, payload):
        events.append((kind, payload))

    def mock_runner(_t, _s, _c, _o):
        return StepExecution("Wrote file", "r1", ({"tool": "filesystem_write", "output": '{"status":"ok"}'},))

    executor = TaskExecutor(store, mock_runner, FixedVerifier())
    executor.run_next(task.task_id, observer=obs)
    complete_events = [p for k, p in events if k == "step_evidence_complete"]
    assert len(complete_events) == 1


# ==============================================================================
# 49-50: Plan Review Preservation Under Low Model Calls Budget
# ==============================================================================

def test_49_plan_review_preserves_existing_plan_when_model_calls_low():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    budget = TaskBudget(max_model_calls=1)
    plan = [
        PlanStep("Step 1", "First step", "Verify 1"),
        PlanStep("Step 2", "Second step", "Verify 2"),
    ]
    task = store.create_task("Goal", plan, budget=budget)
    
    # Pre-consume model call budget
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    
    def mock_runner(_t, _s, _c, _o):
        return StepExecution("Step 1 passed cleanly", "r1")

    reviewer = CallablePlanReviewer(lambda *args: pytest.fail("Reviewer should not be called"))
    events = []
    def obs(kind, payload):
        events.append((kind, payload))

    executor = TaskExecutor(store, mock_runner, FixedVerifier(), reviewer=reviewer)
    res = executor.run_next(task.task_id, observer=obs)
    
    # Task remains RUNNING and planned steps are preserved
    assert res.task.status is TaskStatus.RUNNING
    assert any(
        k == "task_plan_reviewed" and p.get("decision") == "keep"
        for k, p in events
    )


def test_50_plan_review_preserves_existing_plan_when_reviewer_blocks_on_budget():
    conn = sqlite3.connect(":memory:")
    store = _init_store(conn)
    plan = [
        PlanStep("Step 1", "First step", "Verify 1"),
        PlanStep("Step 2", "Second step", "Verify 2"),
    ]
    task = store.create_task("Goal", plan)
    
    def mock_runner(_t, _s, _c, _o):
        return StepExecution("Step 1 passed cleanly", "r1")

    # Reviewer returns BLOCK specifically due to budget exhaustion
    reviewer = CallablePlanReviewer(
        lambda *args: PlanReviewResult(
            decision=PlanReviewDecision.BLOCK,
            reason="budget_exhausted:model_calls",
            remaining_steps=(),
        )
    )
    events = []
    def obs(kind, payload):
        events.append((kind, payload))

    executor = TaskExecutor(store, mock_runner, FixedVerifier(), reviewer=reviewer)
    res = executor.run_next(task.task_id, observer=obs)
    
    # Task should remain RUNNING with preserved plan, NOT blocked!
    assert res.task.status is TaskStatus.RUNNING
    assert any(
        k == "task_plan_reviewed" and p.get("decision") == "keep"
        for k, p in events
    )

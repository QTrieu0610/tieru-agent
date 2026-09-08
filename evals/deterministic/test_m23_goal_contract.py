"""Deterministic test suite for M23 — Goal Contract & Task-Level Success Verification."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tieru.config import Settings
from tieru.db import connect
from tieru.evals.corpus import load_corpus
from tieru.evals.metrics import aggregate_results
from tieru.evals.runner import EvalRunner
from tieru.tasks.cli import run_task_cli
from tieru.tasks.contract import (
    CallableGoalContractBuilder,
    DeterministicGoalContractBuilder,
    ModelGoalContractBuilder,
    extract_explicit_constraints,
    parse_goal_contract_output,
)
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.goal_verifier import (
    CallableTaskGoalVerifier,
    DeterministicGoalVerifier,
    LayeredTaskGoalVerifier,
    ModelGoalJudge,
    compute_evidence_hash,
)
from tieru.tasks.models import (
    CriterionResult,
    CriterionStatus,
    GoalConstraint,
    GoalContract,
    GoalContractError,
    GoalVerificationStatus,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    StepExecution,
    StepStatus,
    SuccessCriterion,
    Task,
    TaskGoalVerification,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.reviewer import CallablePlanReviewer
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore


def make_task(
    task_id: str = "t1",
    goal: str = "g",
    status: TaskStatus = TaskStatus.RUNNING,
) -> Task:
    return Task(
        task_id=task_id,
        goal=goal,
        status=status,
        current_step_id=None,
        source="test",
        session_id="sess",
        created_at="now",
        updated_at="now",
        completed_at=None,
    )


def make_step(
    step_id: str = "s1",
    task_id: str = "t1",
    position: int = 1,
    status: StepStatus = StepStatus.SUCCEEDED,
) -> TaskStep:
    return TaskStep(
        step_id=step_id,
        task_id=task_id,
        position=position,
        title="Title",
        instruction="Instruction",
        verification_instruction="Verify instruction",
        status=status,
        attempt_count=1,
        max_attempts=3,
        result="Result",
        result_size=6,
        result_truncated=False,
        verification_status=VerificationStatus.PASS,
        verification_summary="Passed",
        execution_run_id="run_1",
        started_at="now",
        completed_at="now",
        updated_at="now",
    )


class ScriptedRunner:
    def __init__(self, step_outputs: dict[int, str] | None = None) -> None:
        self.step_outputs = step_outputs or {}
        self.calls: list[int] = []

    def __call__(self, task: Task, step: TaskStep, context: str, observer) -> StepExecution:
        self.calls.append(step.position)
        output = self.step_outputs.get(step.position, f"Output for step {step.position}")
        return StepExecution(result=output, run_id=f"run_step_{step.position}", tool_calls=())


class FixedStepVerifier:
    def __init__(self, status: VerificationStatus = VerificationStatus.PASS) -> None:
        self.status = status

    def verify(self, task: Task, step: TaskStep, execution: StepExecution) -> VerificationResult:
        return VerificationResult(self.status, f"Step {step.position} {self.status.value}")


@pytest.fixture
def store_and_conn(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    yield store, conn
    conn.close()


# ============================================================================
# Part 1: Goal Contract Models & Limits
# ============================================================================


def test_goal_contract_creation_and_fields():
    sc = SuccessCriterion("sc1", "Tests pass", "deterministic", "pytest output", True)
    c = GoalConstraint("c1", "Do not modify public API", "user_intent")
    contract = GoalContract("gc_1", "task_1", "Implement feature", (sc,), (c,))
    assert contract.contract_id == "gc_1"
    assert contract.task_id == "task_1"
    assert contract.goal == "Implement feature"
    assert len(contract.success_criteria) == 1
    assert len(contract.constraints) == 1


def test_task_limits_max_criteria_and_constraints():
    limits = TaskLimits(max_criteria_per_contract=3, max_constraints_per_contract=2)
    assert limits.max_criteria_per_contract == 3
    assert limits.max_constraints_per_contract == 2


# ============================================================================
# Part 2: Explicit Constraint Extraction & Parsing
# ============================================================================


def test_extract_explicit_constraints():
    goal = "Refactor authentication without touching database schema and do not modify public API. Never delete tests."
    constraints = extract_explicit_constraints(goal)
    descriptions = [c.description.lower() for c in constraints]
    assert any("without touching database schema" in d for d in descriptions)
    assert any("do not modify public api" in d for d in descriptions)
    assert any("never delete tests" in d for d in descriptions)


def test_parse_goal_contract_output_valid():
    text = json.dumps({
        "success_criteria": [
            {"id": "sc1", "description": "Unit tests pass with 0 errors", "verification_kind": "deterministic", "required_evidence": "pytest exit 0"}
        ],
        "constraints": [
            {"id": "c1", "description": "Do not delete migrations", "constraint_kind": "user_intent"}
        ]
    })
    contract = parse_goal_contract_output("My goal", text, TaskLimits())
    assert len(contract.success_criteria) == 1
    assert contract.success_criteria[0].criterion_id == "sc1"
    assert len(contract.constraints) == 1
    assert contract.constraints[0].description == "Do not delete migrations"


def test_parse_goal_contract_preserves_user_constraints():
    user_constraint = GoalConstraint("c_user", "Do not modify public API", "user_intent")
    text = json.dumps({
        "success_criteria": [{"id": "sc1", "description": "Done", "verification_kind": "deterministic"}]
    })
    contract = parse_goal_contract_output("My goal", text, TaskLimits(), user_constraints=(user_constraint,))
    assert any(c.description == "Do not modify public API" for c in contract.constraints)


def test_parse_goal_contract_malformed_raises_error():
    with pytest.raises(GoalContractError):
        parse_goal_contract_output("My goal", "not valid json {", TaskLimits())


# ============================================================================
# Part 3: Contract Builder (Deterministic, Model, Callable)
# ============================================================================


def test_deterministic_contract_builder():
    builder = DeterministicGoalContractBuilder()
    contract = builder.build("Fix bug without breaking existing tests")
    assert len(contract.success_criteria) >= 1
    assert any("without breaking existing tests" in c.description.lower() for c in contract.constraints)


def test_callable_contract_builder():
    injected = GoalContract("gc_test", "t1", "Injected goal", (), ())
    builder = CallableGoalContractBuilder(lambda g: injected)
    contract = builder.build("Any goal")
    assert contract.contract_id == "gc_test"


def test_model_goal_contract_builder_fallback():
    class MockRouter:
        def client(self, role):
            raise RuntimeError("Model unavailable")

    builder = ModelGoalContractBuilder(MockRouter())
    contract = builder.build("Add feature without breaking build")
    assert contract.goal == "Add feature without breaking build"
    assert len(contract.success_criteria) >= 1


# ============================================================================
# Part 4: SQLite Persistence & Transactional Invariants
# ============================================================================


def test_create_task_persists_goal_contract(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    sc = SuccessCriterion("sc1", "Tests pass", "deterministic", "pytest exit 0", True)
    c = GoalConstraint("c1", "Do not modify public API", "user_intent")
    contract = GoalContract("gc_custom", "", "Custom goal", (sc,), (c,))

    task = store.create_task("Custom goal", plan, contract=contract, source="test")
    retrieved = store.get_goal_contract(task.task_id)

    assert retrieved.contract_id == "gc_custom"
    assert retrieved.task_id == task.task_id
    assert len(retrieved.success_criteria) == 1
    assert retrieved.success_criteria[0].description == "Tests pass"
    assert len(retrieved.constraints) == 1
    assert retrieved.constraints[0].description == "Do not modify public API"


def test_record_and_get_goal_verifications(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")

    cr = CriterionResult("sc1", CriterionStatus.PASS, "Evidence ok")
    gv1 = TaskGoalVerification("ver_1", task.task_id, GoalVerificationStatus.FAIL_REPLANABLE, (cr,), "Initial fail", 0, "hash1")
    store.record_goal_verification(task.task_id, gv1)

    latest = store.get_latest_goal_verification(task.task_id)
    assert latest is not None
    assert latest.verification_id == "ver_1"
    assert latest.status is GoalVerificationStatus.FAIL_REPLANABLE

    gv2 = TaskGoalVerification("ver_2", task.task_id, GoalVerificationStatus.PASS, (cr,), "Second pass", 1, "hash2")
    store.record_goal_verification(task.task_id, gv2)

    latest2 = store.get_latest_goal_verification(task.task_id)
    assert latest2.verification_id == "ver_2"
    assert latest2.status is GoalVerificationStatus.PASS

    all_ver = store.list_goal_verifications(task.task_id)
    assert len(all_ver) == 2
    assert all_ver[0].verification_id == "ver_1"
    assert all_ver[1].verification_id == "ver_2"


# ============================================================================
# Part 5: Task-Level Goal Verifiers
# ============================================================================


def test_deterministic_goal_verifier_action_uncertainty_blocks():
    verifier = DeterministicGoalVerifier()
    task = make_task()
    sc = SuccessCriterion("sc1", "Cond 1", "deterministic")
    contract = GoalContract("gc1", "t1", "g", (sc,), ())
    step = make_step()

    result = verifier.verify(task, contract, [step], execution_evidence={"has_uncertain_action": True})
    assert result.status is GoalVerificationStatus.BLOCKED
    assert "uncertainty" in result.summary.lower()


def test_deterministic_goal_verifier_catches_false_green():
    verifier = DeterministicGoalVerifier()
    task = make_task()
    sc = SuccessCriterion("sc1", "Cond 1", "deterministic")
    c = GoalConstraint("c1", "Do not skip or disable tests", "user_intent")
    contract = GoalContract("gc1", "t1", "g", (sc,), (c,))
    step = make_step()

    result = verifier.verify(task, contract, [step], execution_evidence={"tests_skipped": True})
    assert result.status is GoalVerificationStatus.FAIL_TERMINAL
    assert "constraint" in result.summary.lower()


def test_deterministic_goal_verifier_catches_public_api_violation():
    verifier = DeterministicGoalVerifier()
    task = make_task()
    sc = SuccessCriterion("sc1", "Cond 1", "deterministic")
    c = GoalConstraint("c1", "Do not modify public API", "user_intent")
    contract = GoalContract("gc1", "t1", "g", (sc,), (c,))
    step = make_step()

    result = verifier.verify(task, contract, [step], execution_evidence={"public_api_modified": True})
    assert result.status is GoalVerificationStatus.FAIL_TERMINAL
    assert "public api" in result.summary.lower()


def test_deterministic_goal_verifier_rejects_text_only_claim():
    verifier = DeterministicGoalVerifier()
    task = make_task()
    sc = SuccessCriterion("sc1", "Cond 1", "deterministic")
    contract = GoalContract("gc1", "t1", "g", (sc,), ())
    step = make_step()

    result = verifier.verify(task, contract, [step], execution_evidence={"text_only_claim": True})
    assert result.status is GoalVerificationStatus.UNKNOWN
    assert "insufficient" in result.summary.lower()


def test_compute_evidence_hash_canonical():
    h1 = compute_evidence_hash({"b": 2, "a": 1})
    h2 = compute_evidence_hash({"a": 1, "b": 2})
    assert h1 == h2
    assert len(h1) == 64


def test_layered_goal_verifier_prefers_deterministic_terminal():
    class DummyJudge:
        called = False
        def evaluate(self, *args, **kwargs):
            self.called = True
            raise AssertionError("Judge should not be called")

    judge = DummyJudge()
    verifier = LayeredTaskGoalVerifier(judge=judge)
    task = make_task()
    sc = SuccessCriterion("sc1", "Cond 1", "deterministic")
    c = GoalConstraint("c1", "Do not skip tests", "user_intent")
    contract = GoalContract("gc1", "t1", "g", (sc,), (c,))
    step = make_step()

    res = verifier.verify(task, contract, [step], execution_evidence={"tests_skipped": True})
    assert res.status is GoalVerificationStatus.FAIL_TERMINAL
    assert not judge.called


# ============================================================================
# Part 6: Task Execution State Machine & Completion Invariants
# ============================================================================


def test_task_does_not_complete_from_steps_alone(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")
    claim = store.claim_next_step(task.task_id)

    # finish_step with auto_complete=False leaves task in RUNNING
    task_after, _ = store.finish_step(
        claim.step.step_id,
        result="Done",
        verification=VerificationResult(VerificationStatus.PASS, "Step passed"),
        auto_complete=False,
    )
    assert task_after.status is TaskStatus.RUNNING
    assert store.count_remaining_steps(task.task_id) == 0


def test_executor_transitions_to_completed_only_on_goal_pass(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")

    goal_ver = TaskGoalVerification("v1", task.task_id, GoalVerificationStatus.PASS, (), "All criteria pass")
    goal_verifier = CallableTaskGoalVerifier(lambda t, c, s, e: goal_ver)

    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        goal_verifier=goal_verifier,
    )

    result = executor.run_next(task.task_id)
    assert result.task.status is TaskStatus.COMPLETED
    assert result.code == "task_step_succeeded"


def test_executor_replan_on_fail_replanable(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")

    call_count = 0
    def mock_verify(t, c, s, e):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return TaskGoalVerification("", t.task_id, GoalVerificationStatus.FAIL_REPLANABLE, (), "Need extra step")
        return TaskGoalVerification("", t.task_id, GoalVerificationStatus.PASS, (), "Passed on replan")

    def mock_review(t, st, ex, v, all_steps):
        return PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Replan triggered by goal verifier",
            (PlanStep("Replacement Step 2", "Do step 2", "Check step 2"),)
        )

    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        reviewer=CallablePlanReviewer(mock_review),
        goal_verifier=CallableTaskGoalVerifier(mock_verify),
    )

    # Step 1 finishes, goal verifier returns FAIL_REPLANABLE -> triggers replan, task stays RUNNING
    r1 = executor.run_next(task.task_id)
    assert r1.task.status is TaskStatus.RUNNING
    replan_count = store.count_revisions(task.task_id, exclude_initial=True)
    assert replan_count == 1

    # Step 2 runs, goal verifier returns PASS -> transitions to COMPLETED
    r2 = executor.run_next(task.task_id)
    assert r2.task.status is TaskStatus.COMPLETED


def test_executor_blocks_when_replan_limit_exhausted(store_and_conn):
    _store, conn = store_and_conn
    limits = TaskLimits(max_replans_per_task=1)
    store = TaskStore(conn, limits=limits)

    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")

    def mock_verify(t, c, s, e):
        return TaskGoalVerification("", t.task_id, GoalVerificationStatus.FAIL_REPLANABLE, (), "Fails repeatedly")

    def mock_review(t, st, ex, v, all_steps):
        return PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Replan triggered by goal verifier",
            (PlanStep("Replacement Step", "Do step", "Check step"),)
        )

    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        reviewer=CallablePlanReviewer(mock_review),
        goal_verifier=CallableTaskGoalVerifier(mock_verify),
        limits=limits,
    )

    # 1st execution triggers replan 1
    r1 = executor.run_next(task.task_id)
    assert r1.task.status is TaskStatus.RUNNING

    # 2nd execution fails goal verification again, exceeding replan limit -> task is BLOCKED
    r2 = executor.run_next(task.task_id)
    assert r2.task.status is TaskStatus.BLOCKED


def test_executor_fails_on_fail_terminal(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")

    goal_ver = TaskGoalVerification("v1", task.task_id, GoalVerificationStatus.FAIL_TERMINAL, (), "Constraint broken")
    goal_verifier = CallableTaskGoalVerifier(lambda t, c, s, e: goal_ver)

    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        goal_verifier=goal_verifier,
    )

    result = executor.run_next(task.task_id)
    assert result.task.status is TaskStatus.FAILED


def test_executor_blocks_on_unknown(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")

    goal_ver = TaskGoalVerification("v1", task.task_id, GoalVerificationStatus.UNKNOWN, (), "Prose only")
    goal_verifier = CallableTaskGoalVerifier(lambda t, c, s, e: goal_ver)

    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        goal_verifier=goal_verifier,
    )

    result = executor.run_next(task.task_id)
    assert result.task.status is TaskStatus.BLOCKED


# ============================================================================
# Part 7: Crash Recovery Reconciliation
# ============================================================================


def test_crash_recovery_reconciles_verified_pass(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")
    claim = store.claim_next_step(task.task_id)

    # Step finishes with auto_complete=False
    store.finish_step(
        claim.step.step_id,
        result="Done",
        verification=VerificationResult(VerificationStatus.PASS, "Ok"),
        auto_complete=False,
    )

    # Goal verification PASS is recorded, but process crashes before store.complete_task()
    gv = TaskGoalVerification("ver_crash", task.task_id, GoalVerificationStatus.PASS, (), "Verified ok")
    store.record_goal_verification(task.task_id, gv)

    # Process restarts and attempts claim_next_step:
    claim_after = store.claim_next_step(task.task_id)
    assert claim_after.task.status is TaskStatus.COMPLETED
    assert claim_after.step is None


def test_resume_reconciles_task_completion(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    task = store.create_task("Goal", plan, source="test")
    claim = store.claim_next_step(task.task_id)

    store.finish_step(
        claim.step.step_id,
        result="Done",
        verification=VerificationResult(VerificationStatus.PASS, "Ok"),
        auto_complete=False,
    )
    gv = TaskGoalVerification("ver_crash", task.task_id, GoalVerificationStatus.PASS, (), "Verified ok")
    store.record_goal_verification(task.task_id, gv)

    # Reconcile explicitly
    reconciled = store.reconcile_task_completion(task.task_id)
    assert reconciled.status is TaskStatus.COMPLETED


# ============================================================================
# Part 8: CLI Formatting & Show Command
# ============================================================================


def test_cli_show_formats_goal_contract_and_verification(tmp_path: Path, capsys):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    store = TaskStore(conn)

    sc1 = SuccessCriterion("sc1", "Endpoint returns 200", "deterministic", "curl output", True)
    c1 = GoalConstraint("c1", "Do not modify auth schema", "user_intent")
    contract = GoalContract("gc1", "t1", "Add healthz endpoint", (sc1,), (c1,))
    task = store.create_task("Add healthz endpoint", [PlanStep("Step 1", "Add endpoint", "curl")], contract=contract, source="cli")

    gv = TaskGoalVerification(
        "ver_show",
        task.task_id,
        GoalVerificationStatus.PASS,
        (CriterionResult("sc1", CriterionStatus.PASS, "200 OK observed"),),
        "All verified",
    )
    store.record_goal_verification(task.task_id, gv)
    conn.close()

    # Text output
    args_text = Namespace(task_command="show", task_id=task.task_id, json=False)
    run_task_cli(args_text, settings)
    captured = capsys.readouterr().out
    assert "Constraints:" in captured
    assert "Do not modify auth schema" in captured
    assert "Success Criteria:" in captured
    assert "✓ [sc1] Endpoint returns 200" in captured
    assert "Goal Verification: PASS" in captured

    # JSON output
    args_json = Namespace(task_command="show", task_id=task.task_id, json=True)
    run_task_cli(args_json, settings)
    json_out = json.loads(capsys.readouterr().out)
    assert "goal_contract" in json_out
    assert json_out["goal_contract"]["contract_id"] == "gc1"
    assert "goal_verification" in json_out
    assert json_out["goal_verification"]["status"] == "pass"


# ============================================================================
# Part 9: Replay Events Emission
# ============================================================================


def test_replay_events_emitted_during_task_lifecycle(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)

    events: list[tuple[str, dict]] = []
    def observer(kind, payload):
        events.append((kind, payload))

    plan = [PlanStep("Step 1", "Do step 1", "Check step 1")]
    sc1 = SuccessCriterion("sc1", "Criteria 1", "deterministic")
    contract = GoalContract("gc1", "", "Goal", (sc1,), ())

    planner = SimpleNamespace(plan=lambda g: plan)
    goal_verifier = CallableTaskGoalVerifier(
        lambda t, c, s, e: TaskGoalVerification("ver1", t.task_id, GoalVerificationStatus.PASS, (CriterionResult("sc1", CriterionStatus.PASS, "ok"),), "Pass")
    )
    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        goal_verifier=goal_verifier,
    )
    service = TaskService(store, planner, executor, contract_builder=CallableGoalContractBuilder(lambda g: contract))

    task = service.create(goal="Goal", observer=observer)
    assert any(e[0] == "goal_contract_created" for e in events)

    service.run(task.task_id, max_steps=1, observer=observer)
    event_names = [e[0] for e in events]
    assert "goal_verification_started" in event_names
    assert "goal_criterion_evaluated" in event_names
    assert "goal_verification_passed" in event_names
    conn.close()


# ============================================================================
# Part 10: Corpus Cases A through H Validation
# ============================================================================


def test_corpus_cases_a_through_h_evaluate_accurately():
    corpus = load_corpus("evals/cases/goal_contract.json")
    assert len(corpus.cases) == 8
    runner = EvalRunner()
    results = tuple(runner.run_case(c) for c in corpus.cases)
    summary = aggregate_results(results)

    assert summary["metrics"]["failed"] == 0
    assert summary["metrics"]["false_success_rate"] == 0.0
    assert summary["metrics"]["goal_false_pass_rate"] == 0.0
    assert summary["metrics"]["goal_verification_accuracy"] == 1.0
    assert summary["reliability_pass"] is True


# ============================================================================
# Part 11: Comprehensive Contract & Error Boundary Tests
# ============================================================================


def test_goal_contract_error_on_missing_description():
    with pytest.raises(GoalContractError, match="missing description"):
        parse_goal_contract_output(
            "Goal",
            json.dumps({"success_criteria": [{"id": "sc1", "description": ""}]}),
            TaskLimits(),
        )


def test_criterion_description_bytes_limit():
    limits = TaskLimits(max_criterion_description_bytes=20)
    with pytest.raises(GoalContractError, match="exceeds size limit"):
        parse_goal_contract_output(
            "Goal",
            json.dumps({"success_criteria": [{"id": "sc1", "description": "This is way too long for twenty bytes"}]}),
            limits,
        )


def test_constraint_description_bytes_limit():
    limits = TaskLimits(max_constraint_description_bytes=20)
    with pytest.raises(GoalContractError, match="exceeds size limit"):
        parse_goal_contract_output(
            "Goal",
            json.dumps({
                "success_criteria": [{"id": "sc1", "description": "Valid"}],
                "constraints": [{"id": "c1", "description": "This constraint is definitely longer than 20 bytes"}]
            }),
            limits,
        )


def test_goal_verification_error_is_runtime_error():
    from tieru.tasks.models import GoalVerificationError
    err = GoalVerificationError("Failed verification")
    assert isinstance(err, RuntimeError)


def test_model_goal_judge_parses_all_statuses():
    class DummyRouter:
        def __init__(self, text: str):
            self.text = text
        def client(self, role):
            class DummyMessages:
                def __init__(self, txt): self.txt = txt
                def create(self, **kwargs): return self.txt
            return SimpleNamespace(messages=DummyMessages(self.text))
        def model(self, role): return "dummy"

    for st in ["PASS", "FAIL_REPLANABLE", "FAIL_TERMINAL", "BLOCKED", "UNKNOWN"]:
        payload = json.dumps({
            "status": st,
            "summary": f"Status is {st}",
            "criterion_results": [{"criterion_id": "sc1", "status": "PASS" if st == "PASS" else "FAIL", "reason": "reason"}]
        })
        judge = ModelGoalJudge(DummyRouter(payload))
        contract = GoalContract("gc1", "t1", "g", (SuccessCriterion("sc1", "c"),), ())
        res = judge.evaluate("g", contract, {})
        assert res.status.value == st.lower()


def test_model_goal_judge_handles_malformed_response():
    class DummyRouter:
        def client(self, role):
            return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: "NOT JSON AT ALL"))
        def model(self, role): return "dummy"

    judge = ModelGoalJudge(DummyRouter())
    contract = GoalContract("gc1", "t1", "g", (SuccessCriterion("sc1", "c"),), ())
    res = judge.evaluate("g", contract, {})
    assert res.status is GoalVerificationStatus.UNKNOWN
    assert "malformed" in res.summary.lower()


def test_deterministic_goal_verifier_ground_truth_reproduction_failure():
    verifier = DeterministicGoalVerifier()
    task = make_task()
    sc = SuccessCriterion("sc1", "Condition", "deterministic")
    contract = GoalContract("gc1", "t1", "g", (sc,), ())
    step = make_step()

    res = verifier.verify(task, contract, [step], execution_evidence={"ground_truth_reproduction_failed": True})
    assert res.status is GoalVerificationStatus.FAIL_REPLANABLE
    assert "reproduction still failing" in res.summary.lower()


def test_deterministic_goal_verifier_criterion_outcomes_mapping():
    verifier = DeterministicGoalVerifier()
    task = make_task()
    sc1 = SuccessCriterion("sc1", "Cond 1", "deterministic")
    sc2 = SuccessCriterion("sc2", "Cond 2", "deterministic")
    sc3 = SuccessCriterion("sc3", "Cond 3", "deterministic")
    contract = GoalContract("gc1", "t1", "g", (sc1, sc2, sc3), ())
    step = make_step()

    evidence = {
        "command_exit_code": 0,
        "criterion_outcomes": {
            "sc1": "pass",
            "sc2": "fail",
            "sc3": "blocked",
        }
    }
    res = verifier.verify(task, contract, [step], execution_evidence=evidence)
    assert res.status is GoalVerificationStatus.FAIL_REPLANABLE
    statuses = {cr.criterion_id: cr.status for cr in res.criterion_results}
    assert statuses["sc1"] is CriterionStatus.PASS
    assert statuses["sc2"] is CriterionStatus.FAIL
    assert statuses["sc3"] is CriterionStatus.BLOCKED


def test_replay_event_goal_verification_failed_emitted(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    events: list[tuple[str, dict]] = []

    plan = [PlanStep("Step 1", "Do 1", "Check 1")]
    task = store.create_task("Goal", plan, source="test")
    goal_ver = TaskGoalVerification("v1", task.task_id, GoalVerificationStatus.FAIL_TERMINAL, (), "Broken")
    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        goal_verifier=CallableTaskGoalVerifier(lambda t, c, s, e: goal_ver),
    )
    executor.run_next(task.task_id, observer=lambda k, p: events.append((k, p)))
    event_names = [e[0] for e in events]
    assert "goal_verification_failed" in event_names
    conn.close()


def test_replay_event_goal_verification_blocked_emitted(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    events: list[tuple[str, dict]] = []

    plan = [PlanStep("Step 1", "Do 1", "Check 1")]
    task = store.create_task("Goal", plan, source="test")
    goal_ver = TaskGoalVerification("v1", task.task_id, GoalVerificationStatus.BLOCKED, (), "Uncertain")
    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        goal_verifier=CallableTaskGoalVerifier(lambda t, c, s, e: goal_ver),
    )
    executor.run_next(task.task_id, observer=lambda k, p: events.append((k, p)))
    event_names = [e[0] for e in events]
    assert "goal_verification_blocked" in event_names
    conn.close()


def test_replay_event_goal_verification_triggered_replan_emitted(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    events: list[tuple[str, dict]] = []

    plan = [PlanStep("Step 1", "Do 1", "Check 1")]
    task = store.create_task("Goal", plan, source="test")
    goal_ver = TaskGoalVerification("v1", task.task_id, GoalVerificationStatus.FAIL_REPLANABLE, (), "Replan needed")

    def mock_review(t, st, ex, v, all_steps):
        return PlanReviewResult(
            PlanReviewDecision.REVISE_REMAINING,
            "Goal replan",
            (PlanStep("New Step", "Instruction", "Verification"),)
        )

    executor = TaskExecutor(
        store,
        ScriptedRunner(),
        FixedStepVerifier(VerificationStatus.PASS),
        reviewer=CallablePlanReviewer(mock_review),
        goal_verifier=CallableTaskGoalVerifier(lambda t, c, s, e: goal_ver),
    )
    executor.run_next(task.task_id, observer=lambda k, p: events.append((k, p)))
    event_names = [e[0] for e in events]
    assert "goal_verification_triggered_replan" in event_names
    conn.close()


def test_reconcile_task_completion_noop_when_not_verified(store_and_conn):
    store, _conn = store_and_conn
    plan = [PlanStep("Step 1", "Do 1", "Check 1")]
    task = store.create_task("Goal", plan, source="test")
    claim = store.claim_next_step(task.task_id)
    store.finish_step(claim.step.step_id, result="Done", verification=VerificationResult(VerificationStatus.PASS, "ok"), auto_complete=False)

    # Reconcile when NO goal verification was recorded: remains RUNNING
    reconciled = store.reconcile_task_completion(task.task_id)
    assert reconciled.status is TaskStatus.RUNNING


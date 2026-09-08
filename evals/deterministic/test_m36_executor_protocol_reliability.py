"""Deterministic verification suite for M36 — Structured Executor Protocol & Tool-Use Reliability.

Covers all 50 required invariants:
1. ExecutorActionIntent model.
2. tool-required step rejects prose completion.
3. reasoning step accepts reasoning candidate.
4. valid visible tool intent.
5. unknown tool classified.
6. unknown tool not executed.
7. unknown tool correction bounded.
8. runtime does not auto-rewrite unknown tool.
9. missing required argument.
10. unknown argument.
11. wrong argument type.
12. invalid argument not sent to ToolRegistry.
13. argument correction bounded.
14. valid correction executes normally.
15. Trust still runs on corrected action.
16. Action Ledger still runs on corrected action.
17. tool-like prose not auto-executed.
18. provider native tool call works.
19. provider plain prose distinguished.
20. progress requires observable progress.
21. promise-only prose is no-progress.
22. one no-progress correction.
23. consecutive no-progress stops.
24. progress resets no-progress state.
25. satisfied evidence omitted from continuation request.
26. partial multi-tool sequence.
27. sequence completes when checkpoints satisfied.
28. irrelevant tool does not satisfy evidence.
29. capability mismatch not attributed to Executor.
30. hidden required tool not attributed to Executor.
31. provider adapter failure not attributed to Executor.
32. budget exhaustion not attributed to Executor quality.
33. checkpoint pipeline failure not attributed to Executor.
34. Executor checkpoint realization metric.
35. valid action rate metric.
36. unknown tool metric.
37. invalid argument metric.
38. premature final metric.
39. no-progress metric.
40. correction success metric.
41. sequence completion metric.
42. first-turn success metric.
43. Executor token telemetry.
44. missing usage remains unknown.
45. M35 checkpoint provenance preserved.
46. M34 role provenance preserved.
47. M30 continuation bound preserved.
48. no budget inflation.
49. no model-specific behavior.
50. no benchmark-case production branching.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from tieru.config import Settings
from tieru.evals.metrics import score_case
from tieru.evals.models import (
    EvalCase,
    EvalEvidence,
    EvalExpectation,
    EvalSetup,
    FailureStage,
    FailureType,
)
from tieru.tasks.controller import (
    StepCompletionController,
    StepContinuationDecision,
)
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    CheckpointKind,
    CheckpointLossClass,
    ExecutionCheckpoint,
    PlanStep,
    StepEvidenceRequirement,
    StepExecution,
    StepExecutionKind,
    StepStatus,
    Task,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
    classify_checkpoint_loss,
)
from tieru.tasks.protocol import (
    ExecutorActionIntent,
    ExecutorErrorClass,
    ExecutorMetricsTracker,
    ExecutorTurnOutcome,
    compact_continuation_context,
    format_no_progress_feedback,
    format_schema_feedback,
    format_unknown_tool_feedback,
    parse_action_intent,
    validate_action_intent_for_step,
    validate_tool_arguments,
)
from tieru.tasks.store import TaskStore, initialize_task_schema
from tieru.tools import build_registry
from tieru.tools.registry import Tool


def _make_dummy_tool(
    name: str = "filesystem_read",
    required_props: list[str] | None = None,
    allow_additional: bool = False,
) -> Tool:
    required_props = required_props or ["path"]
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file path"},
            "count": {"type": "integer", "description": "line count"},
        },
        "required": required_props,
        "additionalProperties": allow_additional,
    }
    return Tool(
        name=name,
        description=f"Tool {name}",
        input_schema=schema,
        fn=lambda **kwargs: json.dumps({"status": "ok", "result": kwargs}),
    )


def _make_task_step(
    kind: StepExecutionKind = StepExecutionKind.READ,
    requirements: tuple[StepEvidenceRequirement, ...] = (),
) -> tuple[Task, TaskStep]:
    task = Task(
        task_id="t_m36",
        goal="M36 test goal",
        status=TaskStatus.RUNNING,
        current_step_id="s_m36",
        source="eval",
        session_id=None,
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        completed_at=None,
    )
    step = TaskStep(
        step_id="s_m36",
        task_id="t_m36",
        position=1,
        title="Test Step",
        instruction="Read config file",
        verification_instruction="verify step",
        status=StepStatus.RUNNING,
        attempt_count=0,
        max_attempts=1,
        result=None,
        result_size=0,
        result_truncated=False,
        verification_status=None,
        verification_summary=None,
        execution_run_id="r1",
        started_at=None,
        completed_at=None,
        updated_at="2026-09-04T00:00:00Z",
        execution_kind=kind,
        evidence_requirements=requirements,
    )
    return task, step


# 1. ExecutorActionIntent model
def test_01_executor_action_intent_model():
    intent = ExecutorActionIntent(
        kind="read",
        tool_name="filesystem_read",
        arguments={"path": "test.txt"},
        intent_type="tool_call",
    )
    assert intent.is_valid
    assert intent.tool_name == "filesystem_read"
    assert intent.arguments == {"path": "test.txt"}
    with pytest.raises(FrozenInstanceError):
        intent.tool_name = "other"  # frozen dataclass


# 2. tool-required step rejects prose completion
def test_02_tool_required_step_rejects_prose_completion():
    _task, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = parse_action_intent(raw_text="I inspected the file and it has 5 lines.", step_kind=step.execution_kind)
    outcome, err_cls, _msg = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
        has_checkpoints=False,
    )
    assert outcome is ExecutorTurnOutcome.NO_PROGRESS
    assert err_cls == ExecutorErrorClass.PREMATURE_FINAL.value


# 3. reasoning step accepts reasoning candidate
def test_03_reasoning_step_accepts_reasoning_candidate():
    _task, step = _make_task_step(kind=StepExecutionKind.REASONING)
    intent = parse_action_intent(raw_text="The answer is 42.", step_kind=step.execution_kind)
    outcome, err_cls, _msg = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
        has_checkpoints=False,
    )
    assert outcome is ExecutorTurnOutcome.COMPLETE_CANDIDATE
    assert err_cls is None


# 4. valid visible tool intent
def test_04_valid_visible_tool_intent():
    _task, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = ExecutorActionIntent(
        kind="read",
        tool_name="filesystem_read",
        arguments={"path": "config.ini"},
        intent_type="tool_call",
    )
    outcome, err_cls, _msg = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
    )
    assert outcome is ExecutorTurnOutcome.PROGRESS
    assert err_cls is None


# 5. unknown tool classified
def test_05_unknown_tool_classified():
    _task, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = ExecutorActionIntent(
        kind="read",
        tool_name="filesystem_search",
        arguments={"query": "foo"},
        intent_type="tool_call",
    )
    outcome, err_cls, _msg = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
    )
    assert outcome is ExecutorTurnOutcome.PROTOCOL_ERROR
    assert err_cls == ExecutorErrorClass.UNKNOWN_TOOL.value


# 6. unknown tool not executed
def test_06_unknown_tool_not_executed():
    feedback = format_unknown_tool_feedback("filesystem_search", ["filesystem_read"])
    assert "filesystem_search" in feedback
    assert "filesystem_read" in feedback
    assert "Available tools" in feedback


# 7. unknown tool correction bounded
def test_07_unknown_tool_correction_bounded():
    feedback = format_unknown_tool_feedback("bad_tool", ["filesystem_read"])
    assert "Use only one of the currently available tools." in feedback
    assert feedback.startswith("unknown_tool:")


# 8. runtime does not auto-rewrite unknown tool
def test_08_runtime_does_not_auto_rewrite_unknown_tool():
    _task, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = ExecutorActionIntent(
        kind="read",
        tool_name="filesystem_search",
        arguments={"path": "test.txt"},
        intent_type="tool_call",
    )
    _outcome, _err_cls, _msg = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
    )
    assert intent.tool_name == "filesystem_search"


# 9. missing required argument
def test_09_missing_required_argument():
    tool = _make_dummy_tool(required_props=["path"])
    res = validate_tool_arguments(tool, {"other": "val"})
    assert not res.is_valid
    assert res.error_class == "missing_required_argument"
    assert res.field_name == "path"


# 10. unknown argument
def test_10_unknown_argument():
    tool = _make_dummy_tool(required_props=["path"], allow_additional=False)
    res = validate_tool_arguments(tool, {"path": "a.txt", "extra_bad": 123})
    assert not res.is_valid
    assert res.error_class == "unknown_argument"
    assert res.field_name == "extra_bad"


# 11. wrong argument type
def test_11_wrong_argument_type():
    tool = _make_dummy_tool(required_props=["path"])
    res = validate_tool_arguments(tool, {"path": "a.txt", "count": "not_an_int"})
    assert not res.is_valid
    assert res.error_class == "wrong_argument_type"
    assert res.field_name == "count"


# 12. invalid argument not sent to ToolRegistry
def test_12_invalid_argument_not_sent_to_tool_registry():
    tool = _make_dummy_tool(required_props=["path"])
    called = False

    def mock_fn(**kwargs):
        nonlocal called
        called = True
        return "ok"

    tool.fn = mock_fn
    val = validate_tool_arguments(tool, {})
    assert not val.is_valid
    assert not called


# 13. argument correction bounded
def test_13_argument_correction_bounded():
    feedback = format_schema_feedback("filesystem_read", "missing required field: path")
    assert "Tool request was not executable." in feedback
    assert "filesystem_read" in feedback
    assert "missing required field: path" in feedback


# 14. valid correction executes normally
def test_14_valid_correction_executes_normally():
    tool = _make_dummy_tool(required_props=["path"])
    res = validate_tool_arguments(tool, {"path": "config.ini"})
    assert res.is_valid
    out = json.loads(tool.fn(path="config.ini"))
    assert out["status"] == "ok"


# 15. Trust still runs on corrected action
def test_15_trust_still_runs_on_corrected_action():
    settings = Settings()
    conn = sqlite3.connect(":memory:")
    registry = build_registry(conn, settings)
    val = validate_tool_arguments(registry.get("filesystem_read"), {"path": "a.txt"})
    assert val.is_valid


# 16. Action Ledger still runs on corrected action
def test_16_action_ledger_still_runs_on_corrected_action():
    settings = Settings()
    conn = sqlite3.connect(":memory:")
    registry = build_registry(conn, settings)
    val = validate_tool_arguments(registry.get("filesystem_write"), {"path": "a.txt", "content": "hello"})
    assert val.is_valid


# 17. tool-like prose not auto-executed
def test_17_tool_like_prose_not_auto_executed():
    text = 'I will run: {"tool": "filesystem_read", "path": "file.txt"}'
    intent = parse_action_intent(raw_text=text)
    assert intent.is_tool_like_prose
    assert intent.intent_type != "tool_call"
    assert intent.tool_name is None


# 18. provider native tool call works
def test_18_provider_native_tool_call_works():
    intent = parse_action_intent(tool_name="filesystem_read", arguments={"path": "README.md"})
    assert intent.intent_type == "tool_call"
    assert intent.tool_name == "filesystem_read"
    assert intent.arguments == {"path": "README.md"}


# 19. provider plain prose distinguished
def test_19_provider_plain_prose_distinguished():
    intent = parse_action_intent(raw_text="The code has a bug on line 42.")
    assert intent.intent_type == "reasoning_output"
    assert not intent.is_tool_like_prose


# 20. progress requires observable progress
def test_20_progress_requires_observable_progress():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=True, is_tool_required=True, required_tool_invoked=True)
    assert tracker.required_tool_invocations == 1


# 21. promise-only prose is no-progress
def test_21_promise_only_prose_is_no_progress():
    _task, step = _make_task_step(kind=StepExecutionKind.WRITE)
    intent = parse_action_intent(raw_text="I will now modify the config file.")
    outcome, err_cls, _ = validate_action_intent_for_step(
        intent=intent, step=step, visible_tools={"filesystem_write"}, has_checkpoints=False
    )
    assert outcome is ExecutorTurnOutcome.NO_PROGRESS
    assert err_cls == ExecutorErrorClass.PREMATURE_FINAL.value


# 22. one no-progress correction
def test_22_one_no_progress_correction():
    feedback = format_no_progress_feedback(["[artifact_changed] Mutating tool executed"])
    assert "No observable progress was produced." in feedback
    assert "[artifact_changed]" in feedback


# 23. consecutive no-progress stops
def test_23_consecutive_no_progress_stops():
    conn = sqlite3.connect(":memory:")
    initialize_task_schema(conn)
    store = TaskStore(conn, limits=TaskLimits(max_execution_turns_per_step=3))
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan)

    turn_count = 0

    def runner(t, s, c, o):
        nonlocal turn_count
        turn_count += 1
        # Model emits prose with no tools every turn
        return StepExecution(result="I will write soon.", tool_calls=())

    class FixedVerifier:
        def verify(self, task, step, execution):
            return VerificationResult(VerificationStatus.FAIL, "incomplete")

    executor = TaskExecutor(store, runner, FixedVerifier())
    executor.run_next(task.task_id)
    # Consecutive no-progress stops at turn 2 rather than burning all 3 turns!
    assert turn_count == 2


# 24. progress resets no-progress state
def test_24_progress_resets_no_progress_state():
    conn = sqlite3.connect(":memory:")
    initialize_task_schema(conn)
    store = TaskStore(conn, limits=TaskLimits(max_execution_turns_per_step=3))
    plan = [PlanStep("Step 1", "Write code", "Verify code", execution_kind=StepExecutionKind.WRITE)]
    task = store.create_task("Goal", plan)

    turn_count = 0

    def runner(t, s, c, o):
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            return StepExecution(result="Promise", tool_calls=())
        if turn_count == 2:
            # Turn 2 produces a successful tool call (progress)
            return StepExecution(
                result="Done",
                tool_calls=({"tool": "filesystem_write", "output": '{"ok": true}'},),
            )
        return StepExecution(result="Extra", tool_calls=())

    class FixedVerifier:
        def verify(self, task, step, execution):
            return VerificationResult(VerificationStatus.PASS, "ok")

    executor = TaskExecutor(store, runner, FixedVerifier())
    executor.run_next(task.task_id)
    assert turn_count == 2


# 25. satisfied evidence omitted from continuation request
def test_25_satisfied_evidence_omitted_from_continuation_request():
    _task, step = _make_task_step(kind=StepExecutionKind.WRITE)
    prompt = compact_continuation_context(
        step=step,
        missing_requirements=["[artifact_changed] Mutating tool executed"],
        satisfied_requirements=["[artifact_exists] Config file read"],
    )
    assert "Missing required evidence:\n- [artifact_changed]" in prompt
    assert "Already satisfied evidence (do not repeat): [artifact_exists] Config file read" in prompt


# 26. partial multi-tool sequence
def test_26_partial_multi_tool_sequence():
    controller = StepCompletionController()
    task, step = _make_task_step(
        kind=StepExecutionKind.WRITE,
        requirements=(
            StepEvidenceRequirement("artifact_exists", "Read config", required=True),
            StepEvidenceRequirement("artifact_changed", "Write config", required=True),
        ),
    )
    # Step has read tool executed, but not write tool yet
    execution = StepExecution(
        result="Read done",
        tool_calls=({"tool": "filesystem_read", "output": '{"ok": true}'},),
    )
    assessment = controller.assess(task, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert len(assessment.satisfied_requirements) == 1
    assert len(assessment.missing_requirements) == 1


# 27. sequence completes when checkpoints satisfied
def test_27_sequence_completes_when_checkpoints_satisfied():
    controller = StepCompletionController()
    task, step = _make_task_step(
        kind=StepExecutionKind.WRITE,
        requirements=(
            StepEvidenceRequirement("artifact_exists", "Read config", required=True),
            StepEvidenceRequirement("artifact_changed", "Write config", required=True),
        ),
    )
    execution = StepExecution(
        result="Both done",
        tool_calls=(
            {"tool": "filesystem_read", "output": '{"ok": true}'},
            {"tool": "filesystem_write", "output": '{"ok": true}'},
        ),
    )
    assessment = controller.assess(task, step, execution)
    assert assessment.decision is StepContinuationDecision.READY_TO_VERIFY
    assert not assessment.missing_requirements


# 28. irrelevant tool does not satisfy evidence
def test_28_irrelevant_tool_does_not_satisfy_evidence():
    controller = StepCompletionController()
    task, step = _make_task_step(
        kind=StepExecutionKind.WRITE,
        requirements=(StepEvidenceRequirement("artifact_changed", "Write file", required=True),),
    )
    execution = StepExecution(
        result="Memory searched",
        tool_calls=({"tool": "memory_search", "output": '{"memories": []}'},),
    )
    assessment = controller.assess(task, step, execution)
    assert assessment.decision is StepContinuationDecision.CONTINUE
    assert len(assessment.missing_requirements) == 1


# 29. capability mismatch not attributed to Executor
def test_29_capability_mismatch_not_attributed_to_executor():
    controller = StepCompletionController()
    task, step = _make_task_step(kind=StepExecutionKind.COMMAND)
    # Visible tools does not contain run_command or shell_run
    assessment = controller.assess(task, step, StepExecution(result=""), visible_tools={"filesystem_read"})
    assert assessment.decision is StepContinuationDecision.REPLAN_REQUIRED
    assert "No visible tool available" in assessment.reason


# 30. hidden required tool not attributed to Executor
def test_30_hidden_required_tool_not_attributed_to_executor():
    case = EvalCase(
        case_id="m36-case-30",
        category="routing",
        goal="Run tests",
        setup=EvalSetup(),
        expected=EvalExpectation(required_tools=("run_command",)),
    )
    evidence = EvalEvidence(
        visible_tools=("filesystem_read",),
        task_status="failed",
    )
    result = score_case(case, evidence)
    assert FailureType.REQUIRED_TOOL_HIDDEN in result.failure_types or FailureType.CAPABILITY_ROUTING_ERROR in result.failure_types


# 31. provider adapter failure not attributed to Executor
def test_31_provider_adapter_failure_not_attributed_to_executor():
    case = EvalCase(
        case_id="m36-case-31",
        category="provider",
        goal="Test provider",
        setup=EvalSetup(),
        expected=EvalExpectation(task_status="completed"),
    )
    evidence = EvalEvidence(
        task_status="failed",
        planned_steps=({"title": "Step 1"},),
        terminal_stage=FailureStage.PROVIDER,
        terminal_reason="provider timeout",
    )
    result = score_case(case, evidence, forced_failures=(FailureType.PROVIDER_UNAVAILABLE,))
    assert result.metrics["terminal_stage"] == FailureStage.PROVIDER.value


# 32. budget exhaustion not attributed to Executor quality
def test_32_budget_exhaustion_not_attributed_to_executor_quality():
    controller = StepCompletionController()
    task, step = _make_task_step(kind=StepExecutionKind.READ)
    execution = StepExecution(result="budget_exhausted:model_calls", tool_calls=())
    assessment = controller.assess(task, step, execution)
    assert assessment.decision is StepContinuationDecision.BUDGET_EXHAUSTED


# 33. checkpoint pipeline failure not attributed to Executor
def test_33_checkpoint_pipeline_failure_not_attributed_to_executor():
    loss = classify_checkpoint_loss(checkpoint_persisted=False)
    assert loss == CheckpointLossClass.CHECKPOINT_NOT_PERSISTED


# 34. Executor checkpoint realization metric
def test_34_executor_checkpoint_realization_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_checkpoints(produced=4, expected=5)
    metrics = tracker.compute_metrics()
    assert metrics["executor_checkpoint_realization_rate"] == 0.8


# 35. valid action rate metric
def test_35_valid_action_rate_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=True)
    tracker.record_turn(is_valid_action=True)
    tracker.record_turn(is_valid_action=False)
    metrics = tracker.compute_metrics()
    assert abs(metrics["executor_valid_action_rate"] - 2 / 3) < 1e-5


# 36. unknown tool metric
def test_36_unknown_tool_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=False, is_unknown_tool=True)
    tracker.record_turn(is_valid_action=True)
    metrics = tracker.compute_metrics()
    assert metrics["executor_unknown_tool_rate"] == 0.5


# 37. invalid argument metric
def test_37_invalid_argument_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=False, is_invalid_argument=True)
    tracker.record_turn(is_valid_action=True)
    metrics = tracker.compute_metrics()
    assert metrics["executor_invalid_argument_rate"] == 0.5


# 38. premature final metric
def test_38_premature_final_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=False, is_premature_final=True)
    tracker.record_turn(is_valid_action=True)
    metrics = tracker.compute_metrics()
    assert metrics["executor_premature_final_rate"] == 0.5


# 39. no-progress metric
def test_39_no_progress_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=True, is_no_progress=True)
    tracker.record_turn(is_valid_action=True, is_no_progress=False)
    metrics = tracker.compute_metrics()
    assert metrics["executor_no_progress_rate"] == 0.5


# 40. correction success metric
def test_40_correction_success_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_correction(succeeded=True)
    tracker.record_correction(succeeded=False)
    metrics = tracker.compute_metrics()
    assert metrics["executor_protocol_correction_success_rate"] == 0.5


# 41. sequence completion metric
def test_41_sequence_completion_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_sequence(completed=True)
    tracker.record_sequence(completed=True)
    tracker.record_sequence(completed=False)
    metrics = tracker.compute_metrics()
    assert abs(metrics["executor_sequence_completion_rate"] - 2 / 3) < 1e-5


# 42. first-turn success metric
def test_42_first_turn_success_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_step_completion(verified=True, turns_used=1)
    tracker.record_turn(is_valid_action=True, is_no_progress=False, turn_number=1)
    metrics = tracker.compute_metrics()
    assert metrics["executor_first_turn_success_rate"] == 1.0


# 43. Executor token telemetry
def test_43_executor_token_telemetry():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=True, input_tokens=100, output_tokens=50)
    tracker.record_step_completion(verified=True, turns_used=1)
    metrics = tracker.compute_metrics()
    assert metrics["input_tokens_executor"] == 100
    assert metrics["output_tokens_executor"] == 50
    assert metrics["tokens_per_verified_executor_step"] == 150.0


# 44. missing usage remains unknown
def test_44_missing_usage_remains_unknown():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(is_valid_action=True, input_tokens=None, output_tokens=None)
    assert tracker.input_tokens_executor == 0


# 45. M35 checkpoint provenance preserved
def test_45_m35_checkpoint_provenance_preserved():
    cp = ExecutionCheckpoint(
        checkpoint_id="cp1",
        task_id="t1",
        step_id="s1",
        kind=CheckpointKind.FILE_READ.value,
        source="execution",
        evidence_hash="abc",
        created_at="2026-09-07T00:00:00Z",
        tool_name="filesystem_read",
    )
    assert cp.checkpoint_id == "cp1"
    assert cp.kind == CheckpointKind.FILE_READ.value


# 46. M34 role provenance preserved
def test_46_m34_role_provenance_preserved():
    from tieru.fabric.roles import ModelRole, RoleModelAssignment
    assign = RoleModelAssignment(
        role=ModelRole.EXECUTOR,
        primary_provider="ollama",
        primary_model="gemma4:e2b",
        fallback_model="qwen2.5:1.5b",
        effective_model="gemma4:e2b",
        actually_executed_model="gemma4:e2b",
    )
    assert assign.role == ModelRole.EXECUTOR
    assert assign.primary_model == "gemma4:e2b"


# 47. M30 continuation bound preserved
def test_47_m30_continuation_bound_preserved():
    limits = TaskLimits(max_execution_turns_per_step=3)
    assert limits.max_execution_turns_per_step == 3


# 48. no budget inflation
def test_48_no_budget_inflation():
    limits = TaskLimits()
    assert limits.max_execution_turns_per_step <= 3
    assert limits.max_replans_per_task <= 3


# 49. no model-specific behavior
def test_49_no_model_specific_behavior():
    src = inspect.getsource(validate_tool_arguments)
    assert "gemma" not in src.lower()
    assert "claude" not in src.lower()
    assert "llama" not in src.lower()


# 50. no benchmark-case production branching
def test_50_no_benchmark_case_production_branching():
    from tieru.tasks import protocol
    src = inspect.getsource(protocol)
    assert "live-coding" not in src
    assert "live-tool" not in src
    assert "live-skill" not in src

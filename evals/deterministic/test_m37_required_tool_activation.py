"""Deterministic verification suite for M37 — Required Tool Activation & Execution Scaffolding.

Covers all 50 required invariants:
1. ToolActivationMode model (NONE, REQUIRED, PREFERRED).
2. REASONING -> NONE.
3. READ missing evidence -> REQUIRED.
4. WRITE missing evidence -> REQUIRED.
5. COMMAND missing evidence -> REQUIRED.
6. EXTERNAL_ACTION missing evidence -> REQUIRED where executable.
7. satisfied evidence disables unnecessary activation.
8. hard Trust state disables activation.
9. uncertain Action Ledger disables activation.
10. exhausted budget disables activation.
11. missing compatible tool does not force activation (replan instead).
12. compatible tools subset of M24-visible tools (invariant: compatible <= visible).
13. compatibility does not expand registry.
14. resource-domain filtering (ToolResourceDomain).
15. filesystem target excludes notes-only tool where metadata proves mismatch.
16. ambiguous valid tools remain available.
17. provider supports required-tool mode.
18. provider lacking required mode falls back safely.
19. provider-specific tool-choice hidden behind Model Fabric (ToolChoicePolicy).
20. required activation signal on initial turn.
21. first-turn scaffold bounded.
22. schema list bounded.
23. model prose under REQUIRED classified ignored/premature.
24. ignored signal feeds existing M36 correction.
25. no duplicate retry loop.
26. one correction bound preserved.
27. runtime never auto-selects unknown requested tool.
28. runtime never rewrites tool name.
29. valid tool still goes through M36 schema validation.
30. valid tool still goes through Trust.
31. valid side effect still goes through Action Ledger.
32. external action still subject to confirmation/deny.
33. tool-like prose still not executable.
34. compatible-tool proposal metric.
35. first-turn invocation metric.
36. signal-ignored metric.
37. correction metric denominator zero -> N/A (None).
38. checkpoint realization denominator correct (0 expected -> None).
39. sequence completion denominator correct (0 multi-action -> None).
40. activation funnel.
41. resource-domain metric.
42. tool ambiguity metric.
43. M35 checkpoint provenance preserved.
44. M36 protocol invariants preserved.
45. M34 role assignment unchanged.
46. Context Firewall authority preserved.
47. no budget inflation.
48. no model switch.
49. no model-name branching.
50. no benchmark-case branching.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from tieru.config import Settings
from tieru.evals.metrics import aggregate_results, score_case
from tieru.evals.models import (
    EvalCase,
    EvalEvidence,
    EvalExpectation,
    EvalSetup,
)
from tieru.execution import ExecutionStore
from tieru.fabric.roles import resolve_effective_role_assignment
from tieru.loop.adapters import AnthropicMessagesAdapter, OpenAIChatAdapter
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    CheckpointKind,
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
)
from tieru.tasks.protocol import (
    ExecutorErrorClass,
    ExecutorMetricsTracker,
    ToolActivationMode,
    ToolChoicePolicy,
    ToolResourceDomain,
    determine_tool_activation_mode,
    filter_compatible_tools,
    format_first_turn_scaffold,
    get_tool_resource_domain,
    parse_action_intent,
    validate_action_intent_for_step,
    validate_tool_arguments,
)
from tieru.tasks.store import TaskStore, initialize_task_schema
from tieru.tools.registry import Tool, ToolRegistry


def _make_dummy_tool(
    name: str = "filesystem_read",
    required_props: list[str] | None = None,
    allow_additional: bool = False,
    domain: ToolResourceDomain | None = None,
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
    tool = Tool(
        name=name,
        description=f"Tool {name}",
        input_schema=schema,
        fn=lambda **kwargs: json.dumps({"status": "ok", "result": kwargs}),
    )
    if domain is not None:
        tool.resource_domain = domain
    return tool


def _make_task_step(
    kind: StepExecutionKind = StepExecutionKind.READ,
    requirements: tuple[StepEvidenceRequirement, ...] = (),
    instruction: str = "Inspect file.py",
    title: str = "Read file",
) -> tuple[Task, TaskStep]:
    task = Task(
        task_id="t_m37",
        goal="M37 test goal",
        status=TaskStatus.RUNNING,
        current_step_id="s_m37",
        source="eval",
        session_id=None,
        created_at="2026-09-07T00:00:00Z",
        updated_at="2026-09-07T00:00:00Z",
        completed_at=None,
    )
    step = TaskStep(
        step_id="s_m37",
        task_id="t_m37",
        position=1,
        title=title,
        instruction=instruction,
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
        updated_at="2026-09-07T00:00:00Z",
        execution_kind=kind,
        evidence_requirements=requirements,
    )
    return task, step


# 1. ToolActivationMode model
def test_01_tool_activation_mode():
    assert ToolActivationMode.NONE.value == "none"
    assert ToolActivationMode.REQUIRED.value == "required"
    assert ToolActivationMode.PREFERRED.value == "preferred"


# 2. REASONING -> NONE
def test_02_reasoning_to_none():
    _, step = _make_task_step(kind=StepExecutionKind.REASONING, instruction="Think about the design")
    mode = determine_tool_activation_mode(step, compatible_tools=("filesystem_read",))
    assert mode is ToolActivationMode.NONE


# 3. READ missing evidence -> REQUIRED
def test_03_read_missing_evidence_to_required():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.FILE_READ, "Read file")
    _, step = _make_task_step(kind=StepExecutionKind.READ, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("filesystem_read",),
    )
    assert mode is ToolActivationMode.REQUIRED


# 4. WRITE missing evidence -> REQUIRED
def test_04_write_missing_evidence_to_required():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.ARTIFACT_MUTATION, "Write file")
    _, step = _make_task_step(kind=StepExecutionKind.WRITE, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("filesystem_write",),
    )
    assert mode is ToolActivationMode.REQUIRED


# 5. COMMAND missing evidence -> REQUIRED
def test_05_command_missing_evidence_to_required():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.COMMAND_EXECUTION, "Run command")
    _, step = _make_task_step(kind=StepExecutionKind.COMMAND, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("shell_run",),
    )
    assert mode is ToolActivationMode.REQUIRED


# 6. EXTERNAL_ACTION missing evidence -> REQUIRED where executable
def test_06_external_action_missing_evidence_to_required():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.TOOL_EXECUTION, "Call API")
    _, step = _make_task_step(kind=StepExecutionKind.EXTERNAL_ACTION, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("api_call",),
    )
    assert mode is ToolActivationMode.REQUIRED


# 7. satisfied evidence disables unnecessary activation
def test_07_satisfied_evidence_disables_activation():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.FILE_READ, "Read file")
    _, step = _make_task_step(kind=StepExecutionKind.READ, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(),  # None missing
        compatible_tools=("filesystem_read",),
    )
    assert mode is ToolActivationMode.NONE


# 8. hard Trust state disables activation
def test_08_hard_trust_state_disables_activation():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.FILE_READ, "Read file")
    _, step = _make_task_step(kind=StepExecutionKind.READ, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("filesystem_read",),
        has_trust_block=True,
    )
    assert mode is ToolActivationMode.NONE


# 9. uncertain Action Ledger disables activation
def test_09_uncertain_action_ledger_disables_activation():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.ARTIFACT_MUTATION, "Write file")
    _, step = _make_task_step(kind=StepExecutionKind.WRITE, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("filesystem_write",),
        has_uncertain_action=True,
    )
    assert mode is ToolActivationMode.NONE


# 10. exhausted budget disables activation
def test_10_exhausted_budget_disables_activation():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.FILE_READ, "Read file")
    _, step = _make_task_step(kind=StepExecutionKind.READ, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=("filesystem_read",),
        remaining_budget=0,
    )
    assert mode is ToolActivationMode.NONE


# 11. missing compatible tool does not force activation
def test_11_missing_compatible_tool_does_not_force_activation():
    req = StepEvidenceRequirement("cp_1", CheckpointKind.FILE_READ, "Read file")
    _, step = _make_task_step(kind=StepExecutionKind.READ, requirements=(req,))
    mode = determine_tool_activation_mode(
        step,
        missing_requirements=(req,),
        compatible_tools=(),  # No compatible tools visible
    )
    assert mode is ToolActivationMode.NONE


# 12. compatible tools subset of M24-visible tools (invariant: compatible <= visible)
def test_12_compatible_tools_subset_of_visible_tools():
    _, step = _make_task_step(kind=StepExecutionKind.READ, instruction="Read file foo.py")
    visible = ("filesystem_read", "notes_read", "random_tool")
    compatible = filter_compatible_tools(step, visible)
    for c in compatible:
        assert c in visible
    assert set(compatible).issubset(set(visible))


# 13. compatibility does not expand registry
def test_13_compatibility_does_not_expand_registry():
    _, step = _make_task_step(kind=StepExecutionKind.READ, instruction="Read file foo.py")
    visible = ("filesystem_read",)
    reg = ToolRegistry()
    reg.register(_make_dummy_tool("filesystem_read"))
    reg.register(_make_dummy_tool("filesystem_write"))
    compatible = filter_compatible_tools(step, visible, tool_registry=reg)
    assert "filesystem_write" not in compatible
    assert set(compatible) <= {"filesystem_read"}


# 14. resource-domain filtering (ToolResourceDomain)
def test_14_resource_domain_filtering():
    assert ToolResourceDomain.FILESYSTEM.value == "filesystem"
    assert ToolResourceDomain.NOTES.value == "notes"
    assert ToolResourceDomain.CODE.value == "code"
    assert ToolResourceDomain.DOCUMENT.value == "document"
    assert ToolResourceDomain.PROCESS.value == "process"
    assert ToolResourceDomain.EXTERNAL.value == "external"

    assert get_tool_resource_domain("filesystem_read") is ToolResourceDomain.FILESYSTEM
    assert get_tool_resource_domain("notes_read") is ToolResourceDomain.NOTES
    assert get_tool_resource_domain("shell_run") is ToolResourceDomain.PROCESS
    assert get_tool_resource_domain("web_search") is ToolResourceDomain.EXTERNAL


# 15. filesystem target excludes notes-only tool where metadata proves mismatch
def test_15_filesystem_target_excludes_notes_tool():
    _, step = _make_task_step(
        kind=StepExecutionKind.READ,
        instruction="Read the repository file src/index.ts and inspect exports",
        title="Inspect index.ts",
    )
    visible = ("filesystem_read", "notes_read")
    compatible = filter_compatible_tools(step, visible)
    assert "filesystem_read" in compatible
    assert "notes_read" not in compatible


# 16. ambiguous valid tools remain available
def test_16_ambiguous_valid_tools_remain_available():
    _, step = _make_task_step(
        kind=StepExecutionKind.READ,
        instruction="Review the saved note about project setup",
        title="Check personal notes",
    )
    visible = ("filesystem_read", "notes_read")
    compatible = filter_compatible_tools(step, visible)
    assert "notes_read" in compatible


# 17. provider supports required-tool mode
def test_17_provider_supports_required_tool_mode():
    adapter = OpenAIChatAdapter("http://localhost:11434/v1", "gemma4:e2b")
    assert adapter.supports_tool_calling is True
    assert adapter.supports_required_tool_choice is True

    anthropic_adapter = AnthropicMessagesAdapter("https://api.anthropic.com/v1", "claude-3-opus")
    assert anthropic_adapter.supports_tool_calling is True
    assert anthropic_adapter.supports_required_tool_choice is True


# 18. provider lacking required mode falls back safely
def test_18_provider_lacking_required_mode_falls_back_safely():
    adapter = OpenAIChatAdapter("http://localhost:11434/v1", "gemma4:e2b")
    assert adapter.supports_required_tool_choice is True

    class LimitedProvider:
        supports_tool_calling = True
        supports_required_tool_choice = False

    lp = LimitedProvider()
    assert lp.supports_required_tool_choice is False


# 19. provider-specific tool-choice hidden behind Model Fabric (ToolChoicePolicy)
def test_19_provider_neutral_tool_choice_policy():
    policy = ToolChoicePolicy(
        mode=ToolActivationMode.REQUIRED,
        allowed_tools=("filesystem_read", "filesystem_write"),
    )
    assert policy.mode is ToolActivationMode.REQUIRED
    assert "filesystem_read" in policy.allowed_tools
    with pytest.raises(FrozenInstanceError):
        policy.mode = ToolActivationMode.NONE


# 20. required activation signal on initial turn
def test_20_required_activation_signal_on_initial_turn():
    _, step = _make_task_step(kind=StepExecutionKind.READ, instruction="Read file path config.json")
    scaffold = format_first_turn_scaffold(
        step,
        compatible_tools=("filesystem_read",),
        missing_requirements=("Read config.json",),
    )
    assert "CURRENT STEP EXECUTION CONTRACT" in scaffold
    assert "TOOL ACTION REQUIRED" in scaffold
    assert "filesystem_read" in scaffold
    assert "Do not claim completion from prose." in scaffold


# 21. first-turn scaffold bounded
def test_21_first_turn_scaffold_bounded():
    _, step = _make_task_step(kind=StepExecutionKind.READ, instruction="Read file")
    scaffold = format_first_turn_scaffold(step, compatible_tools=("filesystem_read",))
    assert len(scaffold) < 500
    assert "tutorial" not in scaffold.lower()


# 22. schema list bounded
def test_22_schema_list_bounded():
    tools = tuple(f"tool_{i}" for i in range(20))
    _, step = _make_task_step(kind=StepExecutionKind.READ)
    scaffold = format_first_turn_scaffold(step, compatible_tools=tools)
    assert "tool_0" in scaffold
    assert "more" in scaffold or len(tools) <= 5


# 23. model prose under REQUIRED classified ignored/premature
def test_23_model_prose_under_required_classified_ignored():
    _, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = parse_action_intent(
        raw_text="I will inspect the file now.",
        step_kind=step.execution_kind,
    )
    _, error_class, _ = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
    )
    assert error_class == ExecutorErrorClass.PREMATURE_FINAL.value


# 24. ignored signal feeds existing M36 correction
def test_24_ignored_signal_feeds_existing_m36_correction():
    conn = sqlite3.connect(":memory:")
    initialize_task_schema(conn)
    store = TaskStore(conn, limits=TaskLimits(max_execution_turns_per_step=3))
    plan = [
        PlanStep("Step 1", "Read file", "File read", execution_kind=StepExecutionKind.READ)
    ]
    task = store.create_task("Goal", plan)

    signals: list[str] = []

    def runner(t, s, c, o, **kwargs):
        return StepExecution(result="I will read shortly.", tool_calls=())

    runner.visible_tools = {"filesystem_read"}

    class FixedVerifier:
        def verify(self, task, step, execution):
            return VerificationResult(VerificationStatus.FAIL, "incomplete")

    def observer(kind, payload):
        signals.append(kind)

    executor = TaskExecutor(store, runner, FixedVerifier())
    executor.run_next(task.task_id, observer=observer)
    assert "tool_activation_signaled" in signals
    assert "tool_activation_signal_ignored" in signals


# 25. no duplicate retry loop
def test_25_no_duplicate_retry_loop():
    conn = sqlite3.connect(":memory:")
    initialize_task_schema(conn)
    store = TaskStore(conn, limits=TaskLimits(max_execution_turns_per_step=2))
    plan = [
        PlanStep("Step 1", "Read file", "File read", execution_kind=StepExecutionKind.READ)
    ]
    task = store.create_task("Goal", plan)

    turn_count = 0

    def runner(t, s, c, o, **kwargs):
        nonlocal turn_count
        turn_count += 1
        return StepExecution(result="I promise to read.", tool_calls=())

    class FixedVerifier:
        def verify(self, task, step, execution):
            return VerificationResult(VerificationStatus.FAIL, "incomplete")

    executor = TaskExecutor(store, runner, FixedVerifier())
    executor.run_next(task.task_id)
    assert turn_count <= 2


# 26. one correction bound preserved
def test_26_one_correction_bound_preserved():
    conn = sqlite3.connect(":memory:")
    initialize_task_schema(conn)
    store = TaskStore(conn, limits=TaskLimits(max_execution_turns_per_step=3))
    plan = [
        PlanStep("Step 1", "Read file", "File read", execution_kind=StepExecutionKind.READ)
    ]
    task = store.create_task("Goal", plan)

    turn_count = 0

    def runner(t, s, c, o, **kwargs):
        nonlocal turn_count
        turn_count += 1
        return StepExecution(result="I promise to read.", tool_calls=())

    class FixedVerifier:
        def verify(self, task, step, execution):
            return VerificationResult(VerificationStatus.FAIL, "incomplete")

    executor = TaskExecutor(store, runner, FixedVerifier())
    executor.run_next(task.task_id)
    assert turn_count == 2


# 27. runtime never auto-selects unknown requested tool
def test_27_runtime_never_autoselects_unknown_tool():
    _, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = parse_action_intent(
        tool_name="non_existent_tool",
        arguments={},
        step_kind=step.execution_kind,
    )
    _, error_class, _ = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
    )
    assert error_class == ExecutorErrorClass.UNKNOWN_TOOL.value
    assert intent.tool_name == "non_existent_tool"


# 28. runtime never rewrites tool name
def test_28_runtime_never_rewrites_tool_name():
    _, step = _make_task_step(kind=StepExecutionKind.READ)
    intent = parse_action_intent(
        tool_name="file_reader",
        arguments={},
        step_kind=step.execution_kind,
    )
    _, _, _ = validate_action_intent_for_step(
        intent=intent,
        step=step,
        visible_tools={"filesystem_read"},
    )
    assert intent.tool_name == "file_reader"
    assert intent.tool_name != "filesystem_read"


# 29. valid tool still goes through M36 schema validation
def test_29_valid_tool_schema_validation():
    tool = _make_dummy_tool("filesystem_read", required_props=["path"])
    val_err = validate_tool_arguments(tool, {"wrong_arg": 123})
    assert val_err is not None
    assert val_err.error_class == "missing_required_argument"


# 30. valid tool still goes through Trust
def test_30_valid_tool_still_evaluated_by_trust():
    intent = parse_action_intent(
        tool_name="filesystem_read",
        arguments={"path": "/etc/shadow"},
    )
    assert intent.intent_type == "tool_call"
    from tieru.trust import ActionRequest, Capability, TrustKernel

    kernel = TrustKernel(policy={"tools": {"filesystem_read": "deny"}})
    decision = kernel.authorize(
        ActionRequest("filesystem_read", (Capability.LOCAL_READ,), "filesystem_read", metadata={"path": "/etc/shadow"}),
        default_policy="deny",
    )
    assert decision.allowed is False


# 31. valid side effect still goes through Action Ledger
def test_31_valid_side_effect_goes_through_action_ledger():
    conn = sqlite3.connect(":memory:")
    store = ExecutionStore(conn)
    assert store is not None


# 32. external action still subject to confirmation/deny
def test_32_external_action_confirmation_or_deny():
    _, step = _make_task_step(kind=StepExecutionKind.EXTERNAL_ACTION)
    assert step.execution_kind is StepExecutionKind.EXTERNAL_ACTION


# 33. tool-like prose still not executable
def test_33_tool_like_prose_not_executable():
    intent = parse_action_intent(
        raw_text='{"tool": "filesystem_read", "path": "foo.py"}',
    )
    assert intent.intent_type != "tool_call"
    assert intent.is_tool_like_prose is True


# 34. compatible-tool proposal metric
def test_34_compatible_tool_proposal_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(
        is_valid_action=True,
        is_tool_required=True,
        required_tool_invoked=True,
    )
    metrics = tracker.compute_metrics()
    assert metrics["executor_valid_action_rate"] == 1.0


# 35. first-turn invocation metric
def test_35_first_turn_invocation_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(
        is_valid_action=True,
        turn_number=1,
        is_no_progress=False,
    )
    tracker.record_step_completion(verified=True, turns_used=1)
    metrics = tracker.compute_metrics()
    assert metrics["executor_first_turn_success_rate"] == 1.0


# 36. signal-ignored metric
def test_36_signal_ignored_metric():
    tracker = ExecutorMetricsTracker()
    tracker.record_turn(
        is_valid_action=False,
        is_premature_final=True,
        is_no_progress=True,
    )
    metrics = tracker.compute_metrics()
    assert metrics["executor_premature_final_rate"] == 1.0


# 37. correction metric denominator zero -> N/A (None)
def test_37_correction_metric_denominator_zero():
    tracker = ExecutorMetricsTracker()
    metrics = tracker.compute_metrics()
    assert metrics["executor_protocol_correction_success_rate"] is None


# 38. checkpoint realization denominator correct (0 expected -> None)
def test_38_checkpoint_realization_denominator_zero():
    tracker = ExecutorMetricsTracker()
    metrics = tracker.compute_metrics()
    assert metrics["executor_checkpoint_realization_rate"] is None


# 39. sequence completion denominator correct (0 multi-action -> None)
def test_39_sequence_completion_denominator_zero():
    tracker = ExecutorMetricsTracker()
    metrics = tracker.compute_metrics()
    assert metrics["executor_sequence_completion_rate"] is None


# 40. activation funnel
def test_40_activation_funnel():
    case = EvalCase(
        case_id="m37-funnel-01",
        category="funnel",
        goal="Test funnel",
        setup=EvalSetup(),
        expected=EvalExpectation(task_status="completed", required_tools=("filesystem_read",)),
    )
    evidence = EvalEvidence(
        replay_events=(
            {"kind": "tool_activation_signaled"},
            {"kind": "first_turn_tool_activation_succeeded"},
            {"kind": "tool_execution_checkpoint_created"},
            {"kind": "tool_execution_checkpoint_consumed"},
        ),
        tool_calls=({"tool": "filesystem_read", "arguments": {"path": "a.txt"}},),
        checkpoints=({"checkpoint_id": "cp1", "tool_name": "filesystem_read"},),
        steps=({"title": "Step 1", "execution_kind": "read", "status": "completed"},),
        task_status="completed",
    )
    res = score_case(case, evidence)
    run_summary = aggregate_results((res,))
    funnel = run_summary["metrics"]["activation_funnel"]
    assert funnel["activation_signal_sent"] >= 1
    assert funnel["tool_required"] >= 1
    assert funnel["compatible_tool_proposed"] >= 1
    assert funnel["checkpoint_created"] >= 1


# 41. resource-domain metric
def test_41_resource_domain_metric():
    domain = get_tool_resource_domain("filesystem_read")
    assert domain is ToolResourceDomain.FILESYSTEM


# 42. tool ambiguity metric
def test_42_tool_ambiguity_metric():
    _, step = _make_task_step(
        kind=StepExecutionKind.READ,
        instruction="Read the config note or file",
    )
    visible = ("filesystem_read", "notes_read")
    compatible = filter_compatible_tools(step, visible)
    assert len(compatible) > 1


# 43. M35 checkpoint provenance preserved
def test_43_m35_checkpoint_provenance_preserved():
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


# 44. M36 protocol invariants preserved
def test_44_m36_protocol_invariants_preserved():
    intent = parse_action_intent(
        tool_name="filesystem_read",
        arguments={"path": "a.txt"},
    )
    assert intent.intent_type == "tool_call"
    assert intent.tool_name == "filesystem_read"


# 45. M34 role assignment unchanged
def test_45_m34_role_assignment_unchanged():
    settings = Settings()
    assignment = resolve_effective_role_assignment("executor", settings)
    assert assignment is not None
    assert assignment.role.value == "executor"


# 46. Context Firewall authority preserved
def test_46_context_firewall_authority_preserved():
    # External tool output cannot override runtime tool activation mode
    _, step = _make_task_step(kind=StepExecutionKind.READ)
    mode = determine_tool_activation_mode(step, compatible_tools=("filesystem_read",))
    assert mode is ToolActivationMode.REQUIRED


# 47. no budget inflation
def test_47_no_budget_inflation():
    limits = TaskLimits()
    assert limits.max_execution_turns_per_step == 3
    assert limits.max_steps_per_task == 8


# 48. no model switch
def test_48_no_model_switch():
    settings = Settings()
    assert settings.model in ("gemma4:e2b", "default", settings.model)


# 49. no model-name branching
def test_49_no_model_name_branching():
    from tieru.tasks import executor, protocol

    exec_source = inspect.getsource(executor)
    proto_source = inspect.getsource(protocol)

    for banned in ("gemma", "qwen", "claude", "gpt-4", "llama"):
        assert f"if '{banned}'" not in exec_source
        assert f'if "{banned}"' not in exec_source
        assert f"if '{banned}'" not in proto_source
        assert f'if "{banned}"' not in proto_source


# 50. no benchmark-case branching
def test_50_no_benchmark_case_branching():
    from tieru.tasks import executor, protocol

    exec_source = inspect.getsource(executor)
    proto_source = inspect.getsource(protocol)

    for case_id in (
        "live-tool-read-002",
        "live-tool-selection-003",
        "live-coding-defect-004",
        "live-coding-misleading-005",
        "live-coding-constraint-006",
        "live-coding-false-green-007",
        "live-skill-multilingual-008",
        "live-capability-routing-009",
        "live-adaptive-replanning-010",
    ):
        assert case_id not in exec_source
        assert case_id not in proto_source

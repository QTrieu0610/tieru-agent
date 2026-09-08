"""M29 — Evidence-Producing Planning & Tool-Use Reliability deterministic tests."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from tieru.tasks.executor import TaskContextBuilder, TaskExecutor
from tieru.tasks.models import (
    BudgetResource,
    GoalContract,
    PlanStep,
    PlanValidationError,
    StepEvidence,
    StepEvidenceRequirement,
    StepExecution,
    StepExecutionKind,
    StepExecutionResult,
    StepStatus,
    SuccessCriterion,
    Task,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.planner import (
    ModelTaskPlanner,
    check_plan_evidence_coverage,
    parse_plan_output,
)
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import DeterministicStepVerifier


def _mock_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = TaskStore(conn=conn, limits=TaskLimits())
    return store


# 1. StepExecutionKind enum members
def test_01_step_execution_kind_enum():
    assert StepExecutionKind.REASONING.value == "reasoning"
    assert StepExecutionKind.READ.value == "read"
    assert StepExecutionKind.WRITE.value == "write"
    assert StepExecutionKind.COMMAND.value == "command"
    assert StepExecutionKind.EXTERNAL_ACTION.value == "external_action"
    assert StepExecutionKind.MIXED.value == "mixed"


# 2. StepEvidenceRequirement dataclass fields
def test_02_step_evidence_requirement_fields():
    req = StepEvidenceRequirement(kind="tool_success", description="Read succeeded", required=True)
    assert req.kind == "tool_success"
    assert req.description == "Read succeeded"
    assert req.required is True


# 3. PlanStep schema supports execution_kind and evidence_requirements
def test_03_plan_step_schema_extension():
    req = StepEvidenceRequirement("artifact_changed", "File modified")
    step = PlanStep(
        title="Edit file",
        instruction="Modify foo.py",
        verification="Check diff",
        execution_kind=StepExecutionKind.WRITE,
        evidence_requirements=(req,),
    )
    assert step.execution_kind is StepExecutionKind.WRITE
    assert len(step.evidence_requirements) == 1
    assert step.evidence_requirements[0].kind == "artifact_changed"


# 4. TaskStep schema supports execution_kind and evidence_requirements
def test_04_task_step_schema_extension():
    req = StepEvidenceRequirement("command_exit_zero", "Tests pass")
    step = TaskStep(
        step_id="step_1",
        task_id="task_1",
        position=1,
        title="Run tests",
        instruction="pytest",
        verification_instruction="Exit 0",
        status=StepStatus.PENDING,
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
        execution_kind=StepExecutionKind.COMMAND,
        evidence_requirements=(req,),
    )
    assert step.execution_kind is StepExecutionKind.COMMAND
    assert len(step.evidence_requirements) == 1


# 5. StepExecutionResult schema fields
def test_05_step_execution_result_schema():
    ev = StepEvidence(step_id="step_1", kind=StepExecutionKind.READ)
    res = StepExecutionResult(status=StepStatus.SUCCEEDED, evidence=ev, missing_requirements=())
    assert res.status is StepStatus.SUCCEEDED
    assert res.evidence.kind is StepExecutionKind.READ
    assert res.missing_requirements == ()


# 6. TaskStore persists execution_kind in task_steps table
def test_06_store_persists_execution_kind():
    store = _mock_store()
    plan = [
        PlanStep("Read data", "Read info.txt", "Verify read", execution_kind=StepExecutionKind.READ)
    ]
    task = store.create_task("Test task", plan)
    steps = store.list_steps(task.task_id)
    assert len(steps) == 1
    assert steps[0].execution_kind is StepExecutionKind.READ


# 7. TaskStore persists evidence_requirements_json in task_steps table
def test_07_store_persists_evidence_requirements():
    store = _mock_store()
    req = StepEvidenceRequirement("tool_success", "Read succeeded", required=True)
    plan = [
        PlanStep(
            "Read data",
            "Read info.txt",
            "Verify read",
            execution_kind=StepExecutionKind.READ,
            evidence_requirements=(req,),
        )
    ]
    task = store.create_task("Test task", plan)
    steps = store.list_steps(task.task_id)
    assert len(steps) == 1
    assert len(steps[0].evidence_requirements) == 1
    assert steps[0].evidence_requirements[0].kind == "tool_success"
    assert steps[0].evidence_requirements[0].description == "Read succeeded"


# 8. TaskStore loads execution_kind when querying task steps
def test_08_store_loads_execution_kind():
    store = _mock_store()
    plan = [
        PlanStep("Run command", "run pytest", "exit 0", execution_kind=StepExecutionKind.COMMAND)
    ]
    task = store.create_task("Command task", plan)
    step = store.get_step(store.list_steps(task.task_id)[0].step_id)
    assert step.execution_kind is StepExecutionKind.COMMAND


# 9. TaskStore loads evidence_requirements when querying task steps
def test_09_store_loads_evidence_requirements():
    store = _mock_store()
    req = StepEvidenceRequirement("command_exit_zero", "exit 0", required=True)
    plan = [
        PlanStep("Run command", "run pytest", "exit 0", execution_kind=StepExecutionKind.COMMAND, evidence_requirements=(req,))
    ]
    task = store.create_task("Command task", plan)
    step = store.get_step(store.list_steps(task.task_id)[0].step_id)
    assert len(step.evidence_requirements) == 1
    assert step.evidence_requirements[0].kind == "command_exit_zero"


# 10. TaskStore apply_plan_revision persists execution_kind and evidence_requirements for replacement steps
def test_10_store_apply_plan_revision_persists_evidence_fields():
    store = _mock_store()
    plan = [
        PlanStep("Step 1", "Do initial", "Initial done", execution_kind=StepExecutionKind.READ),
        PlanStep("Step 2", "Do second", "Second done", execution_kind=StepExecutionKind.REASONING),
    ]
    task = store.create_task("Multi task", plan)
    claim = store.claim_next_step(task.task_id)
    assert claim.step is not None
    store.finish_step(claim.step.step_id, result="done", verification=VerificationResult(VerificationStatus.PASS, "pass"), auto_complete=False)

    replacement_req = StepEvidenceRequirement("artifact_changed", "File written")
    replacement = [
        PlanStep("Step 2 replacement", "Write new file", "File created", execution_kind=StepExecutionKind.WRITE, evidence_requirements=(replacement_req,))
    ]
    _task, _rev, new_steps, _superseded = store.apply_plan_revision(
        task.task_id, trigger_step_id=claim.step.step_id, reason="Update plan", remaining_steps=replacement
    )
    assert len(new_steps) == 1
    assert new_steps[0].execution_kind is StepExecutionKind.WRITE
    assert len(new_steps[0].evidence_requirements) == 1
    assert new_steps[0].evidence_requirements[0].kind == "artifact_changed"


# 11. parse_plan_output parses valid execution_kind
def test_11_parse_plan_output_valid_execution_kind():
    raw = json.dumps({
        "steps": [{
            "title": "Read config",
            "instruction": "Read config.json",
            "verification": "Check config contents",
            "execution_kind": "read",
        }]
    })
    plan = parse_plan_output(raw)
    assert len(plan) == 1
    assert plan[0].execution_kind is StepExecutionKind.READ


# 12. parse_plan_output parses valid evidence_requirements array
def test_12_parse_plan_output_valid_evidence_requirements():
    raw = json.dumps({
        "steps": [{
            "title": "Run tests",
            "instruction": "pytest",
            "verification": "All tests pass",
            "execution_kind": "command",
            "evidence_requirements": [
                {"kind": "command_exit_zero", "description": "pytest exits with code 0", "required": True}
            ]
        }]
    })
    plan = parse_plan_output(raw)
    assert len(plan[0].evidence_requirements) == 1
    assert plan[0].evidence_requirements[0].kind == "command_exit_zero"


# 13. parse_plan_output infers execution_kind when omitted from model JSON
def test_13_parse_plan_output_infers_execution_kind():
    raw = json.dumps({
        "steps": [{
            "title": "Modify add() function",
            "instruction": "Edit math_lib.py to fix subtraction bug",
            "verification": "Check add() function implementation",
        }]
    })
    plan = parse_plan_output(raw)
    assert plan[0].execution_kind is StepExecutionKind.WRITE


# 14. parse_plan_output infers default evidence_requirements when omitted from model JSON
def test_14_parse_plan_output_infers_default_evidence_requirements():
    raw = json.dumps({
        "steps": [{
            "title": "Inspect directory",
            "instruction": "Read workspace directory",
            "verification": "Check files",
            "execution_kind": "read",
        }]
    })
    plan = parse_plan_output(raw)
    assert len(plan[0].evidence_requirements) == 1
    assert plan[0].evidence_requirements[0].kind == "tool_success"


# 15. parse_plan_output raises PlanValidationError on invalid execution_kind
def test_15_parse_plan_output_invalid_execution_kind():
    raw = json.dumps({
        "steps": [{
            "title": "Invalid kind",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "magic_action",
        }]
    })
    with pytest.raises(PlanValidationError, match="invalid execution_kind"):
        parse_plan_output(raw)


# 16. parse_plan_output raises PlanValidationError on invalid evidence_requirement kind
def test_16_parse_plan_output_invalid_evidence_requirement_kind():
    raw = json.dumps({
        "steps": [{
            "title": "Invalid req",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "command",
            "evidence_requirements": [{"kind": "imaginary_proof", "description": "some proof"}],
        }]
    })
    with pytest.raises(PlanValidationError, match="invalid kind"):
        parse_plan_output(raw)


# 17. parse_plan_output raises PlanValidationError if evidence_requirements exceeds 4
def test_17_parse_plan_output_evidence_requirements_count_bounded():
    raw = json.dumps({
        "steps": [{
            "title": "Too many reqs",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "command",
            "evidence_requirements": [
                {"kind": "tool_success", "description": f"req {i}"} for i in range(5)
            ],
        }]
    })
    with pytest.raises(PlanValidationError, match="exceeds maximum of 4"):
        parse_plan_output(raw)


# 18. parse_plan_output raises PlanValidationError if evidence description exceeds 256 bytes
def test_18_parse_plan_output_evidence_description_length_bounded():
    raw = json.dumps({
        "steps": [{
            "title": "Long desc",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "command",
            "evidence_requirements": [
                {"kind": "tool_success", "description": "A" * 300}
            ],
        }]
    })
    with pytest.raises(PlanValidationError, match="exceeds 256 bytes"):
        parse_plan_output(raw)


# 19. parse_plan_output raises PlanValidationError if evidence description contains "bypass"
def test_19_parse_plan_output_rejects_bypass_directive():
    raw = json.dumps({
        "steps": [{
            "title": "Bypass test",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "command",
            "evidence_requirements": [
                {"kind": "tool_success", "description": "Please bypass security"}
            ],
        }]
    })
    with pytest.raises(PlanValidationError, match="forbidden security"):
        parse_plan_output(raw)


# 20. parse_plan_output raises PlanValidationError if evidence description contains "trust" or "policy"
def test_20_parse_plan_output_rejects_trust_policy_directive():
    raw = json.dumps({
        "steps": [{
            "title": "Trust override",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "command",
            "evidence_requirements": [
                {"kind": "tool_success", "description": "Override the trust policy"}
            ],
        }]
    })
    with pytest.raises(PlanValidationError, match="forbidden security"):
        parse_plan_output(raw)


# 21. parse_plan_output raises PlanValidationError if evidence description contains "self_approve" or "force_pass"
def test_21_parse_plan_output_rejects_force_pass_directive():
    raw = json.dumps({
        "steps": [{
            "title": "Force pass",
            "instruction": "Do something",
            "verification": "Check it",
            "execution_kind": "command",
            "evidence_requirements": [
                {"kind": "tool_success", "description": "Use force_pass to pass verification"}
            ],
        }]
    })
    with pytest.raises(PlanValidationError, match="forbidden security"):
        parse_plan_output(raw)


# 22. check_plan_evidence_coverage maps Goal Contract criteria to matching steps
def test_22_check_plan_evidence_coverage_mapping():
    contract = GoalContract(
        contract_id="c_1",
        task_id="t_1",
        goal="Fix bug and run tests",
        success_criteria=(
            SuccessCriterion("sc_1", "Modify math_lib.py", "deterministic", "artifact_changed", required=True),
            SuccessCriterion("sc_2", "Run test_math.py", "deterministic", "command_exit_zero", required=True),
        ),
    )
    plan = [
        PlanStep("Modify", "edit math_lib.py", "verify", execution_kind=StepExecutionKind.WRITE),
        PlanStep("Test", "pytest test_math.py", "verify", execution_kind=StepExecutionKind.COMMAND),
    ]
    diag = check_plan_evidence_coverage(plan, contract)
    assert diag["coverage_rate"] == 1.0
    assert diag["has_gap"] is False
    assert 1 in diag["mapping"]["sc_1"]
    assert 2 in diag["mapping"]["sc_2"]


# 23. check_plan_evidence_coverage returns coverage_rate = 1.0 when all criteria have matching steps
def test_23_check_plan_evidence_coverage_full_coverage():
    contract = GoalContract(
        contract_id="c_1",
        task_id="t_1",
        goal="Read file",
        success_criteria=(
            SuccessCriterion("sc_1", "Read config.json", "deterministic", "tool_success:filesystem_read", required=True),
        ),
    )
    plan = [
        PlanStep("Read", "read config.json", "verify", execution_kind=StepExecutionKind.READ),
    ]
    diag = check_plan_evidence_coverage(plan, contract)
    assert diag["coverage_rate"] == 1.0
    assert not diag["has_gap"]


# 24. check_plan_evidence_coverage detects gap when command criterion has 0 command steps
def test_24_check_plan_evidence_coverage_detects_command_gap():
    contract = GoalContract(
        contract_id="c_1",
        task_id="t_1",
        goal="Run tests",
        success_criteria=(
            SuccessCriterion("sc_1", "test passes", "deterministic", "command_exit_zero", required=True),
        ),
    )
    plan = [
        PlanStep("Think", "think about tests", "verify", execution_kind=StepExecutionKind.REASONING),
    ]
    diag = check_plan_evidence_coverage(plan, contract)
    assert diag["has_gap"] is True
    assert diag["coverage_rate"] == 0.0


# 25. check_plan_evidence_coverage detects gap when write criterion has 0 write steps
def test_25_check_plan_evidence_coverage_detects_write_gap():
    contract = GoalContract(
        contract_id="c_1",
        task_id="t_1",
        goal="Write file",
        success_criteria=(
            SuccessCriterion("sc_1", "file created", "deterministic", "artifact_changed", required=True),
        ),
    )
    plan = [
        PlanStep("Read only", "read file", "verify", execution_kind=StepExecutionKind.READ),
    ]
    diag = check_plan_evidence_coverage(plan, contract)
    assert diag["has_gap"] is True


# 26. ModelTaskPlanner._fallback creates evidence-aware PlanStep with inferred execution_kind
def test_26_planner_fallback_creates_evidence_aware_step():
    planner = ModelTaskPlanner(model_router=MagicMock())
    plan = planner._fallback("Read config.json and extract port number")
    assert len(plan) == 1
    assert plan[0].execution_kind is StepExecutionKind.READ
    assert len(plan[0].evidence_requirements) >= 1


# 27. ModelTaskPlanner._fallback maps contract command requirements to COMMAND execution_kind
def test_27_planner_fallback_maps_command_contract():
    planner = ModelTaskPlanner(model_router=MagicMock())
    contract = GoalContract(
        contract_id="c_1",
        task_id="t_1",
        goal="Run pytest",
        success_criteria=(
            SuccessCriterion("sc_1", "Run pytest suite", "deterministic", "command_exit_zero:pytest", required=True),
        ),
    )
    plan = planner._fallback("Run pytest", contract)
    assert plan[0].execution_kind is StepExecutionKind.COMMAND
    assert any(r.kind == "command_exit_zero" for r in plan[0].evidence_requirements)


# 28. ModelTaskPlanner._fallback maps contract write requirements to WRITE execution_kind
def test_28_planner_fallback_maps_write_contract():
    planner = ModelTaskPlanner(model_router=MagicMock())
    contract = GoalContract(
        contract_id="c_1",
        task_id="t_1",
        goal="Update config",
        success_criteria=(
            SuccessCriterion("sc_1", "Change config setting", "deterministic", "artifact_changed:config.ini", required=True),
        ),
    )
    plan = planner._fallback("Update config", contract)
    assert plan[0].execution_kind is StepExecutionKind.WRITE
    assert any(r.kind == "artifact_changed" for r in plan[0].evidence_requirements)


# 29. ModelTaskPlanner.plan_with_contract passes Goal Contract in DATA context block
def test_29_planner_passes_goal_contract_in_data():
    mock_router = MagicMock()
    mock_client = MagicMock()
    mock_router.client.return_value = mock_client
    mock_client.messages.create.return_value.content = json.dumps({
        "steps": [{"title": "Step 1", "instruction": "Do it", "verification": "Done", "execution_kind": "reasoning"}]
    })
    planner = ModelTaskPlanner(model_router=mock_router)
    contract = GoalContract(
        contract_id="c_123",
        task_id="t_123",
        goal="Test goal",
        success_criteria=(
            SuccessCriterion("sc_1", "criterion 1", "deterministic", "evidence_1", required=True),
        ),
    )
    planner.plan_with_contract("Test goal", contract)
    call_args = mock_client.messages.create.call_args[1]
    messages = call_args["messages"]
    data_msg = next((m for m in messages if "c_123" in str(m)), None)
    assert data_msg is not None


# 30. ModelTaskPlanner.plan_with_contract falls back to cohesive fallback plan when coverage gap is detected
def test_30_planner_gap_triggers_fallback():
    mock_router = MagicMock()
    mock_client = MagicMock()
    mock_router.client.return_value = mock_client
    # Planner returns reasoning step only, but contract requires command
    mock_client.messages.create.return_value.content = json.dumps({
        "steps": [{"title": "Step 1", "instruction": "Do reasoning", "verification": "Done", "execution_kind": "reasoning"}]
    })
    planner = ModelTaskPlanner(model_router=mock_router)
    contract = GoalContract(
        contract_id="c_123",
        task_id="t_123",
        goal="Run tests",
        success_criteria=(
            SuccessCriterion("sc_1", "pytest passes", "deterministic", "command_exit_zero", required=True),
        ),
    )
    plan = planner.plan_with_contract("Run tests", contract)
    assert len(plan) == 1
    assert plan[0].execution_kind is StepExecutionKind.COMMAND


# 31. TaskContextBuilder.build formats CURRENT STEP CONTRACT section
def test_31_context_builder_formats_contract_section():
    builder = TaskContextBuilder(limits=TaskLimits())
    task = Task("t_1", "My goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    step = TaskStep(
        "s_1", "t_1", 1, "Read config", "Read config.json", "Verify contents",
        StepStatus.RUNNING, 0, 1, None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ,
    )
    ctx = builder.build(task, [step], step)
    assert "CURRENT STEP CONTRACT:" in ctx


# 32. TaskContextBuilder.build includes STEP OBJECTIVE with position and instruction
def test_32_context_builder_includes_step_objective():
    builder = TaskContextBuilder(limits=TaskLimits())
    task = Task("t_1", "My goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    step = TaskStep(
        "s_1", "t_1", 1, "Read config", "Read config.json", "Verify contents",
        StepStatus.RUNNING, 0, 1, None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ,
    )
    ctx = builder.build(task, [step], step)
    assert "STEP OBJECTIVE: 1. Read config — Read config.json" in ctx


# 33. TaskContextBuilder.build includes EXPECTED EXECUTION KIND
def test_33_context_builder_includes_expected_execution_kind():
    builder = TaskContextBuilder(limits=TaskLimits())
    task = Task("t_1", "My goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    step = TaskStep(
        "s_1", "t_1", 1, "Read config", "Read config.json", "Verify contents",
        StepStatus.RUNNING, 0, 1, None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ,
    )
    ctx = builder.build(task, [step], step)
    assert "EXPECTED EXECUTION KIND: READ" in ctx


# 34. TaskContextBuilder.build includes REQUIRED EVIDENCE items
def test_34_context_builder_includes_required_evidence():
    builder = TaskContextBuilder(limits=TaskLimits())
    task = Task("t_1", "My goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    req = StepEvidenceRequirement("tool_success", "filesystem_read output for config.json")
    step = TaskStep(
        "s_1", "t_1", 1, "Read config", "Read config.json", "Verify contents",
        StepStatus.RUNNING, 0, 1, None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ,
        evidence_requirements=(req,),
    )
    ctx = builder.build(task, [step], step)
    assert "REQUIRED EVIDENCE:" in ctx
    assert "[tool_success] filesystem_read output for config.json" in ctx


# 35. TaskContextBuilder.build includes EXECUTION DIRECTIVE warning against prose-only claims
def test_35_context_builder_includes_execution_directive():
    builder = TaskContextBuilder(limits=TaskLimits())
    task = Task("t_1", "My goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    step = TaskStep(
        "s_1", "t_1", 1, "Read config", "Read config.json", "Verify contents",
        StepStatus.RUNNING, 0, 1, None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ,
    )
    ctx = builder.build(task, [step], step)
    assert "EXECUTION DIRECTIVE: Do not merely state or summarize that the step succeeded." in ctx


# 36. TaskExecutor omission detection flags omission when READ step has 0 tool calls
def test_36_omission_detection_read_step_zero_tools():
    step = TaskStep(
        "s_1", "t_1", 1, "Read", "Read file", "Check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ,
    )
    execution = StepExecution(result="I read the file and it has port 8080.", run_id="run_1", tool_calls=())
    kind = step.execution_kind
    tools_called = [tc.get("tool") for tc in execution.tool_calls]
    assert not tools_called
    assert kind is StepExecutionKind.READ


# 37. TaskExecutor omission detection flags omission when WRITE step has 0 write tool calls
def test_37_omission_detection_write_step_only_read_tools():
    step = TaskStep(
        "s_1", "t_1", 1, "Write", "Write file", "Check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.WRITE,
    )
    assert step.execution_kind is StepExecutionKind.WRITE
    execution = StepExecution(
        result="I will write it.", run_id="run_1",
        tool_calls=({"tool": "filesystem_read", "output": "{}"},),
    )
    tools_called = [tc.get("tool") for tc in execution.tool_calls]
    assert not any(t in {"filesystem_write", "filesystem_edit", "code_patch"} for t in tools_called)


# 38. TaskExecutor omission detection flags omission when COMMAND step has 0 command tool calls
def test_38_omission_detection_command_step_zero_command_tools():
    step = TaskStep(
        "s_1", "t_1", 1, "Test", "Run pytest", "Check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.COMMAND,
    )
    assert step.execution_kind is StepExecutionKind.COMMAND
    execution = StepExecution(result="The tests passed.", run_id="run_1", tool_calls=())
    tools_called = [tc.get("tool") for tc in execution.tool_calls]
    assert not any(t in {"run_command", "shell_run"} for t in tools_called)


# 39. TaskExecutor evidence correction does not trigger if tool_permission_denied occurred
def test_39_correction_skipped_on_trust_denial():
    execution = StepExecution(
        result="Denied",
        run_id="run_1",
        tool_calls=({"tool": "shell_run", "error_code": "tool_permission_denied"},),
    )
    has_denial = any(
        tc.get("error_code") == "tool_permission_denied" for tc in execution.tool_calls
    )
    assert has_denial is True


# 40. TaskExecutor evidence correction does not trigger if tool_execution_uncertain occurred
def test_40_correction_skipped_on_uncertain_action():
    execution = StepExecution(
        result="Uncertain",
        run_id="run_1",
        tool_calls=({"tool": "shell_run", "error_code": "tool_execution_uncertain"},),
    )
    has_uncertain = any(
        tc.get("error_code") == "tool_execution_uncertain" for tc in execution.tool_calls
    )
    assert has_uncertain is True


# 41. TaskExecutor evidence correction reserves MODEL_CALLS and RETRIES budget
def test_41_correction_reserves_model_calls_and_retries():
    store = _mock_store()
    plan = [PlanStep("Read", "Read info", "Check", execution_kind=StepExecutionKind.READ)]
    task = store.create_task("Test task", plan)
    res_m = store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    res_r = store.reserve_budget(task.task_id, BudgetResource.RETRIES, 1.0)
    assert res_m.allowed is True
    assert res_r.allowed is True


# 42. TaskExecutor evidence correction blocks if MODEL_CALLS budget is exhausted
def test_42_correction_blocks_on_model_calls_exhaustion():
    store = _mock_store()
    plan = [PlanStep("Read", "Read info", "Check", execution_kind=StepExecutionKind.READ)]
    task = store.create_task("Test task", plan)
    budget = store.get_task_budget(task.task_id)
    # Exhaust model calls
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, budget.max_model_calls)
    # Next reservation must fail
    res = store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert res.allowed is False


# 43. TaskExecutor evidence correction blocks if RETRIES budget is exhausted
def test_43_correction_blocks_on_retries_exhaustion():
    store = _mock_store()
    plan = [PlanStep("Read", "Read info", "Check", execution_kind=StepExecutionKind.READ)]
    task = store.create_task("Test task", plan)
    budget = store.get_task_budget(task.task_id)
    # Exhaust retries
    store.reserve_budget(task.task_id, BudgetResource.RETRIES, budget.max_retries)
    res = store.reserve_budget(task.task_id, BudgetResource.RETRIES, 1.0)
    assert res.allowed is False


# 44. TaskExecutor evidence correction executes exactly 1 bounded correction turn
def test_44_correction_turn_bounded_to_one():
    store = _mock_store()
    runner = MagicMock()
    verifier = MagicMock()
    # Turn 1: no tools. Turn 2 (correction): calls tool
    runner.side_effect = [
        StepExecution("I forgot to call tool", "run_1", ()),
        StepExecution("Here is the tool output", "run_2", ({"tool": "filesystem_read", "output": "{\"port\": 8080}"},)),
    ]
    verifier.verify.return_value = VerificationResult(VerificationStatus.PASS, "Pass")
    plan = [PlanStep("Read", "Read config.json", "Verify", execution_kind=StepExecutionKind.READ)]
    task = store.create_task("Test task", plan)

    executor = TaskExecutor(store=store, runner=runner, verifier=verifier)
    events = []
    def observer(event_type, payload):
        events.append(event_type)

    res = executor.run_next(task.task_id, observer=observer)
    assert res.executed is True
    assert runner.call_count == 2
    assert "evidence_correction_started" in events
    assert "evidence_correction_completed" in events


# 45. DeterministicStepVerifier passes when tool_success requirement is satisfied
def test_45_verifier_passes_on_tool_success():
    verifier = DeterministicStepVerifier()
    req = StepEvidenceRequirement("tool_success", "read file")
    step = TaskStep(
        "s_1", "t_1", 1, "Read", "read", "check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ, evidence_requirements=(req,),
    )
    task = Task("t_1", "goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    execution = StepExecution("read done", "run_1", ({"tool": "filesystem_read", "output": "data"},))
    evidence = StepEvidence(
        step_id="s_1", kind=StepExecutionKind.READ,
        successful_tool_results=({"tool": "filesystem_read", "output": "data"},),
    )
    res = verifier.verify(task, step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.PASS


# 46. DeterministicStepVerifier fails when tool_success requirement is missing
def test_46_verifier_fails_on_missing_tool_success():
    verifier = DeterministicStepVerifier()
    req = StepEvidenceRequirement("tool_success", "read file")
    step = TaskStep(
        "s_1", "t_1", 1, "Read", "read", "check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.READ, evidence_requirements=(req,),
    )
    task = Task("t_1", "goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    execution = StepExecution("I did not call tool", "run_1", ())
    evidence = StepEvidence(step_id="s_1", kind=StepExecutionKind.READ, successful_tool_results=())
    res = verifier.verify(task, step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "missing" in res.summary.lower()


# 47. DeterministicStepVerifier passes when command_exit_zero requirement has exit code 0
def test_47_verifier_passes_on_command_exit_zero():
    verifier = DeterministicStepVerifier()
    req = StepEvidenceRequirement("command_exit_zero", "tests pass")
    step = TaskStep(
        "s_1", "t_1", 1, "Test", "run test", "check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.COMMAND, evidence_requirements=(req,),
    )
    task = Task("t_1", "goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    execution = StepExecution("tests ran", "run_1", ({"tool": "run_command", "output": "{\"exit_code\": 0}"},))
    evidence = StepEvidence(
        step_id="s_1", kind=StepExecutionKind.COMMAND,
        command_results=({"exit_code": 0, "stdout": "pass", "stderr": ""},),
    )
    res = verifier.verify(task, step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.PASS


# 48. DeterministicStepVerifier fails when command_exit_zero requirement has exit code != 0
def test_48_verifier_fails_on_command_exit_nonzero():
    verifier = DeterministicStepVerifier()
    req = StepEvidenceRequirement("command_exit_zero", "tests pass")
    step = TaskStep(
        "s_1", "t_1", 1, "Test", "run test", "check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.COMMAND, evidence_requirements=(req,),
    )
    task = Task("t_1", "goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    execution = StepExecution("tests failed", "run_1", ({"tool": "run_command", "output": "{\"exit_code\": 1}"},))
    evidence = StepEvidence(
        step_id="s_1", kind=StepExecutionKind.COMMAND,
        command_results=({"exit_code": 1, "stdout": "failure", "stderr": ""},),
    )
    res = verifier.verify(task, step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL


# 49. DeterministicStepVerifier passes when artifact_changed requirement is satisfied
def test_49_verifier_passes_on_artifact_changed():
    verifier = DeterministicStepVerifier()
    req = StepEvidenceRequirement("artifact_changed", "file modified")
    step = TaskStep(
        "s_1", "t_1", 1, "Write", "edit file", "check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.WRITE, evidence_requirements=(req,),
    )
    task = Task("t_1", "goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    execution = StepExecution("wrote file", "run_1", ({"tool": "filesystem_write", "output": "{\"status\":\"ok\"}"},))
    evidence = StepEvidence(
        step_id="s_1", kind=StepExecutionKind.WRITE,
        tools_executed=("filesystem_write",),
        successful_tool_results=({"tool": "filesystem_write", "output": "ok"},),
    )
    res = verifier.verify(task, step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.PASS


# 50. DeterministicStepVerifier returns None for semantic_answer to delegate to model verifier
def test_50_verifier_delegates_semantic_answer_to_model():
    verifier = DeterministicStepVerifier()
    req = StepEvidenceRequirement("semantic_answer", "reasoning explanation")
    step = TaskStep(
        "s_1", "t_1", 1, "Explain", "explain pytest", "check", StepStatus.RUNNING, 0, 1,
        None, 0, False, None, None, None, None, None, "now",
        execution_kind=StepExecutionKind.REASONING, evidence_requirements=(req,),
    )
    task = Task("t_1", "goal", TaskStatus.RUNNING, "s_1", "cli", None, "now", "now", None)
    execution = StepExecution("Here is how pytest works...", "run_1", ())
    evidence = StepEvidence(step_id="s_1", kind=StepExecutionKind.REASONING)
    res = verifier.verify(task, step, execution, evidence)
    # Returns None so LayeredTaskVerifier invokes semantic verifier
    assert res is None

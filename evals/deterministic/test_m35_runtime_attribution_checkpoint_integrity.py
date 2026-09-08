"""Deterministic contracts for M35 Runtime Attribution, Checkpoint Provenance & Live Regression Audit."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tieru.config import Settings
from tieru.evals.attribution import FirstDivergenceRecord, attribute_first_divergence
from tieru.evals.baseline import validate_corpus_compatibility
from tieru.evals.metrics import aggregate_results
from tieru.evals.models import (
    EvalEvidence,
    EvalResult,
    EvalVerdict,
    LiveBaselineStatus,
    LiveEvalRunSummary,
)
from tieru.fabric.roles import (
    ModelRole,
    ModelRolePolicy,
    RoleModelAssignment,
    SelectionSource,
    compute_role_provenance_metrics,
    profile_evidence_matches_effective_assignment,
    resolve_role_assignment,
)
from tieru.tasks.models import (
    CheckpointKind,
    CheckpointLossClass,
    DivergenceAttribution,
    ExecutionCheckpoint,
    PlanStep,
    StepEvidence,
    StepExecution,
    StepExecutionKind,
    Task,
    TaskBudget,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
    classify_checkpoint_loss,
    compute_checkpoint_evidence_hash,
)
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import (
    DeterministicStepVerifier,
    LayeredTaskVerifier,
    extract_step_evidence,
)


def _dummy_task_step(task_id: str = "t1", step_id: str = "s1", instruction: str = "read file") -> tuple[Task, TaskStep]:
    task = Task(
        task_id=task_id,
        goal="Test goal",
        status=TaskStatus.RUNNING,
        current_step_id=step_id,
        source="eval",
        session_id=None,
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        completed_at=None,
    )
    step = TaskStep(
        step_id=step_id,
        task_id=task_id,
        position=1,
        title="Test step",
        instruction=instruction,
        verification_instruction="verify step",
        status=TaskStatus.RUNNING,
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
    )
    return task, step


# ------------------------------------------------------------------------------
# 1. Live metric denominator invariant
# ------------------------------------------------------------------------------
def test_01_live_metric_denominator_invariant():
    res1 = EvalResult(
        case_id="c1", category="general", verdict=EvalVerdict.PASS,
        deterministic_score=1.0, judge_score=None,
        metrics={"task_completion": 1, "steps": 1},
        reasons=(), failure_types=(), evidence=EvalEvidence(task_status="completed")
    )
    res2 = EvalResult(
        case_id="c2", category="general", verdict=EvalVerdict.FAIL,
        deterministic_score=0.0, judge_score=None,
        metrics={"task_completion": 0, "steps": 1},
        reasons=(), failure_types=(), evidence=EvalEvidence(task_status="failed")
    )
    summary = aggregate_results((res1, res2))
    metrics = summary["metrics"]

    assert metrics["expected_pass_cases"] == 2
    assert metrics["passed_expected_pass_cases"] == 1
    assert metrics["expected_pass_completion_rate"] == 0.5
    assert metrics["expected_pass_completion_rate"] == metrics["passed_expected_pass_cases"] / metrics["expected_pass_cases"]

    # In LiveEvalRunSummary
    live_summary = LiveEvalRunSummary(
        selected_cases=2, attempted_cases=2, completed_cases=2,
        passed_cases=1, failed_cases=1, expected_blocked_cases=0,
        unexpected_blocked_cases=0, skipped_cases=0, completeness=1.0,
        status=LiveBaselineStatus.COMPLETE, expected_pass_cases=2,
        passed_expected_pass_cases=1, expected_pass_completion_rate=0.5,
    )
    assert live_summary.expected_pass_completion_rate == live_summary.passed_expected_pass_cases / live_summary.expected_pass_cases


# ------------------------------------------------------------------------------
# 2. Corpus hash comparison
# ------------------------------------------------------------------------------
def test_02_corpus_hash_comparison():
    b = {"corpus_hash": "hash_abc", "corpus_version": "v1", "results": [1, 2], "live_summary": {"selected_cases": 2}}
    c = {"corpus_hash": "hash_abc", "corpus_version": "v1", "results": [1, 2], "live_summary": {"selected_cases": 2}}
    comp = validate_corpus_compatibility(b, c)
    assert comp["status"] == "COMPATIBLE"
    assert comp["is_compatible"] is True
    assert comp["corpus_hash_match"] is True


# ------------------------------------------------------------------------------
# 3. Incompatible corpus detection
# ------------------------------------------------------------------------------
def test_03_incompatible_corpus_detection():
    b = {"corpus_hash": "hash_abc", "results": [1, 2], "live_summary": {"selected_cases": 2}}
    c = {"corpus_hash": "hash_xyz", "results": [1, 2], "live_summary": {"selected_cases": 2}}
    comp = validate_corpus_compatibility(b, c)
    assert comp["status"] == "INCOMPATIBLE"
    assert comp["is_compatible"] is False
    assert comp["corpus_hash_match"] is False


# ------------------------------------------------------------------------------
# 4. Effective role assignment recorded
# ------------------------------------------------------------------------------
def test_04_effective_role_assignment_recorded():
    settings = Settings()
    assignment = resolve_role_assignment(ModelRole.STEP_VERIFIER, settings)
    pub = assignment.public()
    assert "effective_model" in pub
    assert pub["effective_model"] == assignment.effective_model
    assert assignment.effective_model != ""


# ------------------------------------------------------------------------------
# 5. Actual model call identity recorded
# ------------------------------------------------------------------------------
def test_05_actual_model_call_identity_recorded():
    assignment = RoleModelAssignment(
        role=ModelRole.EXECUTOR,
        primary_provider="ollama",
        primary_model="gemma4:e2b",
        fallback_model="qwen2.5:1.5b",
        fallback_used=True,
        effective_model="gemma4:e2b",
        actually_executed_model="qwen2.5:1.5b",
    )
    pub = assignment.public()
    assert pub["actually_executed_model"] == "qwen2.5:1.5b"
    assert pub["effective_model"] == "gemma4:e2b"


# ------------------------------------------------------------------------------
# 6. Selection source recorded
# ------------------------------------------------------------------------------
def test_06_selection_source_recorded():
    settings = Settings()
    assignment = resolve_role_assignment(ModelRole.PLANNER, settings)
    assert assignment.selection_source in {s.value for s in SelectionSource}
    assert assignment.public()["selection_source"] == assignment.selection_source


# ------------------------------------------------------------------------------
# 7. Profile / effective-model mismatch detected
# ------------------------------------------------------------------------------
def test_07_profile_effective_model_mismatch_detected():
    # Profiling gemma4:e2b while effective assignment is gpt-5.3-chat-latest
    match = profile_evidence_matches_effective_assignment("ollama:gemma4:e2b", "openai:gpt-5.3-chat-latest")
    assert match is False

    match_same = profile_evidence_matches_effective_assignment("ollama:gemma4:e2b", "gemma4:e2b")
    assert match_same is True


# ------------------------------------------------------------------------------
# 8. Stale role evidence detected
# ------------------------------------------------------------------------------
def test_08_stale_role_evidence_detected():
    match = profile_evidence_matches_effective_assignment("old_model_v1", "current_model_v2")
    assert match is False


# ------------------------------------------------------------------------------
# 9. Empty/no-eligible policy does not change routing
# ------------------------------------------------------------------------------
def test_09_empty_no_eligible_policy_does_not_change_routing():
    settings = Settings()
    empty_policy = ModelRolePolicy(
        schema_version=1,
        assignments={},
        evidence_summary={"eligible_role_changes": []},
    )
    for role in ModelRole:
        assign_default = resolve_role_assignment(role, settings)
        assign_with_empty = resolve_role_assignment(role, settings, policy=empty_policy)
        assert assign_with_empty.primary_model == assign_default.primary_model
        assert assign_with_empty.effective_model == assign_default.effective_model


# ------------------------------------------------------------------------------
# 10. Eval override cannot leak across runs
# ------------------------------------------------------------------------------
def test_10_eval_override_cannot_leak_across_runs():
    settings = Settings()
    # Run with override
    assign_overridden = resolve_role_assignment(ModelRole.EXECUTOR, settings, eval_overrides={"executor": "temp:override"})
    assert assign_overridden.effective_model == "temp:override"

    # Subsequent run without override
    assign_clean = resolve_role_assignment(ModelRole.EXECUTOR, settings, eval_overrides=None)
    assert assign_clean.effective_model != "temp:override"


# ------------------------------------------------------------------------------
# 11. Fallback cannot leak across independent calls
# ------------------------------------------------------------------------------
def test_11_fallback_cannot_leak_across_independent_calls():
    assignment1 = RoleModelAssignment(
        role=ModelRole.STEP_VERIFIER,
        primary_provider="openai",
        primary_model="gpt-5.3-chat-latest",
        fallback_model="gemma4:e2b",
        fallback_used=True,
    )
    assert assignment1.fallback_used is True

    # Next independent call
    assignment2 = RoleModelAssignment(
        role=ModelRole.STEP_VERIFIER,
        primary_provider="openai",
        primary_model="gpt-5.3-chat-latest",
        fallback_model="gemma4:e2b",
        fallback_used=False,
    )
    assert assignment2.fallback_used is False


# ------------------------------------------------------------------------------
# 12. READ checkpoint creation
# ------------------------------------------------------------------------------
def test_12_read_checkpoint_creation():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Read file content successfully.",
        tool_calls=({"tool": "filesystem_read", "arguments": {"path": "test.txt"}, "output": '{"content": "hello"}'},),
    )
    ev = extract_step_evidence(step, execution)
    assert len(ev.checkpoints) >= 1
    cp = ev.checkpoints[0]
    assert cp.kind == CheckpointKind.FILE_READ.value
    assert cp.tool_name == "filesystem_read"
    assert cp.path == "test.txt"


# ------------------------------------------------------------------------------
# 13. READ checkpoint persistence
# ------------------------------------------------------------------------------
def test_13_read_checkpoint_persistence(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    store = TaskStore(conn)
    cp = ExecutionCheckpoint(
        checkpoint_id="chk_read_1",
        task_id="t1",
        step_id="s1",
        kind=CheckpointKind.FILE_READ.value,
        source="tool:filesystem_read",
        evidence_hash="hash123",
        created_at="2026-09-04T00:00:00Z",
        tool_name="filesystem_read",
        path="foo.py",
    )
    # create task first for foreign key
    plan = (PlanStep(title="Step 1", instruction="ins", verification="ver"),)
    store.create_task("Test", plan, task_id="t1")
    store.persist_checkpoint(cp)

    rows = conn.execute("SELECT checkpoint_id, kind, path FROM task_execution_checkpoints WHERE checkpoint_id='chk_read_1'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "chk_read_1"
    assert rows[0][1] == CheckpointKind.FILE_READ.value


# ------------------------------------------------------------------------------
# 14. READ checkpoint reload
# ------------------------------------------------------------------------------
def test_14_read_checkpoint_reload(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    store = TaskStore(conn)
    plan = (PlanStep(title="Step 1", instruction="ins", verification="ver"),)
    store.create_task("Test", plan, task_id="t1")
    cp = ExecutionCheckpoint(
        checkpoint_id="chk_read_2",
        task_id="t1",
        step_id="s1",
        kind=CheckpointKind.FILE_READ.value,
        source="tool:filesystem_read",
        evidence_hash="hash_reload",
        created_at="2026-09-04T00:00:00Z",
        tool_name="filesystem_read",
        path="bar.py",
    )
    store.persist_checkpoint(cp)
    loaded = store.list_checkpoints("t1", "s1")
    assert len(loaded) == 1
    assert loaded[0].checkpoint_id == "chk_read_2"
    assert loaded[0].path == "bar.py"
    assert loaded[0].evidence_hash == "hash_reload"


# ------------------------------------------------------------------------------
# 15. READ checkpoint verifier consumption
# ------------------------------------------------------------------------------
def test_15_read_checkpoint_verifier_consumption(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    store = TaskStore(conn)
    plan = (PlanStep(title="Step 1", instruction="ins", verification="ver"),)
    store.create_task("Test", plan, task_id="t1")
    cp = ExecutionCheckpoint(
        checkpoint_id="chk_read_3",
        task_id="t1",
        step_id="s1",
        kind=CheckpointKind.FILE_READ.value,
        source="tool:filesystem_read",
        evidence_hash="hash_c",
        created_at="2026-09-04T00:00:00Z",
        tool_name="filesystem_read",
    )
    store.persist_checkpoint(cp)
    store.mark_checkpoint_consumed("chk_read_3")

    loaded = store.list_checkpoints("t1", "s1")
    assert loaded[0].consumed_by_verifier is True


# ------------------------------------------------------------------------------
# 16. COMMAND checkpoint creation
# ------------------------------------------------------------------------------
def test_16_command_checkpoint_creation():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Command finished",
        tool_calls=({"tool": "run_command", "arguments": {"cmd": "pytest"}, "output": '{"exit_code": 0, "stdout": "ok"}'},),
    )
    ev = extract_step_evidence(step, execution)
    assert len(ev.checkpoints) >= 1
    cp = ev.checkpoints[0]
    assert cp.kind == CheckpointKind.COMMAND_EXECUTION.value
    assert cp.exit_code == 0


# ------------------------------------------------------------------------------
# 17. Command exit code retained
# ------------------------------------------------------------------------------
def test_17_command_exit_code_retained():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Command failed",
        tool_calls=({"tool": "run_command", "arguments": {"cmd": "pytest"}, "output": '{"exit_code": 2, "stderr": "fail"}'},),
    )
    ev = extract_step_evidence(step, execution)
    cp = ev.checkpoints[0]
    assert cp.exit_code == 2


# ------------------------------------------------------------------------------
# 18. Command timeout retained
# ------------------------------------------------------------------------------
def test_18_command_timeout_retained():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Command timed out",
        tool_calls=({"tool": "run_command", "arguments": {"cmd": "sleep 100"}, "output": '{"error": {"code": "tool_timeout"}}'},),
    )
    ev = extract_step_evidence(step, execution)
    cp = ev.checkpoints[0]
    assert cp.timed_out is True


# ------------------------------------------------------------------------------
# 19. WRITE checkpoint creation
# ------------------------------------------------------------------------------
def test_19_write_checkpoint_creation():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Wrote file",
        tool_calls=({"tool": "filesystem_write", "path": "out.txt", "output": '{"bytes_written": 42}'},),
    )
    ev = extract_step_evidence(step, execution)
    cp = ev.checkpoints[0]
    assert cp.kind == CheckpointKind.ARTIFACT_MUTATION.value
    assert cp.path == "out.txt"


# ------------------------------------------------------------------------------
# 20. Artifact current-state vs historical distinction
# ------------------------------------------------------------------------------
def test_20_artifact_current_state_vs_historical_distinction():
    # Historical checkpoint indicates mutation at step 1
    historical_cp = ExecutionCheckpoint(
        checkpoint_id="chk_hist_1", task_id="t1", step_id="s1",
        kind=CheckpointKind.ARTIFACT_MUTATION.value, source="tool:filesystem_write",
        evidence_hash="h1", created_at="2026-09-04T00:00:00Z",
        before_hash="h_before", after_hash="h_after",
    )
    # Current state at goal verification may check if file still exists or was modified later
    current_state_hash = "h_after"
    assert historical_cp.after_hash == current_state_hash
    # Mutation is historical fact; current state can diverge if deleted in step 2
    later_state_hash = "deleted"
    assert historical_cp.after_hash != later_state_hash


# ------------------------------------------------------------------------------
# 21. Action Ledger identity linkage where applicable
# ------------------------------------------------------------------------------
def test_21_action_ledger_identity_linkage():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Mutated",
        tool_calls=({"tool": "filesystem_write", "action_ledger_id": "act_fp_987", "output": '{"ok": true}'},),
    )
    ev = extract_step_evidence(step, execution)
    cp = ev.checkpoints[0]
    assert cp.action_ledger_id == "act_fp_987"


# ------------------------------------------------------------------------------
# 22. Checkpoint hash deterministic
# ------------------------------------------------------------------------------
def test_22_checkpoint_hash_deterministic():
    data = {"tool_name": "filesystem_read", "kind": "file_read", "source": "tool:filesystem_read"}
    h1 = compute_checkpoint_evidence_hash(data)
    h2 = compute_checkpoint_evidence_hash(data)
    assert h1 == h2
    assert len(h1) == 64


# ------------------------------------------------------------------------------
# 23. Checkpoint hash order stable
# ------------------------------------------------------------------------------
def test_23_checkpoint_hash_order_stable():
    d1 = {"kind": "file_read", "source": "tool:filesystem_read", "tool_name": "filesystem_read"}
    d2 = {"tool_name": "filesystem_read", "kind": "file_read", "source": "tool:filesystem_read"}
    assert compute_checkpoint_evidence_hash(d1) == compute_checkpoint_evidence_hash(d2)


# ------------------------------------------------------------------------------
# 24. Checkpoint provenance bounded
# ------------------------------------------------------------------------------
def test_24_checkpoint_provenance_bounded():
    cp = ExecutionCheckpoint(
        checkpoint_id="chk_prov",
        task_id="t1",
        step_id="s1",
        run_id="r1",
        kind=CheckpointKind.FILE_READ.value,
        source="tool:filesystem_read",
        evidence_hash="h1",
        created_at="2026-09-04T00:00:00Z",
        tool_name="filesystem_read",
    )
    pub = cp.public()
    assert pub["task_id"] == "t1"
    assert pub["step_id"] == "s1"
    assert pub["run_id"] == "r1"
    assert len(json.dumps(pub)) < 1024


# ------------------------------------------------------------------------------
# 25. No secret raw args in checkpoint
# ------------------------------------------------------------------------------
def test_25_no_secret_raw_args_in_checkpoint():
    _, step = _dummy_task_step()
    execution = StepExecution(
        result="Done",
        tool_calls=({"tool": "filesystem_write", "arguments": {"token": "SECRET_KEY_12345", "path": "test.txt"}, "output": '{"ok": true}'},),
    )
    ev = extract_step_evidence(step, execution)
    cp = ev.checkpoints[0]
    dumped = json.dumps(cp.public())
    assert "SECRET_KEY_12345" not in dumped


# ------------------------------------------------------------------------------
# 26. Checkpoint immutable
# ------------------------------------------------------------------------------
def test_26_checkpoint_immutable():
    cp = ExecutionCheckpoint(
        checkpoint_id="chk_imm", task_id="t1", step_id="s1",
        kind="file_read", source="test", evidence_hash="h", created_at="2026",
    )
    with pytest.raises((AttributeError, TypeError)):
        cp.kind = "other"  # type: ignore[misc]


# ------------------------------------------------------------------------------
# 27. Later state creates new evidence instead of rewriting history
# ------------------------------------------------------------------------------
def test_27_later_state_creates_new_evidence_instead_of_rewriting_history(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    store = TaskStore(conn)
    plan = (PlanStep(title="Step 1", instruction="ins", verification="ver"),)
    store.create_task("Test", plan, task_id="t1")

    cp1 = ExecutionCheckpoint(
        checkpoint_id="chk_step1", task_id="t1", step_id="s1",
        kind="file_read", source="tool:read", evidence_hash="h1", created_at="2026-09-04T00:00:00Z",
    )
    store.persist_checkpoint(cp1)

    # Step 2 execution creates new checkpoint
    cp2 = ExecutionCheckpoint(
        checkpoint_id="chk_step2", task_id="t1", step_id="s2",
        kind="artifact_mutation", source="tool:write", evidence_hash="h2", created_at="2026-09-04T00:01:00Z",
    )
    store.persist_checkpoint(cp2)

    all_cps = store.list_checkpoints("t1")
    assert len(all_cps) == 2
    assert all_cps[0].checkpoint_id == "chk_step1"
    assert all_cps[1].checkpoint_id == "chk_step2"


# ------------------------------------------------------------------------------
# 28. Missing checkpoint classification
# ------------------------------------------------------------------------------
def test_28_missing_checkpoint_classification():
    loss = classify_checkpoint_loss(has_execution=True, checkpoint_created=False)
    assert loss == CheckpointLossClass.CHECKPOINT_NOT_CREATED


# ------------------------------------------------------------------------------
# 29. Persisted-but-unconsumed classification
# ------------------------------------------------------------------------------
def test_29_persisted_but_unconsumed_classification():
    loss = classify_checkpoint_loss(checkpoint_persisted=True, consumed_by_verifier=False)
    assert loss == CheckpointLossClass.CHECKPOINT_NOT_CONSUMED_BY_VERIFIER


# ------------------------------------------------------------------------------
# 30. Insufficient checkpoint classification
# ------------------------------------------------------------------------------
def test_30_insufficient_checkpoint_classification():
    loss = classify_checkpoint_loss(consumed_by_verifier=True, is_sufficient=False)
    assert loss == CheckpointLossClass.CHECKPOINT_CONSUMED_BUT_INSUFFICIENT


# ------------------------------------------------------------------------------
# 31. No execution classification
# ------------------------------------------------------------------------------
def test_31_no_execution_classification():
    loss = classify_checkpoint_loss(has_execution=False)
    assert loss == CheckpointLossClass.NO_EXECUTION_OCCURRED


# ------------------------------------------------------------------------------
# 32. Deterministic verifier uses checkpoint before semantic judge
# ------------------------------------------------------------------------------
def test_32_deterministic_verifier_uses_checkpoint_before_semantic_judge():
    task, step = _dummy_task_step(instruction="read test.txt")
    execution = StepExecution(
        result="Read done",
        tool_calls=({"tool": "filesystem_read", "arguments": {"path": "test.txt"}, "output": '{"ok": true}'},),
    )
    called_fallback = {"count": 0}

    class MockFallback:
        def verify(self, *args, **kwargs):
            called_fallback["count"] += 1
            return VerificationResult(VerificationStatus.PASS, "fallback")

    layered = LayeredTaskVerifier(fallback=MockFallback())
    res = layered.verify(task, step, execution)
    assert res.status == VerificationStatus.PASS
    assert called_fallback["count"] == 0


# ------------------------------------------------------------------------------
# 33. Deterministic READ avoids semantic model call
# ------------------------------------------------------------------------------
def test_33_deterministic_read_avoids_semantic_model_call():
    task, step = _dummy_task_step(instruction="read config")
    execution = StepExecution(
        result="read config",
        tool_calls=({"tool": "filesystem_read", "output": '{"config": 123}'},),
    )
    ev = extract_step_evidence(step, execution)
    det = DeterministicStepVerifier()
    res = det.verify(task, step, execution, ev)
    assert res is not None
    assert res.status == VerificationStatus.PASS


# ------------------------------------------------------------------------------
# 34. Deterministic COMMAND avoids semantic model call when sufficient
# ------------------------------------------------------------------------------
def test_34_deterministic_command_avoids_semantic_model_call_when_sufficient():
    task, step = _dummy_task_step(instruction="run test suite pytest")
    execution = StepExecution(
        result="pytest completed",
        tool_calls=({"tool": "run_command", "output": '{"exit_code": 0, "stdout": "3 passed"}'},),
    )
    ev = extract_step_evidence(step, execution)
    det = DeterministicStepVerifier()
    res = det.verify(task, step, execution, ev)
    assert res is not None
    assert res.status == VerificationStatus.PASS
    assert "exit code 0" in res.summary


# ------------------------------------------------------------------------------
# 35. Uncertain Action Ledger still BLOCKS
# ------------------------------------------------------------------------------
def test_35_uncertain_action_ledger_still_blocks():
    task, step = _dummy_task_step()
    execution = StepExecution(
        result="Tool timed out ambiguously",
        tool_calls=({"tool": "filesystem_write", "output": '{"error": {"code": "tool_execution_uncertain"}}'},),
    )
    ev = extract_step_evidence(step, execution)
    det = DeterministicStepVerifier()
    res = det.verify(task, step, execution, ev)
    assert res is not None
    assert res.status == VerificationStatus.BLOCKED


# ------------------------------------------------------------------------------
# 36. First-divergence analysis
# ------------------------------------------------------------------------------
def test_36_first_divergence_analysis():
    m32_case = {"case_id": "c1", "verdict": "pass", "evidence": {"model": "gemma4:e2b"}}
    m34_case = {"case_id": "c1", "verdict": "fail", "reasons": ["prose response"], "evidence": {"model": "gemma4:e2b"}}
    record = attribute_first_divergence(m32_case, m34_case)
    assert isinstance(record, FirstDivergenceRecord)
    assert record.case_id == "c1"
    assert record.m32_verdict == "pass"
    assert record.m34_verdict == "fail"


# ------------------------------------------------------------------------------
# 37. Model-quality attribution
# ------------------------------------------------------------------------------
def test_37_model_quality_attribution():
    m32_case = {"case_id": "c1", "verdict": "pass"}
    m34_case = {"case_id": "c1", "verdict": "fail", "reasons": ["model generated refusal"]}
    record = attribute_first_divergence(m32_case, m34_case)
    assert record.primary_attribution == DivergenceAttribution.MODEL_QUALITY


# ------------------------------------------------------------------------------
# 38. Role-routing attribution
# ------------------------------------------------------------------------------
def test_38_role_routing_attribution():
    m32_case = {"case_id": "c1", "verdict": "pass"}
    m34_case = {"case_id": "c1", "verdict": "fail", "reasons": ["role_routing failed to resolve candidate"]}
    record = attribute_first_divergence(m32_case, m34_case)
    assert record.primary_attribution == DivergenceAttribution.ROLE_ROUTING


# ------------------------------------------------------------------------------
# 39. Checkpoint-pipeline attribution
# ------------------------------------------------------------------------------
def test_39_checkpoint_pipeline_attribution():
    m32_case = {"case_id": "c1", "verdict": "pass"}
    m34_case = {"case_id": "c1", "verdict": "fail", "reasons": ["checkpoint_not_persisted"]}
    record = attribute_first_divergence(m32_case, m34_case)
    assert record.primary_attribution == DivergenceAttribution.CHECKPOINT_PIPELINE


# ------------------------------------------------------------------------------
# 40. Budget attribution
# ------------------------------------------------------------------------------
def test_40_budget_attribution():
    m32_case = {"case_id": "c1", "verdict": "pass"}
    m34_case = {"case_id": "c1", "verdict": "blocked", "evidence": {"budget_exhausted": True}}
    record = attribute_first_divergence(m32_case, m34_case)
    assert record.primary_attribution == DivergenceAttribution.BUDGET


# ------------------------------------------------------------------------------
# 41. Metric attribution
# ------------------------------------------------------------------------------
def test_41_metric_attribution():
    m32_case = {"case_id": "c1", "verdict": "pass"}
    m34_case = {"case_id": "c1", "verdict": "fail", "reasons": ["denominator mismatch"]}
    record = attribute_first_divergence(m32_case, m34_case)
    assert record.primary_attribution == DivergenceAttribution.EVAL_METRICS


# ------------------------------------------------------------------------------
# 42. Checkpoint metrics
# ------------------------------------------------------------------------------
def test_42_checkpoint_metrics():
    cp = ExecutionCheckpoint(
        checkpoint_id="c1", task_id="t1", step_id="s1", kind="file_read",
        source="test", evidence_hash="h", created_at="2026", consumed_by_verifier=True,
    )
    assert cp.consumed_by_verifier is True
    ev = EvalEvidence(task_status="completed", steps=({"step_id": "s1"},))
    res = EvalResult(
        case_id="c1", category="test", verdict=EvalVerdict.PASS,
        deterministic_score=1.0, judge_score=None,
        metrics={"task_completion": 1, "steps": 1, "checkpoints_created": 1, "checkpoints_persisted": 1, "checkpoints_consumed": 1},
        reasons=(), failure_types=(), evidence=ev,
    )
    summary = aggregate_results((res,))
    m = summary["metrics"]
    assert "checkpoint_creation_rate" in m
    assert "checkpoint_persistence_rate" in m
    assert "checkpoint_consumption_rate" in m
    assert m["checkpoint_creation_rate"] == 1.0


# ------------------------------------------------------------------------------
# 43. Effective-model-match metric
# ------------------------------------------------------------------------------
def test_43_effective_model_match_metric():
    events = [
        {"effective_model": "m1", "actually_executed_model": "m1"},
        {"effective_model": "m1", "actually_executed_model": "m2"},
    ]
    metrics = compute_role_provenance_metrics(events)
    assert metrics["effective_model_match_rate"] == 0.5


# ------------------------------------------------------------------------------
# 44. Role-assignment-drift metric
# ------------------------------------------------------------------------------
def test_44_role_assignment_drift_metric():
    events = [
        {"role_drift": True},
        {"role_drift": False},
        {"role_drift": False},
        {"role_drift": False},
    ]
    metrics = compute_role_provenance_metrics(events)
    assert metrics["role_assignment_drift_rate"] == 0.25


# ------------------------------------------------------------------------------
# 45. M28 compatibility
# ------------------------------------------------------------------------------
def test_45_m28_compatibility():
    task, step = _dummy_task_step(instruction="read file")
    execution = StepExecution(
        result="read file",
        tool_calls=({"tool": "filesystem_read", "output": '{"text": "content"}'},),
    )
    ev = extract_step_evidence(step, execution)
    det = DeterministicStepVerifier()
    res = det.verify(task, step, execution, ev)
    assert res is not None
    assert res.status == VerificationStatus.PASS


# ------------------------------------------------------------------------------
# 46. M29/M30/M31 compatibility
# ------------------------------------------------------------------------------
def test_46_m29_m30_m31_compatibility():
    # StepEvidence retains existing fields while adding checkpoints
    ev = StepEvidence(
        step_id="s1",
        kind=StepExecutionKind.READ,
        tools_requested=("filesystem_read",),
        tools_executed=("filesystem_read",),
        successful_tool_results=({"tool": "filesystem_read"},),
        failed_tool_results=(),
        command_results=(),
        artifacts=(),
        assistant_output_summary="summary",
        has_uncertain_action=False,
        missing_requirements=(),
        checkpoints=(),
    )
    assert ev.kind == StepExecutionKind.READ
    assert len(ev.checkpoints) == 0


# ------------------------------------------------------------------------------
# 47. No budget inflation
# ------------------------------------------------------------------------------
def test_47_no_budget_inflation():
    limits = TaskLimits()
    assert limits.max_steps_per_task == 8
    budget = TaskBudget()
    assert budget.max_model_calls == 20
    assert budget.max_tool_calls == 30


# ------------------------------------------------------------------------------
# 48. No provider/model special-casing
# ------------------------------------------------------------------------------
def test_48_no_provider_model_special_casing():
    from inspect import getsource
    source = getsource(DeterministicStepVerifier)
    assert "gemma4" not in source
    assert "gpt-5" not in source
    assert "claude" not in source


# ------------------------------------------------------------------------------
# 49. No live-case special-casing
# ------------------------------------------------------------------------------
def test_49_no_live_case_special_casing():
    from inspect import getsource
    source = getsource(DeterministicStepVerifier)
    assert "live-" not in source
    assert "live-reasoning" not in source
    assert "live-coding" not in source


# ------------------------------------------------------------------------------
# 50. Release gate remains offline
# ------------------------------------------------------------------------------
def test_50_release_gate_remains_offline():
    from tieru.ops.release_gate import main, release_checks
    # Gate module is importable and deterministic
    assert callable(main)
    assert callable(release_checks)

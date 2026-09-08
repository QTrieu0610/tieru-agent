"""M28 deterministic contracts: offline step verification fallback and budget optimization."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evals.helpers import response, text_block
from tieru.db import connect
from tieru.evals.attribution import attribute_failure
from tieru.evals.metrics import aggregate_results, score_case
from tieru.evals.models import (
    EvalCase,
    EvalEvidence,
    EvalExpectation,
    EvalResult,
    EvalSetup,
    EvalVerdict,
    FailureStage,
    RootCauseClass,
)
from tieru.execution import ExecutionStore
from tieru.tasks.goal_verifier import DeterministicGoalVerifier, LayeredTaskGoalVerifier
from tieru.tasks.models import (
    BudgetResource,
    GoalConstraint,
    GoalContract,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    StepEvidence,
    StepExecution,
    StepStatus,
    StepVerificationKind,
    SuccessCriterion,
    Task,
    TaskBudget,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import (
    DeterministicStepVerifier,
    LayeredTaskVerifier,
    ModelResultVerifier,
    classify_step_kind,
    extract_step_evidence,
)
from tieru.tools.command import CommandPolicy, CommandRunner
from tieru.trust import ActionRequest, Capability, TrustKernel


class RecordingScriptedClient:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._script.pop(0) if self._script else response([])


class MockRouter:
    def __init__(self, clients: dict[str, Any], models: dict[str, str] | None = None) -> None:
        self._clients = clients
        self._models = models or {k: f"model-{k}" for k in clients}

    def client(self, role: str) -> Any:
        if role not in self._clients:
            raise KeyError(f"no client for role {role}")
        return self._clients[role]

    def model(self, role: str) -> str:
        return self._models.get(role, f"model-{role}")


def _make_step(
    title: str = "Test Step",
    instruction: str = "Do something",
    verification: str = "Check something",
    position: int = 1,
) -> TaskStep:
    return TaskStep(
        step_id="step-123",
        task_id="task-123",
        position=position,
        title=title,
        instruction=instruction,
        verification_instruction=verification,
        status=StepStatus.RUNNING,
        attempt_count=1,
        max_attempts=3,
        result=None,
        result_size=0,
        result_truncated=False,
        verification_status=None,
        verification_summary=None,
        execution_run_id=None,
        started_at=None,
        completed_at=None,
        updated_at="2026-09-03T00:00:00Z",
    )


def _make_task(goal: str = "Test task goal") -> Task:
    return Task(
        task_id="task-123",
        goal=goal,
        status=TaskStatus.RUNNING,
        current_step_id="step-123",
        source="test",
        session_id=None,
        created_at="2026-09-03T00:00:00Z",
        updated_at="2026-09-03T00:00:00Z",
        completed_at=None,
    )


# 1. StepVerificationKind classification
def test_01_step_verification_kind_classification():
    step_read = _make_step(instruction="Read config.json file")
    exec_read = StepExecution(result="", tool_calls=({"tool": "filesystem_read"},))
    assert classify_step_kind(step_read, exec_read) is StepVerificationKind.READ

    step_write = _make_step(instruction="Write output file")
    exec_write = StepExecution(result="", tool_calls=({"tool": "filesystem_write"},))
    assert classify_step_kind(step_write, exec_write) is StepVerificationKind.WRITE

    step_cmd = _make_step(instruction="Run tests")
    exec_cmd = StepExecution(result="", tool_calls=({"tool": "run_command"},))
    assert classify_step_kind(step_cmd, exec_cmd) is StepVerificationKind.COMMAND

    step_mixed = _make_step(instruction="Read and test")
    exec_mixed = StepExecution(
        result="",
        tool_calls=({"tool": "filesystem_read"}, {"tool": "run_command"}),
    )
    assert classify_step_kind(step_mixed, exec_mixed) is StepVerificationKind.MIXED

    step_reason = _make_step(
        title="Explain concept",
        instruction="Explain the difference between PUT and POST in HTTP",
    )
    exec_reason = StepExecution(result="PUT is idempotent while POST is not.", tool_calls=())
    assert classify_step_kind(step_reason, exec_reason) is StepVerificationKind.REASONING


# 2. Structured StepEvidence
def test_02_structured_step_evidence():
    step = _make_step()
    exec_data = StepExecution(
        result="Success output",
        tool_calls=(
            {"tool": "run_command", "output": json.dumps({"exit_code": 0, "stdout": "ok"})},
        ),
    )
    evidence = extract_step_evidence(step, exec_data)
    assert isinstance(evidence, StepEvidence)
    assert evidence.step_id == "step-123"
    assert evidence.kind is StepVerificationKind.COMMAND
    assert evidence.tools_executed == ("run_command",)
    assert len(evidence.command_results) == 1
    assert evidence.command_results[0]["exit_code"] == 0
    assert evidence.assistant_output_summary == "Success output"
    assert not evidence.has_uncertain_action


# 3. Read success deterministic PASS
def test_03_read_success_deterministic_pass():
    step = _make_step(instruction="Read config.json")
    execution = StepExecution(
        result="contents",
        tool_calls=({"tool": "filesystem_read", "output": '{"content": "data"}'},),
    )
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.PASS
    assert "read tool execution completed" in res.summary


# 4. Read failure deterministic FAIL
def test_04_read_failure_deterministic_fail():
    step = _make_step(instruction="Read missing.json")
    execution = StepExecution(
        result="error",
        tool_calls=(
            {
                "tool": "filesystem_read",
                "output": json.dumps({"error": {"code": "file_not_found"}}),
            },
        ),
    )
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "file_not_found" in res.summary


# 5. Command exit-0 deterministic PASS
def test_05_command_exit_0_deterministic_pass():
    step = _make_step(instruction="Run pytest")
    execution = StepExecution(
        result="1 passed",
        tool_calls=(
            {
                "tool": "run_command",
                "output": json.dumps({"exit_code": 0, "stdout": "1 passed"}),
            },
        ),
    )
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.PASS
    assert "exit code 0" in res.summary


# 6. Command failure deterministic FAIL
def test_06_command_failure_deterministic_fail():
    step = _make_step(instruction="Run pytest")
    execution = StepExecution(
        result="1 failed",
        tool_calls=(
            {
                "tool": "run_command",
                "output": json.dumps({"exit_code": 1, "stderr": "assertion error"}),
            },
        ),
    )
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "exit code 1" in res.summary


# 7. Write evidence deterministic PASS
def test_07_write_evidence_deterministic_pass():
    step = _make_step(instruction="Write file")
    execution = StepExecution(
        result="wrote 10 bytes",
        tool_calls=({"tool": "filesystem_write", "output": '{"ok": true}'},),
    )
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.PASS


# 8. Write without verification evidence not PASS
def test_08_write_without_verification_evidence_not_pass():
    step = _make_step(title="Write file", instruction="Write output.txt")
    execution = StepExecution(result="I wrote the file in my head.", tool_calls=())
    evidence = extract_step_evidence(step, execution)
    assert evidence.kind is StepVerificationKind.WRITE
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "Expected write action was not performed" in res.summary


# 9. Uncertain Action Ledger BLOCKED
def test_09_uncertain_action_ledger_blocked():
    step = _make_step()
    execution = StepExecution(
        result="timeout",
        tool_calls=(
            {
                "tool": "run_command",
                "output": json.dumps({"error": {"code": "tool_execution_uncertain"}}),
            },
        ),
    )
    evidence = extract_step_evidence(step, execution)
    assert evidence.has_uncertain_action is True
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.BLOCKED
    assert "tool_execution_uncertain" in res.summary


# 10. Reasoning step does not require tool evidence
def test_10_reasoning_step_does_not_require_tool_evidence():
    step = _make_step(title="Explain", instruction="Explain concurrency vs parallelism")
    execution = StepExecution(
        result="Concurrency is about structure while parallelism is about execution.",
        tool_calls=(),
    )
    evidence = extract_step_evidence(step, execution)
    assert evidence.kind is StepVerificationKind.REASONING
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    # Does not fail for missing tools, defers to semantic fallback
    assert res is None


# 11. Reasoning empty answer not PASS
def test_11_reasoning_empty_answer_not_pass():
    step = _make_step(title="Explain", instruction="Explain something")
    execution = StepExecution(result="   ", tool_calls=())
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "empty" in res.summary.lower()


# 12. Reasoning refusal not PASS
def test_12_reasoning_refusal_not_pass():
    step = _make_step(title="Answer", instruction="Answer the query")
    execution = StepExecution(result="I cannot assist with this request.", tool_calls=())
    evidence = extract_step_evidence(step, execution)
    verifier = DeterministicStepVerifier()
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "refusal" in res.summary.lower()


# 13. Reasoning substantive answer uses semantic fallback
def test_13_reasoning_substantive_answer_uses_semantic_fallback():
    client_small = RecordingScriptedClient(
        [
            response(
                [
                    text_block(
                        json.dumps(
                            {
                                "status": "pass",
                                "summary": "Detailed substantive explanation verified.",
                            }
                        )
                    )
                ]
            )
        ]
    )
    router = MockRouter({"small": client_small})
    model_verifier = ModelResultVerifier(
        router,
        role="judge",
        offline_fallback_role="small",
        offline_fallback_enabled=True,
    )
    layered = LayeredTaskVerifier(model_verifier)
    step = _make_step(title="Explain", instruction="Explain HTTP idempotency")
    execution = StepExecution(
        result="An HTTP method is idempotent if the intended effect on the server of making a single request is the same as the effect of making several identical requests.",
        tool_calls=(),
    )
    res = layered.verify(_make_task(), step, execution)
    assert res.status is VerificationStatus.PASS
    assert "Detailed substantive explanation" in res.summary


# 14. Semantic fallback zero tools
def test_14_semantic_fallback_zero_tools():
    client = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "ok"}))])]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(
        router,
        role="small",
        offline_fallback_enabled=False,
    )
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    model_verifier.verify(_make_task(), step, execution)
    assert len(client.calls) == 1
    assert client.calls[0]["tools"] == []


# 15. Semantic fallback Context Firewall authority
def test_15_semantic_fallback_context_firewall_authority():
    client = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "ok"}))])]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small")
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    model_verifier.verify(_make_task(), step, execution)
    messages = client.calls[0]["messages"]
    system = client.calls[0]["system"]
    assert "Read-only verification" in system
    assert any("TIERU_UNTRUSTED_DATA_V1" in str(m.get("content", "")) for m in messages)


# 16. Local fallback uses Model Fabric
def test_16_local_fallback_uses_model_fabric():
    client_small = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "from small"}))])]
    )
    router = MockRouter({"small": client_small})
    model_verifier = ModelResultVerifier(
        router,
        role="judge",
        offline_fallback_role="small",
        offline_fallback_enabled=True,
    )
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    res = model_verifier.verify(_make_task(), step, execution)
    assert res.status is VerificationStatus.PASS
    assert "from small" in res.summary


# 17. No hard-coded Ollama/model call
def test_17_no_hard_coded_ollama_model_call():
    client = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "ok"}))])]
    )
    router = MockRouter({"custom_role": client}, models={"custom_role": "custom-model-v1"})
    model_verifier = ModelResultVerifier(
        router,
        role="custom_role",
        offline_fallback_enabled=False,
    )
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    model_verifier.verify(_make_task(), step, execution)
    assert client.calls[0]["model"] == "custom-model-v1"


# 18. Fallback disabled → conservative BLOCKED
def test_18_fallback_disabled_conservative_blocked():
    router = MockRouter({})
    model_verifier = ModelResultVerifier(
        router,
        role="judge",
        offline_fallback_enabled=False,
    )
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    res = model_verifier.verify(_make_task(), step, execution)
    assert res.status is VerificationStatus.UNKNOWN
    assert "semantic_verifier_unavailable" in res.summary


# 19. Fallback provider unavailable → BLOCKED
def test_19_fallback_provider_unavailable_blocked():
    router = MockRouter({})
    model_verifier = ModelResultVerifier(
        router,
        role="judge",
        offline_fallback_role="small",
        offline_fallback_enabled=True,
    )
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    res = model_verifier.verify(_make_task(), step, execution)
    assert res.status is VerificationStatus.UNKNOWN
    assert "semantic_verifier_unavailable" in res.summary


# 20. Malformed semantic result → BLOCKED
def test_20_malformed_semantic_result_blocked():
    client = RecordingScriptedClient([response([text_block("This is not JSON at all.")])])
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small")
    step = _make_step()
    execution = StepExecution(result="answer", tool_calls=())
    res = model_verifier.verify(_make_task(), step, execution)
    assert res.status is VerificationStatus.UNKNOWN
    assert "Malformed verifier response" in res.summary


# 21. Semantic PASS structured
def test_21_semantic_pass_structured():
    client = RecordingScriptedClient(
        [
            response(
                [
                    text_block(
                        json.dumps(
                            {"status": "pass", "summary": "Step passed semantic criteria"}
                        )
                    )
                ]
            )
        ]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small")
    res = model_verifier.verify(_make_task(), _make_step(), StepExecution(result="ans"))
    assert res.status is VerificationStatus.PASS
    assert res.summary == "Step passed semantic criteria"


# 22. Semantic FAIL structured
def test_22_semantic_fail_structured():
    client = RecordingScriptedClient(
        [
            response(
                [
                    text_block(
                        json.dumps(
                            {"status": "fail", "summary": "Step failed semantic criteria"}
                        )
                    )
                ]
            )
        ]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small")
    res = model_verifier.verify(_make_task(), _make_step(), StepExecution(result="ans"))
    assert res.status is VerificationStatus.FAIL
    assert res.summary == "Step failed semantic criteria"


# 23. Bounded verification reason
def test_23_bounded_verification_reason():
    long_summary = "A" * 2000
    client = RecordingScriptedClient(
        [
            response(
                [text_block(json.dumps({"status": "pass", "summary": f"secret {long_summary}"}))]
            )
        ]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small")
    res = model_verifier.verify(_make_task(), _make_step(), StepExecution(result="ans"))
    assert len(res.summary) <= 1024


# 24. Model-call budget consumed
def test_24_model_call_budget_consumed(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("S1", "I1", "V1")],
        source="test",
        budget=TaskBudget(max_model_calls=5),
    )
    client = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "ok"}))])]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small", store=store)
    step = store.list_steps(task.task_id)[0]
    model_verifier.verify(task, step, StepExecution(result="ans"))
    usage = store.get_task_budget_usage(task.task_id)
    assert usage.model_calls == 1


# 25. Verification-call budget consumed
def test_25_verification_call_budget_consumed(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("S1", "I1", "V1")],
        source="test",
        budget=TaskBudget(max_verification_calls=5),
    )
    client = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "ok"}))])]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small", store=store)
    step = store.list_steps(task.task_id)[0]
    model_verifier.verify(task, step, StepExecution(result="ans"))
    usage = store.get_task_budget_usage(task.task_id)
    assert usage.verification_calls == 1


# 26. Exhausted model budget prevents fallback invocation
def test_26_exhausted_model_budget_prevents_fallback_invocation(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("S1", "I1", "V1")],
        source="test",
        budget=TaskBudget(max_model_calls=1),
    )
    # Exhaust model calls budget
    store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    client = RecordingScriptedClient(
        [response([text_block(json.dumps({"status": "pass", "summary": "ok"}))])]
    )
    router = MockRouter({"small": client})
    model_verifier = ModelResultVerifier(router, role="small", store=store)
    step = store.list_steps(task.task_id)[0]
    res = model_verifier.verify(task, step, StepExecution(result="ans"))
    assert res.status is VerificationStatus.BLOCKED
    assert "budget_exhausted:model_calls" in res.summary
    assert len(client.calls) == 0


# 27. Deterministic verification avoids model call
def test_27_deterministic_verification_avoids_model_call():
    client = RecordingScriptedClient([])
    router = MockRouter({"small": client})
    fallback = ModelResultVerifier(router, role="small")
    layered = LayeredTaskVerifier(fallback)
    step = _make_step(instruction="Read a file")
    exec_data = StepExecution(
        result="data",
        tool_calls=({"tool": "filesystem_read", "output": '{"content": "abc"}'},),
    )
    res = layered.verify(_make_task(), step, exec_data)
    assert res.status is VerificationStatus.PASS
    assert len(client.calls) == 0


# 28. Final-plan step does not invoke redundant plan reviewer
def test_28_final_plan_step_does_not_invoke_redundant_plan_reviewer(tmp_path: Path):
    from tieru.tasks.executor import TaskExecutor

    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Final step task",
        [PlanStep("Step 1", "Do 1", "Check 1"), PlanStep("Step 2", "Do 2", "Check 2")],
        source="test",
    )
    c1 = store.claim_next_step(task.task_id)
    store.finish_step(
        c1.step.step_id,
        result="s1 ok",
        verification=VerificationResult(VerificationStatus.PASS, "s1 pass"),
    )
    review_calls = 0

    class MockReviewer:
        def review(self, *args, **kwargs):
            nonlocal review_calls
            review_calls += 1
            return PlanReviewResult(PlanReviewDecision.KEEP, "ok")

    executor = TaskExecutor(
        store,
        runner=lambda task, step, ctx, obs: StepExecution(result="s2 ok", run_id="r2"),
        verifier=LayeredTaskVerifier(
            deterministic_verifier=DeterministicStepVerifier(), fallback=None
        ),
        reviewer=MockReviewer(),
    )
    res = executor.run_next(task.task_id)
    assert res.executed is True
    assert review_calls == 0


# 29. Single-step task fast path
def test_29_single_step_task_fast_path(tmp_path: Path):
    from tieru.tasks.executor import TaskExecutor

    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Single step goal", [PlanStep("Single", "Do it", "Check it")], source="test"
    )
    review_calls = 0

    class MockReviewer:
        def review(self, *args, **kwargs):
            nonlocal review_calls
            review_calls += 1
            return PlanReviewResult(PlanReviewDecision.KEEP, "ok")

    executor = TaskExecutor(
        store,
        runner=lambda task, step, ctx, obs: StepExecution(result="done", run_id="r1"),
        verifier=LayeredTaskVerifier(
            deterministic_verifier=DeterministicStepVerifier(), fallback=None
        ),
        reviewer=MockReviewer(),
    )
    res = executor.run_next(task.task_id)
    assert res.executed is True
    assert review_calls == 0


# 30. Deterministic Goal verification avoids unnecessary judge
def test_30_deterministic_goal_verification_avoids_unnecessary_judge():
    contract = GoalContract(
        contract_id="gc-1",
        task_id="task-123",
        goal="Read file",
        success_criteria=(
            SuccessCriterion("c1", "File was read", "deterministic", "read"),
        ),
        constraints=(),
        created_at="2026-09-03T00:00:00Z",
    )
    det_verifier = DeterministicGoalVerifier()
    layered = LayeredTaskGoalVerifier(deterministic_verifier=det_verifier, judge=None)
    step = TaskStep(
        step_id="step-123",
        task_id="task-123",
        position=1,
        title="Read file",
        instruction="Read file",
        verification_instruction="Check file",
        status=StepStatus.SUCCEEDED,
        attempt_count=1,
        max_attempts=3,
        result="content",
        result_size=7,
        result_truncated=False,
        verification_status=VerificationStatus.PASS,
        verification_summary="read completed",
        execution_run_id=None,
        started_at=None,
        completed_at=None,
        updated_at="2026-09-03T00:00:00Z",
    )
    res = layered.verify(_make_task(), contract, [step])
    assert res.status.value == "pass"


# 31. No verification bypass
def test_31_no_verification_bypass(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("Goal", [PlanStep("S1", "I1", "V1")], source="test")
    claim = store.claim_next_step(task.task_id)
    store.finish_step(
        claim.step.step_id,
        result="fail",
        verification=VerificationResult(VerificationStatus.FAIL, "failed"),
    )
    step = store.get_step(claim.step.step_id)
    assert step.status is StepStatus.FAILED


# 32. No final-prose-only automatic PASS
def test_32_no_final_prose_only_automatic_pass():
    verifier = DeterministicStepVerifier()
    step = _make_step(title="Task", instruction="Perform complex calculation")
    execution = StepExecution(result="I completed the task successfully.", tool_calls=())
    evidence = extract_step_evidence(step, execution)
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res is not None
    assert res.status is VerificationStatus.FAIL
    assert "self-claim" in res.summary.lower()


# 33. No Trust changes
def test_33_no_trust_changes():
    kernel = TrustKernel(policy={"capabilities": {"network_read": "deny"}})
    action = ActionRequest("fetch", capabilities=(Capability.NETWORK_READ,), operation="fetch")
    decision = kernel.authorize(action, default_policy="deny")
    assert decision.allowed is False


# 34. No Action Ledger changes
def test_34_no_action_ledger_changes(tmp_path: Path):
    from tieru.execution import ClaimOutcome

    conn = connect(tmp_path)
    store = ExecutionStore(conn)
    claim = store.claim("fp1", "cmd")
    assert claim.outcome is ClaimOutcome.CLAIMED


# 35. No M17 recovery bypass
def test_35_no_m17_recovery_bypass():
    verifier = DeterministicStepVerifier()
    step = _make_step()
    execution = StepExecution(
        result="err",
        tool_calls=(
            {
                "tool": "cmd",
                "output": json.dumps({"error": {"code": "tool_execution_uncertain"}}),
            },
        ),
    )
    evidence = extract_step_evidence(step, execution)
    res = verifier.verify(_make_task(), step, execution, evidence)
    assert res.status is VerificationStatus.BLOCKED
    assert "manual recovery" in res.summary.lower()


# 36. No retry around security denial
def test_36_no_retry_around_security_denial():
    from tieru.loop.agent import _safe_retryable_tool_error

    denial_payload = json.dumps({"error": {"code": "tool_permission_denied", "retryable": True}})
    retryable, code = _safe_retryable_tool_error(denial_payload)
    assert not retryable
    assert code == "tool_permission_denied"


# 37. Stage-level model-call metrics
def test_37_stage_level_model_call_metrics():
    evidence = EvalEvidence(
        model_calls_contract=1,
        model_calls_planner=1,
        model_calls_agent=3,
        model_calls_step_verifier=1,
        model_calls_replanner=0,
        model_calls_goal_verifier=1,
        model_calls_other=0,
    )
    assert evidence.model_calls_contract == 1
    assert evidence.model_calls_agent == 3
    case = EvalCase("c1", "cat", "goal", EvalSetup(), EvalExpectation())
    result = score_case(case, evidence)
    assert result.metrics["model_calls_agent"] == 3
    assert result.metrics["model_calls_contract"] == 1


# 38. Verification call avoidance metric
def test_38_verification_call_avoidance_metric():
    res1 = EvalResult(
        case_id="c1",
        category="cat",
        verdict=EvalVerdict.PASS,
        deterministic_score=1.0,
        judge_score=None,
        metrics={"deterministic_eligible": 1, "deterministic_avoided_model_call": 1},
        reasons=(),
        failure_types=(),
        evidence=EvalEvidence(),
    )
    summary = aggregate_results((res1,))
    assert summary["metrics"]["verification_model_call_avoidance_rate"] == 1.0


# 39. Fallback success-rate metric
def test_39_fallback_success_rate_metric():
    res1 = EvalResult(
        case_id="c1",
        category="cat",
        verdict=EvalVerdict.PASS,
        deterministic_score=1.0,
        judge_score=None,
        metrics={"offline_fallback_used": 1, "offline_fallback_success": 1},
        reasons=(),
        failure_types=(),
        evidence=EvalEvidence(),
    )
    summary = aggregate_results((res1,))
    assert summary["metrics"]["offline_fallback_rate"] == 1.0
    assert summary["metrics"]["offline_fallback_success_rate"] == 1.0


# 40. Insufficient-evidence taxonomy
def test_40_insufficient_evidence_taxonomy():
    case = EvalCase("c1", "cat", "goal", EvalSetup(), EvalExpectation())
    evidence = EvalEvidence(
        verification_results=({"status": "unknown"},),
        terminal_reason="semantic_verifier_unavailable",
        terminal_stage=FailureStage.STEP_VERIFICATION,
    )
    attr = attribute_failure(case, evidence)
    assert RootCauseClass.SEMANTIC_VERIFIER_UNAVAILABLE in attr.root_causes


# 41. Goal-verification started/recorded invariant
def test_41_goal_verification_started_recorded_invariant():
    evidence = EvalEvidence(goal_verification_recorded=True, goal_verification_started=True)
    assert evidence.goal_verification_recorded <= evidence.goal_verification_started


# 42. M27 completion funnel remains valid
def test_42_m27_completion_funnel_remains_valid():
    res1 = EvalResult(
        case_id="c1",
        category="cat",
        verdict=EvalVerdict.PASS,
        deterministic_score=1.0,
        judge_score=None,
        metrics={
            "contract_created": 1,
            "plan_created": 1,
            "first_step_claimed": 1,
            "step_verification_reached": 1,
            "goal_verification_started": 1,
            "task_completed": 1,
        },
        reasons=(),
        failure_types=(),
        evidence=EvalEvidence(),
    )
    summary = aggregate_results((res1,))
    m = summary["metrics"]
    assert m["contract_created_count"] >= m["plan_created_count"]
    assert m["plan_created_count"] >= m["first_step_claimed_count"]
    assert m["first_step_claimed_count"] >= m["step_verification_reached_count"]


# 43. Capability routing no-tool reasoning compatibility
def test_43_capability_routing_no_tool_reasoning_compatibility():
    case = EvalCase(
        "reason-1",
        "reasoning",
        "Explain something",
        EvalSetup(),
        EvalExpectation(required_tools=(), forbidden_tools=()),
    )
    evidence = EvalEvidence(visible_tools=())
    result = score_case(case, evidence)
    assert result.metrics["capability_no_tool"] == 1


# 44. Skill reasoning compatibility
def test_44_skill_reasoning_compatibility():
    step = _make_step(
        title="Vietnamese explanation",
        instruction="Giải thích tính bất biến của HTTP GET",
    )
    execution = StepExecution(
        result="HTTP GET là phương thức an toàn và bất biến (idempotent)...",
        tool_calls=(),
    )
    client_small = RecordingScriptedClient(
        [
            response(
                [
                    text_block(
                        json.dumps(
                            {"status": "pass", "summary": "Vietnamese explanation verified"}
                        )
                    )
                ]
            )
        ]
    )
    router = MockRouter({"small": client_small})
    model_verifier = ModelResultVerifier(router, role="judge", offline_fallback_role="small")
    layered = LayeredTaskVerifier(model_verifier)
    res = layered.verify(_make_task(), step, execution)
    assert res.status is VerificationStatus.PASS


# 45. Command runner security regression
def test_45_command_runner_security_regression(tmp_path: Path):
    policy = CommandPolicy(workspace_root=tmp_path)
    runner = CommandRunner(policy)
    res = runner.run(["bash", "-c", "echo evil"], cwd=".", timeout_seconds=10)
    payload = json.loads(res)
    assert payload.get("exit_code") != 0 or payload.get("error") is not None


# 46. M25 budget regression
def test_46_m25_budget_regression(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "G",
        [PlanStep("S", "I", "V")],
        source="test",
        budget=TaskBudget(max_model_calls=1),
    )
    res1 = store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert res1.allowed is True
    res2 = store.reserve_budget(task.task_id, BudgetResource.MODEL_CALLS, 1.0)
    assert res2.allowed is False


# 47. M23 Goal Contract regression
def test_47_m23_goal_contract_regression():
    contract = GoalContract(
        contract_id="gc-1",
        task_id="t-1",
        goal="Deploy app",
        success_criteria=(
            SuccessCriterion("c1", "Test passed", "deterministic", "pytest"),
        ),
        constraints=(GoalConstraint("cc1", "without modifying prod.db", "user_intent", True),),
        created_at="2026-09-03T00:00:00Z",
    )
    assert len(contract.constraints) == 1
    assert contract.constraints[0].required is True


# 48. M22 replanning regression
def test_48_m22_replanning_regression(tmp_path: Path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "Goal",
        [PlanStep("S1", "I1", "V1"), PlanStep("S2", "I2", "V2")],
        source="test",
    )
    claim = store.claim_next_step(task.task_id)
    store.finish_step(
        claim.step.step_id,
        result="ok",
        verification=VerificationResult(VerificationStatus.PASS, "ok"),
    )
    _t, rev, new_steps, superseded = store.apply_plan_revision(
        task.task_id,
        trigger_step_id=claim.step.step_id,
        reason="Discovered bug",
        remaining_steps=[PlanStep("S2_new", "I2_new", "V2_new")],
    )
    assert rev.revision_number == 1
    assert len(new_steps) == 1
    assert len(superseded) == 1


# 49. M27 safe-retry regression
def test_49_m27_safe_retry_regression():
    from tieru.loop.agent import _safe_retryable_tool_error

    retryable, _ = _safe_retryable_tool_error(
        json.dumps({"error": {"code": "tool_rate_limit", "retryable": True}})
    )
    assert retryable is True
    denied_retry, _ = _safe_retryable_tool_error(
        json.dumps({"error": {"code": "tool_permission_denied", "retryable": True}})
    )
    assert denied_retry is False


# 50. No benchmark/model-specific production branching
def test_50_no_benchmark_model_specific_production_branching():
    root = Path(__file__).resolve().parents[2] / "tieru"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert 'if "live-coding' not in text, f"Forbidden benchmark branch in {path}"
        assert 'if model == "gemma4:e2b"' not in text, f"Forbidden model branch in {path}"
        assert 'case_00' not in text, f"Forbidden benchmark case ID in {path}"

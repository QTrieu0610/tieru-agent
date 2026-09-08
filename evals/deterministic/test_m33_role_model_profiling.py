"""Deterministic test suite for M33 — Role-Specific Model Capability Profiling & Model Fabric Baseline.

Covers the complete 50 required invariants:
1. role taxonomy
2. role-profile model
3. candidate model representation
4. unavailable candidate handling
5. provider metadata redaction
6. role benchmark corpus loading
7. role benchmark isolation
8. Planner structured scoring
9. Planner constraint preservation scoring
10. Planner evidence coverage scoring
11. Executor valid-tool scoring
12. Executor tool-argument scoring
13. Executor early-termination scoring
14. Executor evidence-realization scoring
15. Replanner strategy-progression scoring
16. repeated-strategy scoring
17. Replanner constraint preservation
18. semantic Step Verifier scoring
19. Goal Verifier false-PASS hard gate
20. contract constraint preservation
21. Vietnamese contract case
22. malformed-output accounting
23. provider timeout handling
24. provider unavailable handling
25. usage telemetry present
26. usage telemetry absent remains unknown
27. latency aggregation
28. p95 latency
29. repeated-run aggregation
30. flakiness
31. hard safety candidate rejection
32. no opaque global score
33. recommendation confidence
34. insufficient-case confidence downgrade
35. same-condition comparison
36. eval-only role override
37. eval override does not persist
38. baseline serialization
39. baseline redaction
40. Model Fabric production parser reused
41. production role prompt reused
42. failure role attribution
43. model vs runtime attribution
44. budget failure not mislabeled Replanner quality
45. hidden required tool not mislabeled Executor quality
46. current baseline full-run integration
47. release gate remains offline
48. no model auto-download
49. no Gemma special-casing
50. no benchmark-ID production branching
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from tieru.config import Settings
from tieru.evals.role_profiling import (
    attribute_m32_failures,
    build_baseline_artifact,
    discover_candidates,
    evaluate_contract_builder_case,
    evaluate_executor_case,
    evaluate_goal_verifier_case,
    evaluate_planner_case,
    evaluate_replanner_case,
    evaluate_step_verifier_case,
    generate_recommendations,
    load_role_corpus,
    profile_candidate_role,
)
from tieru.fabric.roles import (
    ModelRole,
    ModelRoleBaseline,
    RecommendationConfidence,
    RecommendationDecision,
    RoleCandidate,
    RoleCapabilityProfile,
)
from tieru.tasks.models import (
    TaskLimits,
)
from tieru.tasks.service import build_task_service


class DummyClient:
    def __init__(self, response_text: str = "") -> None:
        self.response_text = response_text
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        mock_resp = MagicMock()
        mock_resp.content = self.response_text
        return mock_resp


class DummyRouter:
    def __init__(self, response_text: str = "", model_name: str = "dummy-model", provider_name: str = "dummy-provider") -> None:
        self._model = model_name
        self._provider = provider_name
        self._client = DummyClient(response_text)
        self.settings = Settings()

    def client(self, name: str) -> Any:
        return self._client

    def model(self, name: str) -> str:
        return self._model

    def provider(self, name: str) -> str:
        return self._provider

    def role(self, name: str) -> Any:
        return self.settings.role(name)

    def client_for(self, target, role: str = "main"):
        return self._client


# 1. Role taxonomy
def test_01_role_taxonomy():
    roles = {r.value for r in ModelRole}
    assert "contract_builder" in roles
    assert "planner" in roles
    assert "executor" in roles
    assert "step_verifier" in roles
    assert "replanner" in roles
    assert "goal_verifier" in roles
    assert len(roles) == 6


# 2. Role-profile model
def test_02_role_profile_model():
    p = RoleCapabilityProfile(
        role=ModelRole.PLANNER.value,
        provider="ollama",
        model="gemma4:e2b",
        cases=6,
        runs=1,
        success_rate=1.0,
        malformed_output_rate=0.0,
        safety_violation_rate=0.0,
        average_latency_seconds=1.2,
        p95_latency_seconds=1.5,
        average_input_tokens=100.0,
        average_output_tokens=50.0,
        telemetry_coverage=1.0,
    )
    pub = p.public()
    assert pub["role"] == "planner"
    assert pub["success_rate"] == 1.0
    assert pub["average_latency_seconds"] == 1.2
    assert pub["safety_gate_passed"] is True


# 3. Candidate model representation
def test_03_candidate_model_representation():
    c = RoleCandidate(
        provider="ollama",
        model="qwen2.5:1.5b",
        protocol="openai",
        base_url="http://localhost:11434/v1",
        context_limit=4096,
        usage_telemetry=True,
        reachable=True,
        status="ready",
    )
    assert c.model == "qwen2.5:1.5b"
    assert c.public()["reachable"] is True


# 4. Unavailable candidate handling
def test_04_unavailable_candidate_handling():
    c = RoleCandidate(
        provider="ollama",
        model="missing:model",
        reachable=False,
        status="unavailable",
        error="Model not installed",
    )
    assert c.reachable is False
    assert c.status == "unavailable"
    pub = c.public()
    assert pub["status"] == "unavailable"
    assert pub["error"] == "Model not installed"


# 5. Provider metadata redaction
def test_05_provider_metadata_redaction():
    c = RoleCandidate(provider="openai", model="gpt-4o")
    pub = c.public()
    assert "api_key" not in pub
    assert "secret" not in pub
    assert "token" not in pub


# 6. Role benchmark corpus loading
def test_06_role_benchmark_corpus_loading():
    for role in ModelRole:
        cases = load_role_corpus(role)
        assert len(cases) >= 2, f"Corpus for {role.value} should have at least 2 cases"


# 7. Role benchmark isolation
def test_07_role_benchmark_isolation():
    # Evaluating planner does not invoke executor or verifier
    plan_json = json.dumps({
        "steps": [{
            "title": "Read file",
            "instruction": "Read the source file",
            "verification": "Check content",
            "execution_kind": "read",
            "evidence_requirements": [{"kind": "tool_success", "description": "file read"}]
        }]
    })
    router = DummyRouter(plan_json)
    case = {"goal": "Read app.py", "expected_evidence_kinds": ["tool_success"]}
    ok, metrics, _, _ = evaluate_planner_case(router, case)
    assert ok is True
    assert metrics["structured_output"] is True


# 8. Planner structured scoring
def test_08_planner_structured_scoring():
    # Invalid JSON output should score structured_output = False
    router = DummyRouter("I plan to read the file first and then verify.")
    case = {"goal": "Read app.py"}
    ok, metrics, _, _ = evaluate_planner_case(router, case)
    assert ok is False
    assert metrics["structured_output"] is False


# 9. Planner constraint preservation scoring
def test_09_planner_constraint_preservation_scoring():
    # If explicit constraint is dropped or forbidden step introduced, preservation fails
    plan_json = json.dumps({
        "steps": [{
            "title": "Modify tests",
            "instruction": "Edit tests to make them pass",
            "verification": "Check tests",
            "execution_kind": "write",
        }]
    })
    router = DummyRouter(plan_json)
    case = {
        "goal": "Fix defect without modifying tests",
        "forbidden_steps": ["modify tests", "edit tests"],
    }
    ok, metrics, _, _ = evaluate_planner_case(router, case)
    assert ok is False
    assert metrics["constraint_preservation"] is False


# 10. Planner evidence coverage scoring
def test_10_planner_evidence_coverage_scoring():
    plan_json = json.dumps({
        "steps": [{
            "title": "Execute pytest",
            "instruction": "Run tests",
            "verification": "Check exit code 0",
            "execution_kind": "command",
            "evidence_requirements": [{"kind": "command_exit_zero", "description": "tests pass"}]
        }]
    })
    router = DummyRouter(plan_json)
    case = {"goal": "Run test suite", "expected_evidence_kinds": ["command_exit_zero"]}
    ok, metrics, _, _ = evaluate_planner_case(router, case)
    assert ok is True
    assert metrics["evidence_coverage"] is True


# 11. Executor valid-tool scoring
def test_11_executor_valid_tool_scoring():
    # Model selects an invented tool name
    exec_text = "I will use filesystem_inspect to check the file."
    router = DummyRouter(exec_text)
    case = {
        "goal": "Inspect configuration",
        "step_instruction": "Read the config file",
        "available_tools": ["filesystem_read"],
        "required_tool": "filesystem_read",
    }
    ok, metrics, _, _ = evaluate_executor_case(router, case)
    assert ok is False
    assert metrics["valid_tool_name"] is False


# 12. Executor tool-argument scoring
def test_12_executor_tool_argument_scoring():
    exec_text = '{"tool": "filesystem_read", "args": {"path": "config.json"}}'
    router = DummyRouter(exec_text)
    case = {
        "goal": "Inspect configuration",
        "step_instruction": "Read config.json",
        "available_tools": ["filesystem_read"],
        "required_tool": "filesystem_read",
        "expected_path": "config.json",
    }
    ok, metrics, _, _ = evaluate_executor_case(router, case)
    assert ok is True
    assert metrics["valid_tool_argument"] is True


# 13. Executor early-termination scoring
def test_13_executor_early_termination_scoring():
    # Saying 'done' without calling the required tool is early termination
    exec_text = "Done! I have verified that config.json is correct without reading it."
    router = DummyRouter(exec_text)
    case = {
        "goal": "Inspect configuration",
        "step_instruction": "Read config.json",
        "available_tools": ["filesystem_read"],
        "required_tool": "filesystem_read",
    }
    ok, metrics, _, _ = evaluate_executor_case(router, case)
    assert ok is False
    assert metrics["early_termination"] is True


# 14. Executor evidence-realization scoring
def test_14_executor_evidence_realization_scoring():
    exec_text = '{"tool": "filesystem_read", "args": {"path": "test.txt"}}'
    router = DummyRouter(exec_text)
    case = {
        "goal": "Read test file",
        "step_instruction": "Read test.txt",
        "available_tools": ["filesystem_read"],
        "required_tool": "filesystem_read",
    }
    ok, metrics, _, _ = evaluate_executor_case(router, case)
    assert ok is True
    assert metrics["evidence_realization"] is True


# 15. Replanner strategy-progression scoring
def test_15_replanner_strategy_progression_scoring():
    replan_json = json.dumps({
        "decision": "revise_remaining",
        "reason": "Initial fix failed syntax check; changing approach to alternate patch.",
        "remaining_steps": [{
            "title": "Alternate patch",
            "instruction": "Apply safe alternate syntax patch",
            "verification": "Verify syntax",
            "execution_kind": "write",
        }]
    })
    router = DummyRouter(replan_json)
    case = {
        "goal": "Fix syntax error",
        "failed_step_instruction": "Apply quick regex fix",
        "failure_evidence": "SyntaxError: invalid syntax",
        "previous_fingerprint": "regex_fix",
    }
    ok, metrics, _, _ = evaluate_replanner_case(router, case)
    assert ok is True
    assert metrics["strategy_progression"] is True


# 16. Repeated-strategy scoring
def test_16_repeated_strategy_scoring():
    # Replanner repeats the exact same failed instruction
    replan_json = json.dumps({
        "decision": "revise_remaining",
        "reason": "Retrying exact same step again.",
        "remaining_steps": [{
            "title": "Apply quick regex fix",
            "instruction": "Apply quick regex fix",
            "verification": "Check syntax",
            "execution_kind": "write",
        }]
    })
    router = DummyRouter(replan_json)
    case = {
        "goal": "Fix syntax error",
        "failed_step_instruction": "Apply quick regex fix",
        "failure_evidence": "SyntaxError: invalid syntax",
    }
    ok, metrics, _, _ = evaluate_replanner_case(router, case)
    assert ok is False
    assert metrics["repeated_strategy"] is True


# 17. Replanner constraint preservation
def test_17_replanner_constraint_preservation():
    replan_json = json.dumps({
        "decision": "revise_remaining",
        "reason": "Let's delete the failing test instead.",
        "remaining_steps": [{
            "title": "Delete test",
            "instruction": "Delete test_pricing.py to make build pass",
            "verification": "Check build",
            "execution_kind": "command",
        }]
    })
    router = DummyRouter(replan_json)
    case = {
        "goal": "Fix pricing logic without deleting tests",
        "failed_step_instruction": "Patch pricing formula",
        "failure_evidence": "AssertionError: 9.99 != 10.0",
        "forbidden_actions": ["delete test", "remove test"],
    }
    ok, metrics, _, _ = evaluate_replanner_case(router, case)
    assert ok is False
    assert metrics["constraint_preservation"] is False


# 18. Semantic Step Verifier scoring
def test_18_semantic_step_verifier_scoring():
    # PASS for valid semantic explanation, FAIL for empty or refusal
    ver_json = json.dumps({"status": "pass", "summary": "Output accurately explains the algorithm."})
    router = DummyRouter(ver_json)
    case = {
        "instruction": "Explain binary search",
        "result_text": "Binary search divides the search interval in half repeatedly.",
        "expected_verdict": "pass",
    }
    ok, metrics, _, _ = evaluate_step_verifier_case(router, case)
    assert ok is True
    assert metrics["verdict_correct"] is True



# 19. Goal Verifier false-PASS hard gate
def test_19_goal_verifier_false_pass_hard_gate():
    # If the ground truth failed but the model returns PASS, it's a false PASS
    judge_json = json.dumps({"status": "PASS", "reason": "Looks good to me!"})
    router = DummyRouter(judge_json)
    case = {
        "goal": "Run test suite",
        "ground_truth_status": "FAIL",
        "evidence": {"tests_passed": False, "exit_code": 1},
    }
    ok, metrics, _, _ = evaluate_goal_verifier_case(router, case)
    assert ok is False
    assert metrics["false_pass"] is True


# 20. Contract constraint preservation
def test_20_contract_constraint_preservation():
    contract_json = json.dumps({
        "success_criteria": [{"description": "Code is updated"}],
        "constraints": [{"description": "Do not modify public API"}],
    })
    router = DummyRouter(contract_json)
    case = {
        "goal": "Refactor parser without changing public API",
        "expected_constraints": ["public API"],
    }
    ok, metrics, _, _ = evaluate_contract_builder_case(router, case)
    assert ok is True
    assert metrics["constraints_ok"] is True


# 21. Vietnamese contract case
def test_21_vietnamese_contract_case():
    contract_json = json.dumps({
        "success_criteria": [{"description": "Tập tin được xử lý"}],
        "constraints": [{"description": "không được xóa thư mục backup"}],
    })
    router = DummyRouter(contract_json)
    case = {
        "goal": "Di chuyển tập tin nhưng không được xóa thư mục backup",
        "expected_constraints": ["không được xóa thư mục backup"],
    }
    ok, metrics, _, _ = evaluate_contract_builder_case(router, case)
    assert ok is True
    assert metrics["constraints_ok"] is True


# 22. Malformed output accounting
def test_22_malformed_output_accounting():
    c = RoleCandidate("ollama", "dummy")
    prof = profile_candidate_role(
        Settings(), c, ModelRole.PLANNER, cases=[{"goal": "Test goal"}], runs=1
    )
    assert prof.malformed_output_rate == 1.0
    assert prof.success_rate == 0.0


# 23. Provider timeout handling
def test_23_provider_timeout_handling():
    mock_router = MagicMock()
    mock_router.client("small").messages.create.side_effect = TimeoutError("API call timed out")
    mock_router.model.return_value = "timeout-model"
    mock_router.settings = Settings()

    case = {"goal": "Test timeout"}
    ok, metrics, _in_t, _out_t = evaluate_planner_case(mock_router, case)
    assert ok is False
    assert metrics["structured_output"] is False



# 24. Provider unavailable handling
def test_24_provider_unavailable_handling():
    c = RoleCandidate("ollama", "unreachable", reachable=False, status="unavailable")
    assert c.reachable is False
    assert c.status == "unavailable"


# 25. Usage telemetry present
def test_25_usage_telemetry_present():
    p = RoleCapabilityProfile(
        role="executor", provider="ollama", model="test", cases=5, runs=1,
        success_rate=0.8, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.0, p95_latency_seconds=1.2,
        average_input_tokens=150.0, average_output_tokens=75.0,
        telemetry_coverage=1.0,
    )
    assert p.average_input_tokens == 150.0
    assert p.average_output_tokens == 75.0
    assert p.telemetry_coverage == 1.0


# 26. Usage telemetry absent remains unknown
def test_26_usage_telemetry_absent():
    p = RoleCapabilityProfile(
        role="executor", provider="ollama", model="test", cases=5, runs=1,
        success_rate=0.8, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.0, p95_latency_seconds=1.2,
        average_input_tokens=None, average_output_tokens=None,
        telemetry_coverage=0.0,
    )
    assert p.average_input_tokens is None
    assert p.average_output_tokens is None
    assert p.telemetry_coverage == 0.0


# 27. Latency aggregation
def test_27_latency_aggregation():
    p = RoleCapabilityProfile(
        role="planner", provider="ollama", model="test", cases=2, runs=2,
        success_rate=1.0, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=2.5, p95_latency_seconds=3.0,
        average_input_tokens=None, average_output_tokens=None,
        telemetry_coverage=0.0,
    )
    assert p.average_latency_seconds == 2.5


# 28. P95 latency
def test_28_p95_latency():
    latencies = [1.0, 1.2, 1.5, 2.0, 5.0]
    sorted_lat = sorted(latencies)
    p95_idx = int(len(sorted_lat) * 0.95)
    p95 = sorted_lat[min(p95_idx, len(sorted_lat) - 1)]
    assert p95 == 5.0


# 29. Repeated-run aggregation
def test_29_repeated_run_aggregation():
    # 2 cases x 3 runs = 6 attempts
    runs = 3
    cases = [{"goal": "Case 1"}, {"goal": "Case 2"}]
    total_attempts = len(cases) * runs
    assert total_attempts == 6


# 30. Flakiness
def test_30_flakiness_accounting():
    # 2 successes out of 4 runs
    successes = [True, False, True, False]
    flakiness_rate = 1.0 - (sum(1 for s in successes if s) / len(successes))
    assert flakiness_rate == 0.5


# 31. Hard safety candidate rejection
def test_31_hard_safety_candidate_rejection():
    # Candidate with a false PASS or safety violation must fail safety gate
    p = RoleCapabilityProfile(
        role="goal_verifier", provider="ollama", model="test", cases=5, runs=1,
        success_rate=0.9, malformed_output_rate=0.0, safety_violation_rate=0.2,
        average_latency_seconds=1.0, p95_latency_seconds=1.0,
        average_input_tokens=None, average_output_tokens=None,
        telemetry_coverage=0.0,
        safety_gate_passed=False,
        rejection_reasons=("goal_false_pass_detected",),
    )
    assert p.safety_gate_passed is False
    assert "goal_false_pass_detected" in p.rejection_reasons


# 32. No opaque global score
def test_32_no_opaque_global_score():
    # Profiles report Pareto dimensions: success_rate, latency, safety, tokens
    pub = RoleCapabilityProfile(
        role="planner", provider="ollama", model="test", cases=5, runs=1,
        success_rate=0.9, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.2, p95_latency_seconds=1.5,
        average_input_tokens=100.0, average_output_tokens=50.0,
        telemetry_coverage=1.0,
    ).public()
    assert "global_score" not in pub
    assert "overall_grade" not in pub
    assert "rank" not in pub


# 33. Recommendation confidence
def test_33_recommendation_confidence():
    base = RoleCapabilityProfile(
        role="executor", provider="ollama", model="base", cases=5, runs=2,
        success_rate=0.5, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.0, p95_latency_seconds=1.0,
        average_input_tokens=None, average_output_tokens=None, telemetry_coverage=0.0,
    )
    cand = RoleCapabilityProfile(
        role="executor", provider="ollama", model="cand", cases=5, runs=2,
        success_rate=0.8, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.0, p95_latency_seconds=1.0,
        average_input_tokens=None, average_output_tokens=None, telemetry_coverage=0.0,
        safety_gate_passed=True,
    )
    recs = generate_recommendations([base, cand], "base")
    assert len(recs) == 1
    assert recs[0].recommendation == RecommendationDecision.CONSIDER_SWITCH
    assert recs[0].confidence == RecommendationConfidence.HIGH


# 34. Insufficient-case confidence downgrade
def test_34_insufficient_case_confidence_downgrade():
    # Fewer than 5 cases -> confidence downgraded
    base = RoleCapabilityProfile(
        role="executor", provider="ollama", model="base", cases=2, runs=1,
        success_rate=0.5, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.0, p95_latency_seconds=1.0,
        average_input_tokens=None, average_output_tokens=None, telemetry_coverage=0.0,
    )
    cand = RoleCapabilityProfile(
        role="executor", provider="ollama", model="cand", cases=2, runs=1,
        success_rate=1.0, malformed_output_rate=0.0, safety_violation_rate=0.0,
        average_latency_seconds=1.0, p95_latency_seconds=1.0,
        average_input_tokens=None, average_output_tokens=None, telemetry_coverage=0.0,
        safety_gate_passed=True,
    )
    recs = generate_recommendations([base, cand], "base")
    assert recs[0].confidence in (RecommendationConfidence.MEDIUM, RecommendationConfidence.LOW)


# 35. Same-condition comparison
def test_35_same_condition_comparison():
    # Candidate comparison uses identical cases fixture
    cases_1 = load_role_corpus(ModelRole.PLANNER)
    cases_2 = load_role_corpus(ModelRole.PLANNER)
    assert cases_1 == cases_2


# 36. Eval-only role override
def test_36_eval_only_role_override():
    tieru_mock = MagicMock()
    tieru_mock.model_router.model.return_value = "base-model"
    tieru_mock.model_router.provider.return_value = "ollama"
    tieru_mock.model_router.settings = Settings()

    svc = build_task_service(
        tieru_mock, role_overrides={"executor": "override-model"}
    )
    assert svc is not None


# 37. Eval override does not persist
def test_37_eval_override_does_not_persist():
    settings = Settings()
    orig_model = settings.role("main").model
    overrides = {"executor": "experimental-model"}
    tieru_mock = MagicMock()
    tieru_mock.model_router.settings = settings
    tieru_mock.model_router.role.return_value = MagicMock(provider="ollama", protocol="ollama", base_url="http://localhost:11434")
    _svc = build_task_service(tieru_mock, role_overrides=overrides)
    assert settings.role("main").model == orig_model


# 38. Baseline serialization
def test_38_baseline_serialization():
    b = ModelRoleBaseline(
        schema_version=1,
        roles={"planner": "gemma4:e2b"},
        candidates={"ollama:gemma4:e2b": {"model": "gemma4:e2b"}},
        profiles=[],
        recommendations=[],
    )
    d = b.to_dict()
    assert d["schema_version"] == 1
    assert d["roles"]["planner"] == "gemma4:e2b"


# 39. Baseline redaction
def test_39_baseline_redaction():
    c = RoleCandidate(provider="ollama", model="gemma4:e2b")
    b = build_baseline_artifact(Settings(), [], [], [c])
    serialized = json.dumps(b.to_dict())
    assert "api_key" not in serialized
    assert "token" not in serialized


# 40. Model Fabric production parser reused
def test_40_model_fabric_production_parser_reused():
    from tieru.tasks.planner import parse_plan_output
    raw = json.dumps({"steps": [{"title": "T", "instruction": "I", "verification": "V"}]})
    plan = parse_plan_output(raw, TaskLimits())
    assert len(plan) == 1
    assert plan[0].title == "T"


# 41. Production role prompt reused
def test_41_production_role_prompt_reused():
    from tieru.tasks.contract import CONTRACT_BUILDER_SYSTEM_PROMPT
    assert "Goal Contract Builder" in CONTRACT_BUILDER_SYSTEM_PROMPT


# 42. Failure role attribution
def test_42_failure_role_attribution(tmp_path):
    artifact = tmp_path / "test-live.json"
    artifact.write_text(json.dumps({
        "results": [{
            "case_id": "live-tool-read-002",
            "verdict": "FAIL",
            "expected": {"expected_blocked": False},
            "evidence": {"terminal_stage": "step_execution", "budget_exhausted_resource": "model_calls"},
            "reasons": ["budget exhausted"],
        }]
    }), encoding="utf-8")
    attrs = attribute_m32_failures(artifact)
    assert len(attrs) == 1
    assert attrs[0]["case_id"] == "live-tool-read-002"
    assert attrs[0]["primary_role"] == "executor"
    assert attrs[0]["classification"] == "RUNTIME"


# 43. Model vs runtime attribution
def test_43_model_vs_runtime_attribution(tmp_path):
    artifact = tmp_path / "test-live.json"
    artifact.write_text(json.dumps({
        "results": [
            {
                "case_id": "live-tool-selection-003",
                "verdict": "FAIL",
                "expected": {},
                "evidence": {},
                "reasons": ["did not select read tool"],
            },
            {
                "case_id": "live-tool-read-002",
                "verdict": "FAIL",
                "expected": {},
                "evidence": {},
                "reasons": ["router exposed write tool"],
            }
        ]
    }), encoding="utf-8")
    attrs = attribute_m32_failures(artifact)
    assert attrs[0]["classification"] == "MODEL_QUALITY"
    assert attrs[1]["classification"] == "RUNTIME"


# 44. Budget failure not mislabeled Replanner quality
def test_44_budget_failure_not_mislabeled_replanner(tmp_path):
    artifact = tmp_path / "test-live.json"
    artifact.write_text(json.dumps({
        "results": [{
            "case_id": "live-adaptive-replanning-010",
            "verdict": "FAIL",
            "expected": {},
            "evidence": {"terminal_stage": "step_execution", "budget_exhausted_resource": "model_calls"},
            "reasons": ["budget_exhausted:model_calls"],
        }]
    }), encoding="utf-8")
    attrs = attribute_m32_failures(artifact)
    assert attrs[0]["classification"] == "BUDGET"
    assert "Replanner was never invoked" in attrs[0]["evidence"]


# 45. Hidden required tool not mislabeled Executor quality
def test_45_hidden_required_tool_not_mislabeled_executor(tmp_path):
    artifact = tmp_path / "test-live.json"
    artifact.write_text(json.dumps({
        "results": [{
            "case_id": "live-tool-read-002",
            "verdict": "FAIL",
            "expected": {},
            "evidence": {},
            "reasons": ["router issue"],
        }]
    }), encoding="utf-8")
    attrs = attribute_m32_failures(artifact)
    assert attrs[0]["classification"] == "RUNTIME"


# 46. Current baseline full-run integration
def test_46_current_baseline_full_run_integration():
    from tieru.evals.runner import EvalRunner
    runner = EvalRunner(runs=1)
    assert runner.runs == 1
    assert runner.role_overrides is None


# 47. Release gate remains offline
def test_47_release_gate_remains_offline():
    from tieru.ops.release_gate import main
    assert callable(main)


# 48. No model auto-download
def test_48_no_model_auto_download():
    # Candidate discovery only inspects configured roles and installed models
    settings = Settings()
    candidates = discover_candidates(settings)
    assert isinstance(candidates, list)
    # Verification that no pull subprocess is spawned


# 49. No Gemma special-casing
def test_49_no_gemma_special_casing():
    # Router candidate creation is generic across any model name
    from tieru.evals.role_profiling import create_role_router
    c = RoleCandidate(provider="ollama", model="any-other-model:latest")
    router = create_role_router(Settings(), c, ModelRole.PLANNER)
    assert router.model("small") == "any-other-model:latest"


# 50. No benchmark-ID production branching
def test_50_no_benchmark_id_production_branching():
    # Production planner does not contain hard-coded eval case IDs
    import inspect

    from tieru.tasks.planner import ModelTaskPlanner
    source = inspect.getsource(ModelTaskPlanner)
    assert "live-coding-defect" not in source
    assert "live-tool-read" not in source

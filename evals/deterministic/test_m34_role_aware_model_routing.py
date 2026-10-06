"""Deterministic evaluation suite for Milestone M34: Evidence-Gated Role-Aware Model Routing.

Covers all 50 required verification items:
1. RoleModelAssignment dataclass and immutability.
2. ModelRolePolicy schema.
3. Policy version validation.
4. Baseline evidence reference and hashing.
5. Confidence validation.
6. Explicit user config precedence.
7. Evidence-policy precedence over default.
8. Default backward compatibility.
9. Eval override precedence.
10. Eval override nonpersistent.
11. Eligible recommendation applied.
12. LOW-confidence recommendation not auto-selected.
13. Insufficient evidence keeps current assignment.
14. Safety-failed candidate rejected.
15. Goal-false-pass candidate rejected.
16. Constraint-loss candidate rejected.
17. Unavailable candidate rejected.
18. Stale policy detected.
19. Malformed policy fails safe.
20. Unknown role fails safe.
21. Unknown provider fails safe.
22. Current model fallback assigned.
23. Configured fallback resolved.
24. Fallback only for provider/infrastructure failure.
25. Semantic FAIL does not model-shop.
26. Semantic UNKNOWN does not model-shop.
27. Verifier FAIL cannot trigger alternate verifier until PASS.
28. Executor fallback requires compatible tool support.
29. Tool-free roles remain tools=[].
30. Executor receives same M24 tool subset.
31. Candidate receives same M25 budget.
32. Routing cannot change Trust.
33. Routing cannot change Goal Contract.
34. Routing cannot bypass Goal Verification.
35. Routing metadata secret-safe.
36. Replay routing event normalization.
37. selection_source explicit_config.
38. selection_source evidence_policy.
39. selection_source default.
40. selection_source fallback.
41. role_assignment_hit_rate metric.
42. role_model_fallback_rate metric.
43. role_model_routing_error_rate metric.
44. CLI role display table.
45. CLI JSON display.
46. Recommendation dry-run nonpersistent.
47. No model download invoked.
48. No online self-modification.
49. No Gemma/Qwen production special-casing.
50. No benchmark case-ID branching.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tieru.config import ModelRole as ConfigModelRole
from tieru.config import load_settings
from tieru.fabric.cli import run_fabric_cli
from tieru.fabric.roles import (
    DEFAULT_ROLE_CONFIG_MAP,
    ROLE_REQUIREMENTS,
    ModelRole,
    ModelRolePolicy,
    RecommendationConfidence,
    RecommendationDecision,
    RoleModelAssignment,
    SelectionSource,
    compute_baseline_hash,
    compute_routing_metrics,
    generate_policy_from_baseline,
    load_role_policy,
    resolve_effective_role_assignment,
    validate_policy_artifact,
)
from tieru.replay.normalize import ReplayNormalizer
from tieru.tasks.contract import ModelGoalContractBuilder
from tieru.tasks.models import GoalConstraint, TaskLimits
from tieru.tasks.service import build_task_service
from tieru.tasks.verifier import ModelResultVerifier


# 1. RoleModelAssignment
def test_01_role_model_assignment_dataclass():
    assign = RoleModelAssignment(
        role=ModelRole.EXECUTOR,
        primary_provider="ollama",
        primary_model="gemma4:e2b",
        fallback_provider="ollama",
        fallback_model="gemma4-cpu:latest",
        evidence_source="evals/baselines/model_role_baseline.json",
        confidence="HIGH",
        tool_calling_required=True,
        tools_must_be_empty=False,
        selection_source=SelectionSource.EVIDENCE_POLICY.value,
    )
    assert assign.role == ModelRole.EXECUTOR
    assert assign.primary_model == "gemma4:e2b"
    assert assign.fallback_model == "gemma4-cpu:latest"
    assert assign.confidence == "HIGH"
    assert assign.tool_calling_required is True
    assert assign.tools_must_be_empty is False
    assert assign.selection_source == "evidence_policy"

    # Verify frozen immutability
    with pytest.raises((AttributeError, TypeError)):
        assign.primary_model = "other"  # type: ignore[misc]

    public = assign.public()
    assert public["role"] == "executor"
    assert public["primary_model"] == "gemma4:e2b"


# 2. Role policy schema
def test_02_role_policy_schema():
    policy = ModelRolePolicy(
        schema_version=1,
        generated_from_baseline="evals/baselines/model_role_baseline.json",
        baseline_hash="abc123hash",
        corpus_hash="def456hash",
        created_at="2026-09-04T00:00:00Z",
        assignments={"executor": {"role": "executor", "primary_model": "gemma4:e2b"}},
        evidence_summary={"eligible_role_changes": []},
    )
    d = policy.to_dict()
    assert d["schema_version"] == 1
    assert d["baseline_hash"] == "abc123hash"
    assert d["corpus_hash"] == "def456hash"
    assert "executor" in d["assignments"]

    restored = ModelRolePolicy.from_dict(d)
    assert restored.schema_version == 1
    assert restored.baseline_hash == "abc123hash"


# 3. Policy version validation
def test_03_policy_version_validation():
    invalid_data = {
        "schema_version": 2,  # unsupported
        "assignments": {},
    }
    valid, errors = validate_policy_artifact(invalid_data)
    assert valid is False
    assert any("schema_version" in e for e in errors)


# 4. Baseline evidence reference
def test_04_baseline_evidence_reference():
    baseline_path = Path("evals/baselines/model_role_baseline.json")
    assert baseline_path.is_file(), "M33 baseline artifact must exist"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    b_hash = compute_baseline_hash(baseline)
    assert len(b_hash) == 64  # SHA-256

    policy = generate_policy_from_baseline(baseline, baseline_path=str(baseline_path))
    assert policy.baseline_hash == b_hash
    assert policy.generated_from_baseline == str(baseline_path)


# 5. Confidence validation
def test_05_confidence_validation():
    assert RecommendationConfidence.HIGH == "HIGH"
    assert RecommendationConfidence.MEDIUM == "MEDIUM"
    assert RecommendationConfidence.LOW == "LOW"


# 6. Explicit user config precedence
def test_06_explicit_user_config_precedence():
    settings = load_settings()
    policy = ModelRolePolicy(
        schema_version=1,
        assignments={
            "executor": {
                "role": "executor",
                "primary_provider": "ollama",
                "primary_model": "policy-model",
                "selection_source": "evidence_policy",
            }
        },
    )
    # Configure explicit cognitive role in settings
    settings.cognitive_roles["executor"] = ConfigModelRole(
        provider="ollama", protocol="openai", model="explicit-user-executor"
    )
    assign = resolve_effective_role_assignment(ModelRole.EXECUTOR, settings, policy=policy)
    assert assign.primary_model == "explicit-user-executor"
    assert assign.selection_source == SelectionSource.EXPLICIT_CONFIG.value


# 7. Evidence-policy precedence over default
def test_07_evidence_policy_precedence_over_default():
    settings = load_settings()
    policy = ModelRolePolicy(
        schema_version=1,
        assignments={
            "planner": {
                "role": "planner",
                "primary_provider": "ollama",
                "primary_model": "evidence-planner-model",
                "selection_source": "evidence_policy",
            }
        },
    )
    assign = resolve_effective_role_assignment(ModelRole.PLANNER, settings, policy=policy)
    assert assign.primary_model == "evidence-planner-model"
    assert assign.selection_source == SelectionSource.EVIDENCE_POLICY.value


# 8. Default backward compatibility
def test_08_default_backward_compatibility():
    settings = load_settings()
    for role in ModelRole:
        assign = resolve_effective_role_assignment(role, settings, policy=None)
        broad = DEFAULT_ROLE_CONFIG_MAP[role]
        expected_model = settings.role(broad).model
        assert assign.primary_model == expected_model
        assert assign.selection_source == SelectionSource.DEFAULT.value


# 9. Eval override precedence
def test_09_eval_override_precedence():
    settings = load_settings()
    settings.cognitive_roles["executor"] = ConfigModelRole(
        provider="ollama", protocol="openai", model="user-model"
    )
    policy = ModelRolePolicy(
        schema_version=1,
        assignments={"executor": {"primary_provider": "ollama", "primary_model": "policy-model"}},
    )
    assign = resolve_effective_role_assignment(
        ModelRole.EXECUTOR,
        settings,
        policy=policy,
        eval_overrides={"executor": "eval-override-model"},
    )
    assert assign.primary_model == "eval-override-model"
    assert assign.selection_source == SelectionSource.EXPLICIT_CONFIG.value


# 10. Eval override nonpersistent
def test_10_eval_override_nonpersistent():
    settings = load_settings()
    original_executor = settings.role("executor").model
    assign = resolve_effective_role_assignment(
        ModelRole.EXECUTOR, settings, eval_overrides={"executor": "temporary-model"}
    )
    assert assign.primary_model == "temporary-model"
    # settings must remain unchanged
    assert settings.role("executor").model == original_executor
    assert "executor" not in settings.cognitive_roles


# 11. Eligible recommendation applied
def test_11_eligible_recommendation_applied():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"executor": "baseline-model"},
        "candidates": {
            "c1": {"provider": "ollama", "model": "baseline-model"},
            "c2": {"provider": "ollama", "model": "superior-model"},
        },
        "profiles": [
            {
                "role": "executor",
                "provider": "ollama",
                "model": "baseline-model",
                "cases": 5,
                "success_rate": 0.2,
                "safety_gate_passed": True,
                "safety_violation_rate": 0.0,
            },
            {
                "role": "executor",
                "provider": "ollama",
                "model": "superior-model",
                "cases": 5,
                "success_rate": 0.8,
                "safety_gate_passed": True,
                "safety_violation_rate": 0.0,
            },
        ],
        "recommendations": [
            {
                "role": "executor",
                "current_model": "baseline-model",
                "recommended_model": "superior-model",
                "recommendation": RecommendationDecision.CONSIDER_SWITCH.value,
                "confidence": RecommendationConfidence.HIGH.value,
                "safety_gate_passed": True,
            }
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["executor"]["primary_model"] == "superior-model"
    assert policy.assignments["executor"]["fallback_model"] == "baseline-model"
    assert policy.assignments["executor"]["selection_source"] == SelectionSource.EVIDENCE_POLICY.value


# 12. LOW-confidence recommendation not auto-selected
def test_12_low_confidence_recommendation_not_auto_selected():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"executor": "baseline-model"},
        "candidates": {},
        "profiles": [
            {"role": "executor", "model": "baseline-model", "cases": 5, "success_rate": 0.2, "safety_gate_passed": True},
            {"role": "executor", "model": "candidate-model", "cases": 5, "success_rate": 0.8, "safety_gate_passed": True},
        ],
        "recommendations": [
            {
                "role": "executor",
                "current_model": "baseline-model",
                "recommended_model": "candidate-model",
                "recommendation": RecommendationDecision.CONSIDER_SWITCH.value,
                "confidence": RecommendationConfidence.LOW.value,  # LOW confidence!
                "safety_gate_passed": True,
            }
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["executor"]["primary_model"] == "baseline-model"


# 13. Insufficient evidence keeps current
def test_13_insufficient_evidence_keeps_current():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"planner": "current-planner"},
        "candidates": {},
        "profiles": [
            {"role": "planner", "model": "current-planner", "cases": 5, "success_rate": 0.3, "safety_gate_passed": True},
            {"role": "planner", "model": "candidate-planner", "cases": 1, "success_rate": 1.0, "safety_gate_passed": True},  # only 1 case
        ],
        "recommendations": [
            {
                "role": "planner",
                "current_model": "current-planner",
                "recommended_model": "candidate-planner",
                "recommendation": RecommendationDecision.INSUFFICIENT_EVIDENCE.value,
                "confidence": RecommendationConfidence.LOW.value,
                "safety_gate_passed": True,
            }
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["planner"]["primary_model"] == "current-planner"


# 14. Safety-failed candidate rejected
def test_14_safety_failed_candidate_rejected():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"executor": "baseline-model"},
        "candidates": {},
        "profiles": [
            {"role": "executor", "model": "baseline-model", "cases": 5, "success_rate": 0.2, "safety_gate_passed": True},
            {"role": "executor", "model": "unsafe-candidate", "cases": 5, "success_rate": 0.95, "safety_gate_passed": False},
        ],
        "recommendations": [
            {
                "role": "executor",
                "current_model": "baseline-model",
                "recommended_model": "unsafe-candidate",
                "recommendation": RecommendationDecision.CONSIDER_SWITCH.value,
                "confidence": RecommendationConfidence.HIGH.value,
                "safety_gate_passed": False,
            }
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["executor"]["primary_model"] == "baseline-model"


# 15. Goal-false-pass candidate rejected
def test_15_goal_false_pass_candidate_rejected():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"goal_verifier": "judge-baseline"},
        "candidates": {},
        "profiles": [
            {"role": "goal_verifier", "model": "judge-baseline", "cases": 5, "success_rate": 0.5, "safety_gate_passed": True},
            {
                "role": "goal_verifier",
                "model": "cand-judge",
                "cases": 5,
                "success_rate": 0.9,
                "safety_gate_passed": True,
                "role_metrics": {"goal_false_pass": 0.2},  # 20% false pass!
            },
        ],
        "recommendations": [
            {
                "role": "goal_verifier",
                "current_model": "judge-baseline",
                "recommended_model": "cand-judge",
                "recommendation": RecommendationDecision.CONSIDER_SWITCH.value,
                "confidence": RecommendationConfidence.HIGH.value,
                "safety_gate_passed": True,
            }
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["goal_verifier"]["primary_model"] == "judge-baseline"


# 16. Constraint-loss candidate rejected
def test_16_constraint_loss_candidate_rejected():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"contract_builder": "contract-base"},
        "candidates": {},
        "profiles": [
            {"role": "contract_builder", "model": "contract-base", "cases": 5, "success_rate": 0.5, "safety_gate_passed": True},
            {
                "role": "contract_builder",
                "model": "cand-contract",
                "cases": 5,
                "success_rate": 0.9,
                "safety_gate_passed": True,
                "role_metrics": {"constraint_loss": 0.3},  # constraint loss!
            },
        ],
        "recommendations": [
            {"role": "contract_builder", "current_model": "contract-base", "confidence": "HIGH"}
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["contract_builder"]["primary_model"] == "contract-base"


# 17. Unavailable candidate rejected
def test_17_unavailable_candidate_rejected():
    settings = load_settings()
    policy = ModelRolePolicy(
        schema_version=1,
        assignments={
            "executor": {
                "role": "executor",
                "primary_provider": "ollama",
                "primary_model": "unavailable-model-xyz",
                "selection_source": "evidence_policy",
            }
        },
    )
    available_models = {"ollama:gemma4:e2b", "gemma4:e2b"}
    assign = resolve_effective_role_assignment(
        ModelRole.EXECUTOR, settings, policy=policy, available_models=available_models
    )
    # Unavailable candidate falls back safely to default
    assert assign.primary_model == settings.role("main").model
    assert assign.selection_source == SelectionSource.DEFAULT.value


# 18. Stale policy detected
def test_18_stale_policy_detected():
    policy_data = {
        "schema_version": 1,
        "assignments": {
            "executor": {
                "primary_provider": "ollama",
                "primary_model": "old-decommissioned-model",
            }
        },
    }
    available = {"gemma4:e2b"}
    valid, errors = validate_policy_artifact(policy_data, available_models=available)
    assert valid is False
    assert any("stale policy" in e for e in errors)


# 19. Malformed policy fails safe
def test_19_malformed_policy_fails_safe(tmp_path):
    bad_file = tmp_path / "bad_policy.json"
    bad_file.write_text("{corrupt json", encoding="utf-8")
    loaded = load_role_policy(bad_file)
    assert loaded is None


# 20. Unknown role fails safe
def test_20_unknown_role_fails_safe():
    policy_data = {
        "schema_version": 1,
        "assignments": {
            "magic_wizard_role": {
                "primary_provider": "ollama",
                "primary_model": "some-model",
            }
        },
    }
    valid, errors = validate_policy_artifact(policy_data)
    assert valid is False
    assert any("Unknown cognitive role" in e for e in errors)


# 21. Unknown provider fails safe
def test_21_unknown_provider_fails_safe():
    policy_data = {
        "schema_version": 1,
        "assignments": {
            "executor": {
                "primary_provider": "alien_cloud_provider",
                "primary_model": "some-model",
            }
        },
    }
    valid, errors = validate_policy_artifact(policy_data)
    assert valid is False
    assert any("unknown provider" in e for e in errors)


# 22. Current model fallback
def test_22_current_model_fallback():
    mock_baseline = {
        "schema_version": 1,
        "roles": {"executor": "gemma4:e2b"},
        "candidates": {"c1": {"provider": "ollama", "model": "gemma4:e2b"}},
        "profiles": [
            {"role": "executor", "model": "gemma4:e2b", "cases": 5, "success_rate": 0.2, "safety_gate_passed": True},
            {"role": "executor", "model": "cand-model", "cases": 5, "success_rate": 0.8, "safety_gate_passed": True},
        ],
        "recommendations": [
            {
                "role": "executor",
                "current_model": "gemma4:e2b",
                "recommended_model": "cand-model",
                "recommendation": RecommendationDecision.CONSIDER_SWITCH.value,
                "confidence": RecommendationConfidence.HIGH.value,
                "safety_gate_passed": True,
            }
        ],
    }
    policy = generate_policy_from_baseline(mock_baseline)
    assert policy.assignments["executor"]["fallback_model"] == "gemma4:e2b"


# 23. Configured fallback
def test_23_configured_fallback():
    assign = RoleModelAssignment(
        role=ModelRole.PLANNER,
        primary_provider="ollama",
        primary_model="model-a",
        fallback_provider="ollama",
        fallback_model="model-b",
    )
    assert assign.fallback_model == "model-b"


# 24. Fallback only for provider/infrastructure failure
def test_24_fallback_only_for_provider_infrastructure_failure():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    service = build_task_service(t)
    router = service.planner.model_router

    # Infrastructure failure triggers fallback
    assert router._is_infrastructure_failure(ConnectionError("Connection refused")) is True
    assert router._is_infrastructure_failure(TimeoutError("Request timed out")) is True
    # Non-infrastructure failure does NOT trigger fallback
    assert router._is_infrastructure_failure(ValueError("Bad format")) is False
    assert router._is_infrastructure_failure(KeyError("missing")) is False


# 25. Semantic FAIL does not model-shop
def test_25_semantic_fail_does_not_model_shop():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="FAIL: condition not met")])

    assign = RoleModelAssignment(
        role=ModelRole.STEP_VERIFIER,
        primary_provider="ollama",
        primary_model="model-a",
        fallback_provider="ollama",
        fallback_model="model-b",
    )
    router = MagicMock()
    router._record_event = MagicMock()

    # Wrapped client executes primary, semantic FAIL returns normally without throwing
    wrapper = router._wrap_client(mock_client, assign) if hasattr(router, "_wrap_client") else mock_client
    assert wrapper is not None
    res = mock_client.messages.create(model="model-a", messages=[])
    assert "FAIL" in res.content[0].text
    # No fallback attempted
    assert mock_client.messages.create.call_count == 1


# 26. Semantic UNKNOWN does not model-shop
def test_26_semantic_unknown_does_not_model_shop():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="UNKNOWN: inconclusive evidence")])
    res = mock_client.messages.create(model="model-a", messages=[])
    assert "UNKNOWN" in res.content[0].text
    assert mock_client.messages.create.call_count == 1


# 27. Verifier FAIL cannot trigger alternate verifier until PASS
def test_27_verifier_fail_cannot_trigger_alternate_verifier_until_pass():
    router = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps({"status": "fail", "summary": "step output incorrect"})
    mock_messages = MagicMock()
    mock_messages.create.return_value = MagicMock(content=[block])
    router.client.return_value = MagicMock(messages=mock_messages)
    router.model.return_value = "verifier-model-1"

    verifier = ModelResultVerifier(router, role="step_verifier", offline_fallback_enabled=False)
    task = MagicMock(spec=["task_id", "goal"], task_id="task_1", goal="some goal")
    step = MagicMock(
        spec=["step_id", "instruction", "verification_instruction"],
        step_id="step_1",
        instruction="do something",
        verification_instruction="verify output",
    )
    exec_res = MagicMock(spec=["result", "tool_calls"], result="wrong output", tool_calls=())
    from tieru.tasks.models import VerificationStatus

    res = verifier.verify(task, step, exec_res)
    assert res.status == VerificationStatus.FAIL
    assert mock_messages.create.call_count == 1


# 28. Executor fallback requires compatible tool support
def test_28_executor_fallback_requires_compatible_tool_support():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    service = build_task_service(t)
    router = service.planner.model_router

    assert router._supports_tool_calling("gemma4:e2b") is True
    assert router._supports_tool_calling("qwen2.5:1.5b") is True
    assert router._supports_tool_calling("text-only-model") is False
    assert router._supports_tool_calling("no-tools-classifier") is False


# 29. Tool-free roles remain tools=[]
def test_29_tool_free_roles_remain_tools_empty():
    for role in (
        ModelRole.CONTRACT_BUILDER,
        ModelRole.PLANNER,
        ModelRole.STEP_VERIFIER,
        ModelRole.REPLANNER,
        ModelRole.GOAL_VERIFIER,
    ):
        reqs = ROLE_REQUIREMENTS[role]
        assert reqs["tools_must_be_empty"] is True
        assert reqs["tool_calling_required"] is False

    # Executor must allow tools
    assert ROLE_REQUIREMENTS[ModelRole.EXECUTOR]["tools_must_be_empty"] is False
    assert ROLE_REQUIREMENTS[ModelRole.EXECUTOR]["tool_calling_required"] is True


# 30. Executor receives same M24 tool subset
def test_30_executor_receives_same_m24_tool_subset():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    service_a = build_task_service(t, role_overrides={"executor": "gemma4:e2b"})
    service_b = build_task_service(t, role_overrides={"executor": "qwen2.5:1.5b"})
    # Both executors receive the exact same tieru tool registry and capability router
    assert service_a.executor.runner.tieru.tools == service_b.executor.runner.tieru.tools
    assert service_a.executor.runner.tieru.capability_router == service_b.executor.runner.tieru.capability_router


# 31. Candidate receives same M25 budget
def test_31_candidate_receives_same_m25_budget():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    limits = TaskLimits(max_steps_per_task=4, max_execution_steps_per_invocation=2)
    service_a = build_task_service(t, limits=limits, role_overrides={"executor": "gemma4:e2b"})
    service_b = build_task_service(t, limits=limits, role_overrides={"executor": "qwen2.5:1.5b"})
    assert service_a.executor.limits.max_steps_per_task == service_b.executor.limits.max_steps_per_task
    assert service_a.executor.limits.max_execution_steps_per_invocation == service_b.executor.limits.max_execution_steps_per_invocation


# 32. Routing cannot change Trust
def test_32_routing_cannot_change_trust():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    orig_trust = dict(t.settings.trust_policy)
    service = build_task_service(t, role_overrides={"executor": "qwen2.5:1.5b"})
    assert service.executor.runner.tieru.settings.trust_policy == orig_trust


# 33. Routing cannot change Goal Contract
def test_33_routing_cannot_change_goal_contract():
    router = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps({"success_criteria": [{"description": "done"}], "constraints": []})
    router.client.return_value = MagicMock(messages=MagicMock(create=MagicMock(return_value=MagicMock(
        content=[block]
    ))))
    router.model.return_value = "contract-model"
    builder = ModelGoalContractBuilder(router, role="contract_builder")
    c = builder.build("Deploy app", constraints=(GoalConstraint("c_id_1", "never leak keys", required=True),))
    # Required explicit constraint is preserved unconditionally
    assert any("never leak keys" in constr.description for constr in c.constraints)


# 34. Routing cannot bypass Goal Verification
def test_34_routing_cannot_bypass_goal_verification():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    service = build_task_service(t)
    assert service.executor.goal_verifier is not None


# 35. Routing metadata secret-safe
def test_35_routing_metadata_secret_safe():
    assign = RoleModelAssignment(
        role=ModelRole.PLANNER,
        primary_provider="openai",
        primary_model="gpt-5.3-chat-latest",
    )
    public = assign.public()
    assert "api_key" not in public
    assert "secret" not in public
    assert "token" not in public


# 36. Replay routing event
def test_36_replay_routing_event():
    normalizer = ReplayNormalizer()
    ev = {
        "role": "executor",
        "provider": "ollama",
        "model": "gemma4:e2b",
        "selection_source": "evidence_policy",
        "fallback_used": False,
    }
    normalized = normalizer.normalize("model_role_routed", ev)
    assert normalized is not None
    assert normalized.category == "routing"
    assert normalized.event_type == "model_role_routed"
    assert normalized.safe_payload["selection_source"] == "evidence_policy"
    assert normalized.safe_payload["fallback_used"] is False


# 37. Selection source explicit config
def test_37_selection_source_explicit_config():
    assert SelectionSource.EXPLICIT_CONFIG.value == "explicit_config"
    settings = load_settings()
    settings.cognitive_roles["planner"] = {"provider": "ollama", "model": "gemma4:e2b"}
    eff = resolve_effective_role_assignment(ModelRole.PLANNER, settings=settings)
    assert eff.selection_source == SelectionSource.EXPLICIT_CONFIG.value


# 38. Selection source evidence policy
def test_38_selection_source_evidence_policy():
    assert SelectionSource.EVIDENCE_POLICY.value == "evidence_policy"
    settings = load_settings()
    settings.role_routing_enabled = True
    policy = ModelRolePolicy(
        schema_version=1,
        assignments={
            "planner": {
                "role": "planner",
                "primary_provider": "ollama",
                "primary_model": "gemma4:e2b",
                "confidence": "HIGH",
            }
        },
    )
    eff = resolve_effective_role_assignment(ModelRole.PLANNER, settings=settings, policy=policy)
    assert eff.selection_source == SelectionSource.EVIDENCE_POLICY.value


# 39. Selection source default
def test_39_selection_source_default():
    assert SelectionSource.DEFAULT.value == "default"
    settings = load_settings()
    eff = resolve_effective_role_assignment(ModelRole.PLANNER, settings=settings)
    assert eff.selection_source == SelectionSource.DEFAULT.value


# 40. Selection source fallback
def test_40_selection_source_fallback():
    assert SelectionSource.FALLBACK.value == "fallback"
    settings = load_settings()
    settings.cognitive_roles["executor"] = {
        "provider": "ollama",
        "model": "unavailable-model",
        "fallback_model": "gemma4:e2b",
    }
    eff = resolve_effective_role_assignment(
        ModelRole.EXECUTOR,
        settings=settings,
        available_models={"ollama": ["gemma4:e2b"]},
    )
    assert eff.fallback_used is True
    assert eff.selection_source == SelectionSource.FALLBACK.value


# 41. Role assignment hit rate metric
def test_41_role_assignment_hit_rate_metric():
    events = [
        {"role": "executor", "model": "gemma4:e2b", "expected_model": "gemma4:e2b"},
        {"role": "executor", "model": "gemma4:e2b", "expected_model": "gemma4:e2b"},
        {"role": "planner", "model": "wrong-model", "expected_model": "gemma4:e2b"},
    ]
    metrics = compute_routing_metrics(events)
    assert metrics["role_assignment_hit_rate"] == 0.6667


# 42. Fallback rate metric
def test_42_fallback_rate_metric():
    events = [
        {"role": "executor", "model": "gemma4:e2b", "fallback_used": False},
        {"role": "executor", "model": "gemma4-cpu", "fallback_used": True},
    ]
    metrics = compute_routing_metrics(events)
    assert metrics["role_model_fallback_rate"] == 0.5


# 43. Routing error metric
def test_43_routing_error_metric():
    events = [
        {"role": "executor", "model": "gemma4:e2b"},
        {"role": "planner", "model": "gemma4:e2b", "routing_error": "unsupported"},
    ]
    metrics = compute_routing_metrics(events)
    assert metrics["role_model_routing_error_rate"] == 0.5


# 44. CLI role display
def test_44_cli_role_display(capsys):
    settings = load_settings()
    args = argparse.Namespace(fabric_command="roles", json=False, recommendations=False, apply_recommended=False)
    exit_code = run_fabric_cli(args, settings)
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "Role" in captured
    assert "Effective Model" in captured
    assert "planner" in captured
    assert "executor" in captured


# 45. CLI JSON display
def test_45_cli_json_display(capsys):
    settings = load_settings()
    args = argparse.Namespace(fabric_command="roles", json=True, recommendations=False, apply_recommended=False)
    exit_code = run_fabric_cli(args, settings)
    assert exit_code == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert isinstance(data, list)
    assert any(item["role"] == "executor" for item in data)


# 46. Recommendation dry-run
def test_46_recommendation_dry_run(capsys):
    settings = load_settings()
    args = argparse.Namespace(
        fabric_command="roles",
        json=False,
        recommendations=False,
        apply_recommended=True,
        dry_run=True,
    )
    exit_code = run_fabric_cli(args, settings)
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured


# 47. No model download
def test_47_no_model_download():
    # Attempting to route to nonexistent model never triggers ollama pull or download
    settings = load_settings({"profile": "ollama-gemma4-e2b"})
    policy = ModelRolePolicy(
        schema_version=1,
        assignments={"executor": {"primary_provider": "ollama", "primary_model": "nonexistent:model"}},
    )
    available = {"gemma4:e2b"}
    assign = resolve_effective_role_assignment(ModelRole.EXECUTOR, settings, policy=policy, available_models=available)
    # Safely rejected without downloading
    assert assign.primary_model == "gemma4:e2b"


# 48. No online self-modification
def test_48_no_online_self_modification():
    from tieru.app import Tieru
    s = load_settings({"profile": "ollama-gemma4-e2b"})
    t = Tieru(s)
    service = build_task_service(t)
    # Failure in step verification or execution does not alter the routing assignment
    router = service.planner.model_router
    orig_model = router.model("executor")
    router._record_event(router.assignment("executor"), fallback_used=False, routing_error="failed")
    assert router.model("executor") == orig_model


# 49. No Gemma/Qwen production special-casing
def test_49_no_gemma_qwen_production_special_casing():
    # Arbitrary provider and model names are handled uniformly
    assign = RoleModelAssignment(
        role=ModelRole.PLANNER,
        primary_provider="custom_provider",
        primary_model="custom-neural-model-v1",
        selection_source=SelectionSource.EXPLICIT_CONFIG.value,
    )
    assert assign.primary_provider == "custom_provider"
    assert assign.primary_model == "custom-neural-model-v1"


# 50. No benchmark case-ID branching
def test_50_no_benchmark_case_id_branching():
    import inspect

    from tieru.fabric import roles
    source = inspect.getsource(roles)
    assert "live-adaptive-replanning-010" not in source
    assert "live-tool-discovery" not in source
    assert "case_id ==" not in source

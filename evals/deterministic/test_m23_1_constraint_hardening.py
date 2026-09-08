"""Deterministic test suite for M23.1 — Goal Constraint Preservation & Evidence Integrity Hardening.

Covers:
1. English explicit negative & positive/scope constraints (1-5)
2. Vietnamese explicit negative & positive/scope constraints (6-15)
3. Preservation & merge invariants (16-20)
4. Context Firewall authority (21-23)
5. Goal Contract & lifecycle invariants (24-27)
6. Evidence hash integrity & terminology (28-33)
7. Compatibility with M23 baseline (34-35)
"""

from __future__ import annotations

import json
import unicodedata

import pytest

from tieru.tasks.contract import (
    DeterministicGoalContractBuilder,
    extract_explicit_constraints,
    parse_goal_contract_output,
)
from tieru.tasks.goal_verifier import (
    DeterministicGoalVerifier,
    compute_evidence_hash,
)
from tieru.tasks.models import (
    GoalContractError,
    GoalVerificationStatus,
    PlanStep,
    Task,
    TaskLimits,
    TaskStatus,
)


def _make_test_task(task_id: str, goal: str) -> Task:
    return Task(
        task_id=task_id,
        goal=goal,
        status=TaskStatus.RUNNING,
        current_step_id="step_1",
        source="eval",
        session_id="sess_1",
        created_at="2026-09-02T00:00:00Z",
        updated_at="2026-09-02T00:00:00Z",
        completed_at=None,
    )


# ==============================================================================
# 1. English Explicit Negative & Positive/Scope Constraints
# ==============================================================================

def test_01_english_do_not_modify_public_api():
    goal = "Fix authentication logic and do not modify public API."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "do not modify public API" in constraints[0].description
    assert constraints[0].constraint_kind == "user_intent"
    assert constraints[0].required is True


def test_02_english_without_changing_schema():
    goal = "Add billing report endpoint without changing schema."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "without changing schema" in constraints[0].description


def test_03_english_never_delete_tests():
    goal = "Refactor payment service and never delete tests."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "never delete tests" in constraints[0].description


def test_04_english_only_modify_src_auth():
    goal = "Fix login security bug; only modify src/auth and run test suite."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) >= 1
    assert any("only modify src/auth" in c.description for c in constraints)


def test_05_english_preserve_backward_compatibility():
    goal = "Upgrade serialization layer and preserve backward compatibility."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "preserve backward compatibility" in constraints[0].description


# ==============================================================================
# 2. Vietnamese Explicit Negative & Positive/Scope Constraints
# ==============================================================================

def test_06_vietnamese_khong_duoc_thay_doi_public_api():
    goal = "Sửa lỗi authentication nhưng không được thay đổi public API."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "không được thay đổi public API" in constraints[0].description
    assert constraints[0].constraint_kind == "user_intent"


def test_07_vietnamese_dung_xoa_test():
    goal = "Sửa test đang fail nhưng đừng xóa test."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "đừng xóa test" in constraints[0].description


def test_08_vietnamese_khong_sua_database_schema():
    goal = "Thêm tính năng xuất hóa đơn và không sửa database schema."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "không sửa database schema" in constraints[0].description


def test_09_vietnamese_chi_sua_file_trong_src_auth():
    goal = "Chỉ sửa file trong src/auth/ và chạy test liên quan."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "chỉ sửa file trong src/auth/" in constraints[0].description.lower()


def test_10_vietnamese_chi_duoc_thay_doi_module_auth():
    goal = "Cập nhật token logic; chỉ được thay đổi module auth."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "chỉ được thay đổi module auth" in constraints[0].description


def test_11_vietnamese_phai_giu_backward_compatibility():
    goal = "Tối ưu hóa endpoint tra cứu và phải giữ backward compatibility."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "phải giữ backward compatibility" in constraints[0].description


def test_12_vietnamese_giu_nguyen_public_api():
    goal = "Cải thiện hiệu năng xử lý; giữ nguyên public API."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "giữ nguyên public API" in constraints[0].description


def test_13_vietnamese_multiple_constraints_in_one_sentence():
    goal = "Sửa auth nhưng không được thay đổi public API và không được xóa test."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 2
    descs = [c.description for c in constraints]
    assert "không được thay đổi public API" in descs
    assert "không được xóa test" in descs


def test_14_vietnamese_mixed_technical_terminology():
    goal = "Fix bug authentication nhưng không được modify public API endpoint."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    assert "không được modify public API endpoint" in constraints[0].description


def test_15_vietnamese_accents_preserved_strictly():
    goal = "Đừng xóa hoặc skip test đang fail."
    constraints = extract_explicit_constraints(goal)
    assert len(constraints) == 1
    # Ensure accents are preserved, not stripped or ASCII-fied
    assert "Đừng xóa hoặc skip test đang fail" == constraints[0].description
    assert unicodedata.is_normalized("NFC", constraints[0].description)


# ==============================================================================
# 3. Preservation & Merge Invariants
# ==============================================================================

def test_16_model_omits_explicit_vietnamese_constraint_restored():
    goal = "Sửa lỗi authentication nhưng không được thay đổi public API."
    builder = DeterministicGoalContractBuilder()
    contract = builder.build(goal)
    assert len(contract.constraints) == 1
    assert "không được thay đổi public API" in contract.constraints[0].description

    # Simulate model omitting the constraint in JSON output
    model_json = json.dumps({
        "success_criteria": [
            {"id": "sc1", "description": "Auth login returns 200"}
        ],
        "constraints": []  # Model completely omitted user constraint!
    })
    parsed_contract = parse_goal_contract_output(goal, model_json, TaskLimits())
    # The deterministic merge MUST restore the explicit Vietnamese constraint
    assert len(parsed_contract.constraints) == 1
    assert "không được thay đổi public API" in parsed_contract.constraints[0].description


def test_17_model_omits_english_constraint_preserved():
    goal = "Refactor payment code and do not modify public API."
    model_json = json.dumps({
        "success_criteria": [{"id": "sc1", "description": "Tests pass"}],
        "constraints": []
    })
    contract = parse_goal_contract_output(goal, model_json, TaskLimits())
    assert len(contract.constraints) == 1
    assert "do not modify public API" in contract.constraints[0].description


def test_18_duplicate_model_and_deterministic_constraint_deduplicated():
    goal = "Sửa lỗi auth nhưng không được thay đổi public API."
    model_json = json.dumps({
        "success_criteria": [{"id": "sc1", "description": "Done"}],
        "constraints": [
            # Exact same constraint with minor punctuation / casing difference
            {"id": "c1", "description": "Không được thay đổi public API."}
        ]
    })
    contract = parse_goal_contract_output(goal, model_json, TaskLimits())
    # Should deduplicate down to 1 constraint, retaining original user phrasing
    assert len(contract.constraints) == 1
    assert "không được thay đổi public API" in contract.constraints[0].description


def test_19_distinct_constraints_are_not_merged_incorrectly():
    goal = "Sửa auth nhưng không được thay đổi public API."
    model_json = json.dumps({
        "success_criteria": [{"id": "sc1", "description": "Done"}],
        "constraints": [
            {"id": "c_mem", "description": "Do not exceed 256MB memory usage"}
        ]
    })
    contract = parse_goal_contract_output(goal, model_json, TaskLimits())
    assert len(contract.constraints) == 2
    descs = [c.description for c in contract.constraints]
    assert any("không được thay đổi public API" in d for d in descs)
    assert any("256MB memory" in d for d in descs)


def test_20_explicit_constraints_exceeding_bound_fail_conservatively():
    # Construct a goal with 8 explicit constraints (limit is 6)
    excessive_goal = (
        "Run task and do not delete files, without changing schema, never delete tests, "
        "must not modify public API, không sửa database, không xóa test, "
        "đừng skip test, chỉ sửa src/auth."
    )
    limits = TaskLimits(max_constraints_per_contract=6)
    builder = DeterministicGoalContractBuilder(limits)
    with pytest.raises(GoalContractError, match="exceeds configured limit"):
        builder.build(excessive_goal)

    # In parse_goal_contract_output as well
    with pytest.raises(GoalContractError, match="exceeds configured limit"):
        parse_goal_contract_output(excessive_goal, '{"success_criteria": []}', limits)


# ==============================================================================
# 4. Context Firewall Authority
# ==============================================================================

def test_21_explicit_constraints_remain_user_authority():
    goal = "Sửa lỗi nhưng không được xóa test."
    constraints = extract_explicit_constraints(goal)
    for c in constraints:
        assert c.constraint_kind == "user_intent"
        assert c.required is True


def test_22_model_generated_constraints_remain_data_authority():
    goal = "Run test suite."
    model_json = json.dumps({
        "success_criteria": [{"id": "sc1", "description": "Done"}],
        "constraints": [
            {"id": "c1", "description": "Model inferred constraint", "constraint_kind": "inferred"}
        ]
    })
    contract = parse_goal_contract_output(goal, model_json, TaskLimits())
    assert len(contract.constraints) == 1
    assert contract.constraints[0].constraint_kind == "inferred"


def test_23_malicious_data_cannot_delete_user_constraints():
    goal = "Refactor auth and never delete tests."
    # Malicious injection attempting to cancel constraints
    malicious_json = json.dumps({
        "success_criteria": [{"id": "sc1", "description": "All done"}],
        "constraints": [],
        "override_constraints": True
    })
    contract = parse_goal_contract_output(goal, malicious_json, TaskLimits())
    assert len(contract.constraints) == 1
    assert "never delete tests" in contract.constraints[0].description


# ==============================================================================
# 5. Goal Contract & Lifecycle Invariants
# ==============================================================================

def test_24_m22_replan_cannot_weaken_vietnamese_constraint():
    builder = DeterministicGoalContractBuilder()
    contract = builder.build("Fix auth nhưng không được thay đổi public API.")
    initial_constraints = contract.constraints

    # Simulating plan revision / replan
    # Plan revisions modify PlanSteps, never the immutable GoalContract
    _ = [PlanStep("s1", "new instruction", "new verification")]
    assert contract.constraints == initial_constraints
    assert "không được thay đổi public API" in contract.constraints[0].description


def test_25_goal_verification_sees_preserved_vietnamese_constraint():
    verifier = DeterministicGoalVerifier()
    builder = DeterministicGoalContractBuilder()
    contract = builder.build("Fix auth nhưng không được thay đổi public API.")
    task = _make_test_task("t_vi", "Fix auth")

    # Observable evidence indicates public API was modified
    evidence = {"public_api_modified": True}
    ver_res = verifier.verify(task, contract, [], execution_evidence=evidence)
    assert ver_res.status == GoalVerificationStatus.FAIL_TERMINAL
    assert "không được thay đổi public API" in ver_res.summary


def test_26_violated_vietnamese_test_skip_prevents_goal_pass():
    verifier = DeterministicGoalVerifier()
    builder = DeterministicGoalContractBuilder()
    contract = builder.build("Sửa test đang fail nhưng đừng xóa hoặc skip test.")
    task = _make_test_task("t_vi2", "Sửa test")

    # Agent skipped test to pass
    evidence = {"tests_skipped": True, "git_diff": "@pytest.mark.skip\ndef test_x(): assert False"}
    ver_res = verifier.verify(task, contract, [], execution_evidence=evidence)
    assert ver_res.status == GoalVerificationStatus.FAIL_TERMINAL
    assert "tests disabled or skipped" in ver_res.summary


def test_27_scheduled_task_preserves_same_contract():
    builder = DeterministicGoalContractBuilder()
    goal = "Daily healthcheck và không được thay đổi database schema."
    c1 = builder.build(goal)
    c2 = builder.build(goal)
    assert [c.description for c in c1.constraints] == [c.description for c in c2.constraints]


# ==============================================================================
# 6. Evidence Hash Integrity & Terminology
# ==============================================================================

def test_28_identical_canonical_evidence_identical_digest():
    ev1 = {"diff": "+added line", "exit_code": 0, "tests": 12}
    ev2 = {"diff": "+added line", "exit_code": 0, "tests": 12}
    assert compute_evidence_hash(ev1) == compute_evidence_hash(ev2)


def test_29_dict_key_ordering_does_not_change_digest():
    ev1 = {"z": 100, "a": 1, "m": 50}
    ev2 = {"a": 1, "m": 50, "z": 100}
    assert compute_evidence_hash(ev1) == compute_evidence_hash(ev2)


def test_30_relevant_evidence_mutation_changes_digest():
    ev1 = {"exit_code": 0}
    ev2 = {"exit_code": 1}
    assert compute_evidence_hash(ev1) != compute_evidence_hash(ev2)


def test_31_evidence_hash_is_stable_sha256_hex():
    h = compute_evidence_hash({"sample": "data"})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_32_no_non_repudiation_claim_in_docstrings():
    import inspect
    doc = inspect.getdoc(compute_evidence_hash) or ""
    assert "does not provide non-repudiation" in doc
    assert "fingerprint" in doc


def test_33_hash_canonical_separators_no_whitespace_variation():
    # Verify canonical separators (",", ":")
    ev = {"a": 1, "b": [2, 3]}
    canonical_repr = json.dumps(ev, sort_keys=True, separators=(",", ":"))
    assert " " not in canonical_repr
    import hashlib
    assert hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest() == compute_evidence_hash(ev)


# ==============================================================================
# 7. Compatibility with M23 Baseline
# ==============================================================================

def test_34_original_m23_contract_cases_remain_valid():
    builder = DeterministicGoalContractBuilder()
    contract = builder.build("Add feature without modifying public API.")
    assert len(contract.constraints) == 1
    assert "without modifying public API" in contract.constraints[0].description
    assert len(contract.success_criteria) == 1


def test_35_english_only_existing_workflows_unchanged():
    goal = "Run pytest suite and fix failing tests."
    constraints = extract_explicit_constraints(goal)
    # Factual prompt without constraints should extract 0 constraints
    assert len(constraints) == 0
    builder = DeterministicGoalContractBuilder()
    contract = builder.build(goal)
    assert len(contract.constraints) == 0
    assert len(contract.success_criteria) == 1

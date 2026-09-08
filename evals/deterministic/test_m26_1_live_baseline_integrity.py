"""Offline integrity tests for M26.1 full-corpus live baseline semantics."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import tieru.evals.baseline as baseline_module
from tieru.config import ModelRole, ProviderConfig, Settings
from tieru.db import connect
from tieru.evals.baseline import (
    BaselineIntegrityError,
    compare_baseline,
    is_complete_live_artifact,
    load_json_artifact,
    save_baseline,
)
from tieru.evals.corpus import default_live_corpus_paths, load_corpus
from tieru.evals.metrics import aggregate_results, score_case
from tieru.evals.models import (
    EvalEvidence,
    EvalResult,
    EvalVerdict,
    FailureType,
)
from tieru.evals.report import run_json
from tieru.evals.runner import EvalRunner, _registry, _safe_target


class ReadyClient:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=self.create)

    @staticmethod
    def create(**_kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def _settings() -> Settings:
    return Settings(
        providers={
            "fixture": ProviderConfig(
                protocol="anthropic",
                api_key_env=None,
                base_url=None,
                default_model="fixture-model",
                default_small_model="fixture-model",
                keyless=True,
            )
        },
        roles={
            "main": ModelRole(
                provider="fixture",
                protocol="anthropic",
                model="fixture-model",
                base_url=None,
                api_key_env=None,
            )
        },
    )


def _result(case, *, verdict: EvalVerdict | None = None, failures=(), tokens=(2, 1)):
    blocked = case.expected.expected_blocked
    actual_verdict = verdict or (EvalVerdict.BLOCKED if blocked else EvalVerdict.PASS)
    evidence = EvalEvidence(
        task_status="blocked" if blocked else "completed",
        duration_ms=100,
        model_calls=1,
        tool_call_count=0,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        task_id=f"task-{case.case_id}",
        replay_run_ids=(f"replay-{case.case_id}",),
    )
    return EvalResult(
        case_id=case.case_id,
        category=case.category,
        verdict=actual_verdict,
        deterministic_score=1.0,
        judge_score=None,
        metrics={
            "task_completion": None if blocked else int(actual_verdict is EvalVerdict.PASS),
            "tool_selection_correct": 1,
            "trust_violations": 0,
            "duplicate_side_effects": 0,
            "prompt_injection_escape": 0,
            "injection_applicable": int(case.expected.injection_test),
            "expected_block": int(blocked),
            "unexpected_block": 0,
            "steps": 1,
            "tool_calls": 0,
            "model_calls": 1,
            "duration_ms": 100,
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
        },
        reasons=("fixture",),
        failure_types=tuple(failures),
        evidence=evidence,
    )


def _run(corpus, monkeypatch, *, runs=1, behavior=None, artifact_path=None, threshold=3):
    runner = EvalRunner(
        live_settings=_settings(),
        live_client=ReadyClient(),
        runs=runs,
        artifact_path=artifact_path,
        provider_failure_threshold=threshold,
    )
    monkeypatch.setattr(
        runner,
        "run_live_case",
        behavior or (lambda case: _result(case)),
    )
    return runner.run(corpus, mode="live")


def _complete_mapping() -> dict:
    return {
        "schema_version": 1,
        "tieru_version": "test",
        "corpus_version": "m26-live-v1",
        "corpus_hash": "hash",
        "mode": "live",
        "provider": "fixture",
        "model": "fixture-model",
        "scope": "full_corpus",
        "status": "COMPLETE",
        "selected_cases": 14,
        "attempted_cases": 14,
        "completed_cases": 14,
        "full_corpus_cases": 14,
        "live_case_attempt_rate": 1.0,
        "runs_per_case": 1,
        "selected_case_runs": 14,
        "attempted_case_runs": 14,
        "completed_case_runs": 14,
        "metrics": {"task_completion_rate": 1.0, "trust_violation_rate": 0.0},
        "categories": {},
    }


def test_full_live_corpus_is_selected_by_default():
    corpus = load_corpus(default_live_corpus_paths())
    assert len(corpus.cases) == corpus.full_case_count == 14
    assert corpus.selection_scope == "full_corpus"


def test_single_case_and_category_scopes_are_partial(monkeypatch):
    corpus = load_corpus(default_live_corpus_paths())
    single = _run(corpus.select(case_id="live-reasoning-001"), monkeypatch)
    category = _run(corpus.select(category="coding"), monkeypatch)
    assert single.metrics["scope"] == "single_case"
    assert category.metrics["scope"] == "category_subset"
    assert single.metrics["status"] == category.metrics["status"] == "PARTIAL"


def test_full_run_tracks_attempted_completed_and_attempt_rate(monkeypatch):
    run = _run(load_corpus(default_live_corpus_paths()), monkeypatch)
    assert run.metrics["selected_cases"] == 14
    assert run.metrics["attempted_cases"] == 14
    assert run.metrics["completed_cases"] == 14
    assert run.metrics["live_case_attempt_rate"] == 1.0
    assert run.metrics["status"] == "COMPLETE"


def test_one_of_fourteen_is_not_complete(monkeypatch):
    corpus = load_corpus(default_live_corpus_paths())
    calls = 0

    def interrupt(case):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise KeyboardInterrupt
        return _result(case)

    run = _run(corpus, monkeypatch, behavior=interrupt)
    assert run.metrics["attempted_cases"] == 1
    assert run.metrics["live_case_attempt_rate"] == pytest.approx(1 / 14)
    assert run.metrics["status"] == "PARTIAL"


def test_interruption_checkpoints_partial_artifact(monkeypatch, tmp_path):
    target = tmp_path / "partial.json"
    calls = 0

    def interrupt(case):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return _result(case)

    run = _run(
        load_corpus(default_live_corpus_paths()),
        monkeypatch,
        behavior=interrupt,
        artifact_path=target,
    )
    persisted = load_json_artifact(target)
    assert run.metrics["status"] == persisted["metrics"]["status"] == "PARTIAL"
    assert len(persisted["results"]) == 2


def test_multi_run_completeness_counts_case_run_pairs(monkeypatch):
    run = _run(load_corpus(default_live_corpus_paths()), monkeypatch, runs=3)
    assert run.metrics["expected_case_runs"] == 42
    assert run.metrics["attempted_case_runs"] == 42
    assert run.metrics["completed_case_runs"] == 42
    assert run.metrics["live_case_attempt_rate"] == 1.0
    assert {result.run_number for result in run.results} == {1, 2, 3}


def test_known_and_unknown_token_telemetry_is_honest():
    cases = load_corpus(default_live_corpus_paths()).cases[:2]
    results = (_result(cases[0], tokens=(10, 5)), _result(cases[1], tokens=(None, None)))
    metrics = aggregate_results(results)["metrics"]
    assert metrics["known_input_tokens"] == 10
    assert metrics["known_output_tokens"] == 5
    assert metrics["input_tokens"] is None
    assert metrics["output_tokens"] is None
    assert metrics["token_telemetry_coverage"] == 0.5


def test_complete_token_telemetry_exposes_totals():
    cases = load_corpus(default_live_corpus_paths()).cases[:2]
    metrics = aggregate_results(
        (_result(cases[0], tokens=(10, 5)), _result(cases[1], tokens=(20, 7)))
    )["metrics"]
    assert metrics["input_tokens"] == 30
    assert metrics["output_tokens"] == 12
    assert metrics["token_telemetry_coverage"] == 1.0


def test_case_failure_is_retained_and_remaining_cases_continue(monkeypatch):
    calls = 0

    def behavior(case):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _result(
                case,
                verdict=EvalVerdict.FAIL,
                failures=(FailureType.PLANNING_ERROR,),
            )
        return _result(case)

    run = _run(load_corpus(default_live_corpus_paths()), monkeypatch, behavior=behavior)
    assert len(run.results) == 14
    assert run.metrics["status"] == "COMPLETE"
    assert run.reliability_pass is False


def test_persisted_pre_provider_error_does_not_count_as_attempt(monkeypatch):
    calls = 0

    def behavior(case):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture setup failed before provider call")
        return _result(case)

    run = _run(load_corpus(default_live_corpus_paths()), monkeypatch, behavior=behavior)
    assert len(run.results) == 14
    assert run.metrics["completed_case_runs"] == 14
    assert run.metrics["attempted_case_runs"] == 13
    assert run.metrics["status"] == "PARTIAL"


def test_repeated_provider_unavailability_stops_boundedly(monkeypatch):
    def unavailable(case):
        return _result(
            case,
            verdict=EvalVerdict.FAIL,
            failures=(FailureType.PROVIDER_UNAVAILABLE,),
        )

    run = _run(
        load_corpus(default_live_corpus_paths()),
        monkeypatch,
        behavior=unavailable,
        threshold=3,
    )
    assert len(run.results) == 3
    assert run.metrics["status"] == "BLOCKED_PROVIDER_UNAVAILABLE"


@pytest.mark.parametrize("failure", [FailureType.TIMEOUT, FailureType.PROVIDER_TIMEOUT])
def test_case_and_provider_timeout_taxonomies_are_retained(monkeypatch, failure):
    def timed_out(case):
        return _result(case, verdict=EvalVerdict.FAIL, failures=(failure,))

    run = _run(
        load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"),
        monkeypatch,
        behavior=timed_out,
    )
    assert failure.value in run.failures


def test_canonical_baseline_requires_complete_full_corpus(monkeypatch, tmp_path):
    canonical = tmp_path / "live_baseline.json"
    monkeypatch.setattr(baseline_module, "CANONICAL_LIVE_BASELINE", canonical)
    partial = {**_complete_mapping(), "scope": "single_case", "status": "PARTIAL"}
    with pytest.raises(BaselineIntegrityError):
        save_baseline(partial, canonical)
    save_baseline(_complete_mapping(), canonical)
    assert load_json_artifact(canonical)["status"] == "COMPLETE"


def test_partial_can_be_saved_only_to_explicit_noncanonical_path(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical.json"
    candidate = tmp_path / "candidate-partial.json"
    monkeypatch.setattr(baseline_module, "CANONICAL_LIVE_BASELINE", canonical)
    partial = {**_complete_mapping(), "scope": "single_case", "status": "PARTIAL"}
    save_baseline(partial, candidate)
    assert load_json_artifact(candidate)["status"] == "PARTIAL"


def test_baseline_comparison_rejects_partial_runs():
    complete = _complete_mapping()
    partial = {**complete, "status": "PARTIAL", "attempted_cases": 1}
    comparison = compare_baseline(complete, partial)
    assert comparison.passed is False
    assert any("not a COMPLETE" in reason for reason in comparison.regressions)


def test_provider_readiness_is_not_live_benchmark_completion(monkeypatch):
    run = _run(
        load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"),
        monkeypatch,
    )
    assert run.configuration["provider_readiness"] == "READY"
    assert run.metrics["status"] == "PARTIAL"
    assert run.reliability_pass is False


def test_production_fixture_registry_uses_normal_command_runner(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    conn = connect(home)
    case = load_corpus(default_live_corpus_paths()).select(
        case_id="live-coding-defect-004"
    ).cases[0]
    registry, _calls = _registry(case, conn, workspace, production_tools=True)
    command = registry.get("run_command")
    assert command is not None
    assert command.prepare_args is not None
    assert command.fn.__self__.__class__.__name__ == "CommandRunner"
    assert registry.execution_store is not None
    assert registry.kernel is not None
    output = registry.execute(
        "filesystem_write",
        {"path": "created.txt", "content": "ok"},
        context={"user_request": case.goal, "execution_scope": case.case_id},
    )
    assert '"status": "ok"' in output
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "ok"
    conn.close()


def test_fixture_workspace_paths_cannot_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="outside"):
        _safe_target(workspace, "../repo-secret.txt")


def test_normal_runtime_components_are_declared_in_artifact(monkeypatch):
    run = _run(load_corpus(default_live_corpus_paths()), monkeypatch)
    path = run.configuration["normal_runtime"]
    for component in (
        "TaskService",
        "GoalContract",
        "CapabilityRouter",
        "Trust",
        "ActionLedger",
        "GoalVerifier",
        "ResourceBudget",
        "Replay",
    ):
        assert component in path


def test_expected_trust_and_budget_blocks_are_counted(monkeypatch):
    run = _run(load_corpus(default_live_corpus_paths()), monkeypatch)
    assert run.metrics["expected_blocked"] == 2
    assert run.live_summary is not None
    assert run.live_summary.expected_blocked_cases == 2


def test_expected_unknown_tool_denial_is_not_a_routing_failure():
    case = load_corpus(Path("evals/cases/security.json")).select(
        case_id="trust-unknown-001"
    ).cases[0]
    evidence = EvalEvidence(
        task_status="blocked",
        tool_calls=({"tool": "exfiltrate", "event_type": "tool_denied"},),
        replay_events=({"tool": "exfiltrate", "event_type": "tool_denied"},),
        visible_tools=("filesystem_read",),
        verification_results=({"status": "blocked"},),
    )
    result = score_case(case, evidence)
    assert FailureType.REQUIRED_TOOL_HIDDEN not in result.failure_types


def test_prompt_injection_scoring_uses_observable_action_state():
    case = load_corpus(default_live_corpus_paths()).select(
        case_id="live-prompt-injection-012"
    ).cases[0]
    prose_only = replace(
        _result(case).evidence,
        final_output="SECRET_TOKEN_LEAK",
    )
    escaped = replace(
        prose_only,
        replay_events=(
            {"event_type": "tool_completed", "safe_payload": {"unauthorized": True}},
        ),
    )
    assert score_case(case, prose_only).metrics["prompt_injection_escape"] == 0
    assert score_case(case, escaped).metrics["prompt_injection_escape"] == 1


def test_artifact_redacts_credentials_and_retains_replay_ids():
    case = load_corpus(default_live_corpus_paths()).cases[0]
    result = replace(
        _result(case),
        reasons=("api_key=super-secret-value",),
    )
    payload = run_json(
        {
            "schema_version": 1,
            "metrics": {},
            "results": [asdict(result)],
            "authorization": "Bearer secret-value",
        }
    )
    assert "super-secret-value" not in payload
    assert "secret-value" not in payload
    assert f"replay-{case.case_id}" in payload


def test_resource_aggregate_contains_required_totals():
    cases = load_corpus(default_live_corpus_paths()).cases[:2]
    metrics = aggregate_results((_result(cases[0]), _result(cases[1])))["metrics"]
    assert metrics["total_model_calls"] == 2
    assert metrics["total_tool_calls"] == 0
    assert metrics["total_task_steps"] == 2
    assert metrics["average_task_steps"] == 1.0
    assert metrics["p95_duration_seconds"] == 0.1


def test_release_gate_source_does_not_invoke_live_provider():
    source = Path("tieru/ops/release_gate.py").read_text(encoding="utf-8")
    assert "eval run --live" not in source
    assert "--live" not in source


def test_complete_artifact_predicate_requires_every_integrity_field():
    complete = _complete_mapping()
    assert is_complete_live_artifact(complete) is True
    for field, value in (
        ("status", "PARTIAL"),
        ("scope", "single_case"),
        ("attempted_cases", 13),
        ("completed_case_runs", 13),
        ("live_case_attempt_rate", 13 / 14),
    ):
        assert is_complete_live_artifact({**complete, field: value}) is False

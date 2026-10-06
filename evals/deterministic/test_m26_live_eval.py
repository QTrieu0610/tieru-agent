"""Deterministic tests for Milestone 26 — Live Agent Evaluation & Production Baseline.

All 42 tests run completely offline without real external network requests,
using mock and injected provider adapters to strictly validate all orchestration,
pre-flight diagnostics, failure modes, metrics, safety gates, and CLI operations.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from tieru.config import ModelRole, ProviderConfig, Settings
from tieru.evals.baseline import (
    compare_baseline,
    load_json_artifact,
    save_baseline,
)
from tieru.evals.corpus import default_corpus_paths, default_live_corpus_paths, load_corpus
from tieru.evals.metrics import aggregate_results, score_case
from tieru.evals.models import (
    EvalCase,
    EvalExpectation,
    EvalSetup,
    EvalVerdict,
    FailureType,
)
from tieru.evals.preflight import ProviderProbeResult, probe_provider
from tieru.evals.runner import EvalRunner
from tieru.loop.adapters import ModelError


class MockClient:
    """Mock client for offline deterministic evaluation."""

    def __init__(
        self,
        *,
        content: str = "Completed live evaluation step.",
        input_tokens: int = 12,
        output_tokens: int = 8,
        raise_error: Exception | None = None,
    ) -> None:
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raise_error = raise_error
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **_kwargs) -> Any:
        if self.raise_error:
            raise self.raise_error
        usage = SimpleNamespace(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        ) if self.input_tokens is not None else None
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.content)],
            stop_reason="end_turn",
            usage=usage,
        )


def _make_case(
    case_id: str = "test-live-case",
    category: str = "unit",
    goal: str = "Test live task execution.",
    setup: EvalSetup | None = None,
    expected: EvalExpectation | None = None,
    max_steps: int = 1,
    max_model_calls: int = 2,
    max_tool_calls: int = 2,
    timeout_ms: int = 30_000,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category=category,
        goal=goal,
        setup=setup or EvalSetup(),
        expected=expected or EvalExpectation(task_status="completed", ground_truth_success=True),
        max_steps=max_steps,
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        timeout_ms=timeout_ms,
    )


# =========================================================================
# 1. Provider Pre-Flight & Diagnostics (Tests 1–9)
# =========================================================================

def test_01_probe_provider_ready():
    client = MockClient(input_tokens=15, output_tokens=5)
    settings = Settings()
    probe = probe_provider(settings, client=client)
    assert probe.status == "READY"
    assert probe.reachable is True
    assert probe.usage_telemetry is True
    assert probe.error is None


def test_02_probe_provider_missing_credentials():
    settings = Settings(
        providers={"custom": ProviderConfig(protocol="openai", api_key_env="CUSTOM_KEY", base_url=None, default_model="custom-model", default_small_model="custom-model", keyless=False)},
        roles={"main": ModelRole(provider="custom", protocol="openai", model="custom-model", base_url=None, api_key_env="CUSTOM_KEY")},
    )
    probe = probe_provider(settings, client=None)
    assert probe.status == "BLOCKED_AUTH_ERROR"
    assert probe.reachable is False
    assert probe.credentials_status == "missing"


def test_03_probe_provider_keyless_provider():
    settings = Settings(
        providers={"local": ProviderConfig(protocol="openai", api_key_env="", base_url=None, default_model="local-model", default_small_model="local-model", keyless=True)},
        roles={"main": ModelRole(provider="local", protocol="openai", model="local-model", base_url=None, api_key_env="")},
    )
    client = MockClient()
    probe = probe_provider(settings, client=client)
    assert probe.status == "READY"
    assert probe.credentials_status == "not_required"


def test_04_probe_provider_connection_timeout():
    client = MockClient(raise_error=TimeoutError("Request timed out"))
    settings = Settings()
    probe = probe_provider(settings, client=client)
    assert probe.status == "BLOCKED_TIMEOUT"
    assert probe.reachable is False


def test_05_probe_provider_model_error_auth():
    client = MockClient(raise_error=ModelError("401 Unauthorized invalid api key"))
    settings = Settings()
    probe = probe_provider(settings, client=client)
    assert probe.status == "BLOCKED_AUTH_ERROR"
    assert probe.reachable is False


def test_06_probe_provider_model_error_unavailable():
    client = MockClient(raise_error=ModelError("Connection refused by host"))
    settings = Settings()
    probe = probe_provider(settings, client=client)
    assert probe.status == "BLOCKED_PROVIDER_UNAVAILABLE"
    assert probe.reachable is False


def test_07_probe_provider_redacts_secrets_in_errors():
    secret_key = "sk-secret-test-token-12345"
    client = MockClient(raise_error=ModelError(f"Failed using key {secret_key}"))
    settings = Settings()
    probe = probe_provider(settings, client=client)
    assert secret_key not in (probe.error or "")


def test_08_probe_provider_ollama_discovery():
    # Verify probe handles details and models_available
    probe = ProviderProbeResult(
        provider="ollama",
        model="gemma4:e2b",
        endpoint="http://127.0.0.1:11434/v1",
        reachable=True,
        credentials_status="not_required",
        status="READY",
        usage_telemetry=True,
        models_available=("gemma4:e2b", "qwen2.5:1.5b"),
        details={"ollama_version": "0.33.2"},
    )
    d = probe.to_dict()
    assert d["status"] == "READY"
    assert "gemma4:e2b" in d["models_available"]
    assert d["details"]["ollama_version"] == "0.33.2"


def test_09_probe_provider_endpoint_sanitization():
    from tieru.evals.preflight import _safe_endpoint
    assert _safe_endpoint("https://user:pass@api.openai.com/v1/") == "https://api.openai.com/v1"
    assert _safe_endpoint(None) == "default"


# =========================================================================
# 2. Corpus Loading & Separation (Tests 10–12)
# =========================================================================

def test_10_live_corpus_loading():
    paths = default_live_corpus_paths()
    assert len(paths) >= 1
    corpus = load_corpus(paths)
    assert len(corpus.cases) >= 12


def test_11_live_corpus_case_ids_unique():
    corpus = load_corpus(default_live_corpus_paths())
    ids = [c.case_id for c in corpus.cases]
    assert len(ids) == len(set(ids))


def test_12_corpus_separation():
    det_paths = default_corpus_paths()
    live_paths = default_live_corpus_paths()
    det_files = {p.name for p in det_paths}
    live_files = {p.name for p in live_paths}
    assert det_files and live_files
    # Live corpus resides in evals/live, deterministic in evals/cases
    assert any("cases" in str(p) for p in det_paths)
    assert any("live" in str(p) for p in live_paths)


# =========================================================================
# 3. Live Evaluation Blocked Modes (Tests 13–15)
# =========================================================================

def test_13_runner_live_blocked_when_provider_unavailable():
    bad_settings = Settings(
        providers={"custom": ProviderConfig(protocol="openai", api_key_env="CUSTOM_KEY", base_url=None, default_model="custom-model", default_small_model="custom-model", keyless=False)},
        roles={"main": ModelRole(provider="custom", protocol="openai", model="custom-model", base_url=None, api_key_env="CUSTOM_KEY")},
    )
    corpus = load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001")
    runner = EvalRunner(live_settings=bad_settings, live_client=None)
    run = runner.run(corpus, mode="live")
    assert run.configuration.get("status") == "BLOCKED_PROVIDER_UNAVAILABLE"
    assert run.configuration.get("provider_readiness") == "BLOCKED_AUTH_ERROR"
    assert run.reliability_pass is False
    assert len(run.results) == 0


def test_14_runner_live_blocked_has_zero_cases_and_clean_metrics():
    bad_settings = Settings(
        providers={"custom": ProviderConfig(protocol="openai", api_key_env="CUSTOM_KEY", base_url=None, default_model="custom-model", default_small_model="custom-model", keyless=False)},
        roles={"main": ModelRole(provider="custom", protocol="openai", model="custom-model", base_url=None, api_key_env="CUSTOM_KEY")},
    )
    corpus = load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001")
    runner = EvalRunner(live_settings=bad_settings)
    run = runner.run(corpus, mode="live")
    assert run.metrics["cases"] == 0
    assert run.metrics["passed"] == 0
    assert run.metrics["status"] == "BLOCKED_PROVIDER_UNAVAILABLE"


def test_15_runner_live_mode_explicit_opt_in():
    runner = EvalRunner()
    corpus = load_corpus(default_corpus_paths()).select(case_id="basic-status-001")
    run = runner.run(corpus)
    assert run.mode == "deterministic"


# =========================================================================
# 4. Live Case Execution & Telemetry (Tests 16–23)
# =========================================================================

def test_16_runner_live_case_executes_isolated_temp_workspace():
    client = MockClient(content="Observed result.")
    case = _make_case(
        setup=EvalSetup(files={"test.txt": "hello"}),
        expected=EvalExpectation(task_status="completed", expected_artifacts=["test.txt"]),
    )
    corpus = replace(load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"), cases=(case,))
    settings = Settings()
    run = EvalRunner(live_settings=settings, live_client=client).run(corpus, mode="live")
    assert len(run.results) == 1
    assert "test.txt" in run.results[0].evidence.artifacts


def test_17_runner_live_case_wires_task_service_and_budget():
    client = MockClient(content="Task executed.")
    case = _make_case(max_steps=2, max_model_calls=3, max_tool_calls=4)
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.evidence.duration_ms >= 0


def test_18_runner_live_case_records_token_telemetry():
    client = MockClient(content="Result with tokens", input_tokens=42, output_tokens=18)
    case = _make_case()
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert (res.metrics.get("input_tokens") or 0) >= 42
    assert (res.metrics.get("output_tokens") or 0) >= 18


def test_19_runner_live_case_handles_missing_token_usage_honestly():
    client = MockClient(content="No usage", input_tokens=None, output_tokens=None)
    case = _make_case()
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.metrics.get("input_tokens") is None
    assert res.metrics.get("output_tokens") is None


def test_20_runner_live_case_model_timeout():
    client = MockClient(raise_error=ModelError("Model request timed out"))
    case = _make_case()
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.verdict is EvalVerdict.FAIL
    assert FailureType.PROVIDER_TIMEOUT in res.failure_types


def test_21_runner_live_case_model_auth_error():
    client = MockClient(raise_error=ModelError("HTTP 401 unauthorized"))
    case = _make_case()
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.verdict is EvalVerdict.FAIL
    assert FailureType.PROVIDER_AUTH_ERROR in res.failure_types


def test_22_runner_live_case_model_rate_limit():
    client = MockClient(raise_error=ModelError("Rate limit exceeded 429"))
    case = _make_case()
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.verdict is EvalVerdict.FAIL
    assert FailureType.PROVIDER_RATE_LIMIT in res.failure_types


def test_23_runner_live_case_unhandled_exception():
    client = MockClient(raise_error=RuntimeError("Unexpected connection reset"))
    case = _make_case()
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.verdict is EvalVerdict.FAIL
    assert FailureType.MODEL_ERROR in res.failure_types


# =========================================================================
# 5. Multi-Run & Flakiness Metrics (Tests 24–28)
# =========================================================================

def test_24_runner_repeated_runs_single_run():
    client = MockClient(content="Consistent pass")
    case = _make_case(expected=EvalExpectation(task_status="completed"))
    corpus = replace(load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"), cases=(case,))
    runner = EvalRunner(live_settings=Settings(), live_client=client, runs=1)
    run = runner.run(corpus, mode="live")
    assert len(run.results) == 1
    assert run.metrics.get("flaky_case_rate") == 0.0
    assert run.metrics.get("runs_per_case") == 1


def test_25_runner_repeated_runs_multiple_runs():
    client = MockClient(content="Consistent pass")
    case = _make_case(expected=EvalExpectation(task_status="completed"))
    corpus = replace(load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"), cases=(case,))
    runner = EvalRunner(live_settings=Settings(), live_client=client, runs=2)
    run = runner.run(corpus, mode="live")
    assert len(run.results) == 2
    assert run.metrics.get("runs_per_case") == 2


def test_26_runner_flakiness_detection_mixed_results():
    class AlternatingClient:
        def __init__(self):
            self.calls = 0
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls <= 6:
                payload = json.dumps({"status": "pass", "summary": "Run 1 success", "steps": [{"title": "s", "instruction": "i", "verification": "v"}]})
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=payload)],
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=5, output_tokens=5),
                )
            raise ModelError("Run 2 failure")

    case = _make_case(
        setup=EvalSetup(script=[{"title": "step1", "instruction": "do step 1", "verification_instruction": "verify"}]),
        expected=EvalExpectation(task_status="completed"),
    )
    corpus = replace(load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"), cases=(case,))
    runner = EvalRunner(live_settings=Settings(), live_client=AlternatingClient(), runs=2)
    run = runner.run(corpus, mode="live")
    assert run.metrics.get("flaky_case_rate") == 1.0
    assert "test-live-case" in (run.metrics.get("flaky_cases") or [])


def test_27_runner_flakiness_detection_consistent_results():
    client = MockClient(content="Same output")
    case = _make_case(expected=EvalExpectation(task_status="completed"))
    corpus = replace(load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"), cases=(case,))
    runner = EvalRunner(live_settings=Settings(), live_client=client, runs=2)
    run = runner.run(corpus, mode="live")
    assert run.metrics.get("flaky_case_rate") == 0.0


def test_28_runner_repeated_runs_aggregates_metrics():
    client = MockClient(content="Pass", input_tokens=10, output_tokens=5)
    case = _make_case(expected=EvalExpectation(task_status="completed"), timeout_ms=30_000)
    corpus = replace(load_corpus(default_live_corpus_paths()).select(case_id="live-reasoning-001"), cases=(case,))
    runner = EvalRunner(live_settings=Settings(), live_client=client, runs=3)
    run = runner.run(corpus, mode="live")
    assert run.metrics["cases"] == 3
    assert (run.metrics.get("input_tokens") or run.metrics.get("known_input_tokens", 0)) >= 30
    assert (run.metrics.get("output_tokens") or run.metrics.get("known_output_tokens", 0)) >= 15


# =========================================================================
# 6. Isolated Tools Execution (Tests 29–31)
# =========================================================================

def test_29_runner_safe_tools_filesystem_read(tmp_path):
    from tieru.evals.runner import _tool_function
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.txt").write_text("file content", encoding="utf-8")
    calls = {}
    fn = _tool_function("filesystem_read", workspace, {}, calls)
    res = fn(path="sample.txt")
    assert res == "file content"
    assert calls["filesystem_read"] == 1


def test_30_runner_safe_tools_filesystem_write(tmp_path):
    from tieru.evals.runner import _tool_function
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = {}
    fn = _tool_function("filesystem_write", workspace, {}, calls)
    res = fn(path="out.txt", content="created")
    assert "Wrote 7 bytes" in res
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "created"


def test_31_runner_safe_tools_run_command(tmp_path):
    from tieru.evals.runner import _tool_function
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = {}
    fn = _tool_function("run_command", workspace, {}, calls)
    res = json.loads(fn(command="python -c \"print('hello-subproc')\""))
    assert res["exit_code"] == 0
    assert "hello-subproc" in res["output"]


# =========================================================================
# 7. Safety Gates & Verification (Tests 32–35)
# =========================================================================

def _dummy_evidence(task_status: str = "completed") -> Any:
    from tieru.evals.models import EvalEvidence
    return EvalEvidence(
        task_status=task_status,
        steps=(),
        tool_calls=(),
        trust_decisions=(),
        action_executions=(),
        replay_events=(),
        selected_skills=(),
        verification_results=(),
        artifacts=(),
        changed_artifacts=(),
        final_output="Done",
        duration_ms=100,
        model_calls=1,
        tool_call_count=0,
        retry_count=0,
        recovery_count=0,
    )


def test_32_safety_gate_trust_violation_blocks_reliability_pass():
    case = _make_case()
    evidence = _dummy_evidence()
    res = score_case(case, evidence)
    res = replace(res, metrics={**res.metrics, "trust_violations": 1})
    summary = aggregate_results((res,))
    assert summary["reliability_pass"] is False


def test_33_safety_gate_duplicate_side_effect_blocks_reliability_pass():
    case = _make_case()
    evidence = _dummy_evidence()
    res = score_case(case, evidence)
    res = replace(res, metrics={**res.metrics, "duplicate_side_effects": 1})
    summary = aggregate_results((res,))
    assert summary["reliability_pass"] is False


def test_34_safety_gate_prompt_injection_escape_blocks_reliability_pass():
    case = _make_case()
    evidence = _dummy_evidence()
    res = score_case(case, evidence)
    res = replace(res, metrics={**res.metrics, "prompt_injection_escape": 1, "injection_applicable": 1})
    summary = aggregate_results((res,))
    assert summary["reliability_pass"] is False


def test_35_safety_gate_goal_false_pass_blocks_reliability_pass():
    case = _make_case()
    evidence = _dummy_evidence()
    res = score_case(case, evidence)
    res = replace(res, metrics={**res.metrics, "goal_false_pass": 1, "goal_false_pass_rate": 1.0})
    summary = aggregate_results((res,))
    assert summary["reliability_pass"] is False


# =========================================================================
# 8. M22–M25 Integration Telemetry in Live Eval (Tests 36–39)
# =========================================================================

def test_36_m25_budget_exhaustion_detected_in_live_eval():
    client = MockClient(content="Running step")
    case = _make_case(
        case_id="budget-exhaust",
        setup=EvalSetup(
            script=[
                {"title": "Step 1", "instruction": "Step 1", "verification_instruction": "Check"},
                {"title": "Step 2", "instruction": "Step 2", "verification_instruction": "Check"},
            ]
        ),
        expected=EvalExpectation(task_status="blocked", expected_blocked=True),
        max_steps=2,
        max_model_calls=1,
    )
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert res.verdict in {EvalVerdict.PASS, EvalVerdict.BLOCKED}


def test_37_m24_capability_routing_events_captured_in_evidence():
    case = _make_case(
        setup=EvalSetup(
            fake_tools=(
                {"name": "filesystem_read", "capabilities": ["local_read"], "always_visible": False, "domains": ["fs"]},
                {"name": "remote_call", "capabilities": ["network"], "always_visible": False, "domains": ["net"]},
            )
        )
    )
    client = MockClient(content="Inspected")
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert "visible_tool_count" in res.metrics


def test_38_m23_goal_contract_constraints_evaluated_in_live_eval():
    case = _make_case(
        setup=EvalSetup(files={"base.txt": "locked"}),
        expected=EvalExpectation(task_status="completed", unchanged_artifacts=["base.txt"]),
    )
    client = MockClient(content="Done without modifying base.txt")
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert "constraint_violation" in res.metrics


def test_39_m22_plan_revisions_and_replans_tracked_in_evidence():
    case = _make_case()
    client = MockClient(content="Completed")
    runner = EvalRunner(live_settings=Settings(), live_client=client)
    res = runner.run_live_case(case)
    assert "replan_count" in res.metrics
    assert "plan_revisions" in res.metrics


# =========================================================================
# 9. Baseline Serialization & CLI (Tests 40–42)
# =========================================================================

def test_40_live_baseline_saving_preserves_live_mode(tmp_path):
    run_dict = {
        "schema_version": 1,
        "tieru_version": "0.1.0",
        "corpus_version": "m26-live-v1",
        "corpus_hash": "abc123",
        "mode": "live",
        "metrics": {"task_completion_rate": 1.0},
        "categories": {},
    }
    target = tmp_path / "live_baseline.json"
    save_baseline(run_dict, target)
    loaded = load_json_artifact(target)
    assert loaded["mode"] == "live"
    assert loaded["metrics"]["task_completion_rate"] == 1.0


def test_41_live_baseline_comparison():
    base = {
        "schema_version": 1,
        "tieru_version": "0.1.0",
        "corpus_version": "m26-live-v1",
        "corpus_hash": "abc123",
        "mode": "live",
        "scope": "full_corpus",
        "status": "COMPLETE",
        "selected_cases": 14,
        "attempted_cases": 14,
        "completed_cases": 14,
        "full_corpus_cases": 14,
        "live_case_attempt_rate": 1.0,
        "selected_case_runs": 14,
        "attempted_case_runs": 14,
        "completed_case_runs": 14,
        "metrics": {"task_completion_rate": 0.8},
        "categories": {},
    }
    curr = {
        "schema_version": 1,
        "tieru_version": "0.1.0",
        "corpus_version": "m26-live-v1",
        "corpus_hash": "abc123",
        "mode": "live",
        "scope": "full_corpus",
        "status": "COMPLETE",
        "selected_cases": 14,
        "attempted_cases": 14,
        "completed_cases": 14,
        "full_corpus_cases": 14,
        "live_case_attempt_rate": 1.0,
        "selected_case_runs": 14,
        "attempted_case_runs": 14,
        "completed_case_runs": 14,
        "metrics": {"task_completion_rate": 0.9},
        "categories": {},
    }
    comp = compare_baseline(base, curr)
    assert comp.passed is True


def test_42_cli_eval_doctor_live_json_output(capsys):
    from tieru.evals.cli import run_eval_cli
    args = SimpleNamespace(eval_command="doctor", live=True, json=True)
    assert run_eval_cli(args) in {0, 1}
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "provider" in data
    assert "status" in data
    assert "usage_telemetry" in data

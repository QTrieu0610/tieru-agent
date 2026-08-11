"""M12 Model Fabric v2 deterministic acceptance coverage."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block
from tieru.config import ConfigError, Settings, load_settings
from tieru.fabric import ModelFabric, ModelSelectionError, RoutingOverrides
from tieru.fabric.availability import AvailabilityService
from tieru.fabric.candidates import CandidateRegistry
from tieru.fabric.performance import ModelPerformanceService
from tieru.loop.models import ModelRouter


def model(provider, name, *, local=False, tools=True, preference=0.5,
          cost="unknown", latency="unknown", enabled=True, extra_caps=None):
    caps = {"text": True, "tool_calling": tools}
    caps.update(extra_caps or {})
    return {
        "provider": provider, "model": name, "local": local,
        "enabled": enabled, "capabilities": caps, "preference": preference,
        "cost_tier": cost, "latency_tier": latency,
    }


def service(*, models, policy="balanced", probe=lambda _candidate: True,
            weights=None, min_samples=5, performance=None):
    settings = Settings(
        provider="ollama", model="gemma4:e2b", small_model="gemma4:e2b",
        fabric_enabled=True, fabric_models=models, fabric_routing_policy=policy,
        fabric_min_history_samples=min_samples,
        fabric_weights=weights or Settings().fabric_weights,
    )
    router = ModelRouter(settings, shared_client=object())
    availability = AvailabilityService(
        settings, probe=probe, credential_override=True
    )
    return ModelFabric(
        settings, router, availability=availability, performance=performance
    )


def test_registry_loads_local_and_optional_cloud_without_network():
    settings = Settings(fabric_models={
        "local": model("ollama", "gemma4:e2b", local=True),
        "cloud": model("anthropic", "configured-cloud", local=False),
    })
    candidates = CandidateRegistry(settings).all()
    assert [(item.candidate_id, item.local) for item in candidates] == [
        ("local", True), ("cloud", False)
    ]


@pytest.mark.parametrize("alias", ["", "bad alias", "../escape"])
def test_registry_rejects_invalid_alias(alias):
    with pytest.raises(ConfigError):
        CandidateRegistry(Settings(fabric_models={alias: model(
            "ollama", "gemma4:e2b", local=True
        )}))


def test_registry_rejects_unknown_provider_and_malformed_capability():
    with pytest.raises(ConfigError, match="unknown provider"):
        CandidateRegistry(Settings(fabric_models={"bad": model("missing", "x")}))
    bad = model("ollama", "x", local=True)
    bad["capabilities"] = {"vision": True}
    with pytest.raises(ConfigError, match="unsupported"):
        CandidateRegistry(Settings(fabric_models={"bad": bad}))


def test_existing_m11_settings_derive_compatible_candidates():
    candidates = CandidateRegistry(Settings()).all()
    assert candidates and all(not item.explicit for item in candidates)


def test_nested_config_validation_does_no_network(tmp_path, monkeypatch):
    path = tmp_path / "tieru.yaml"
    path.write_text("""version: 1
active_profile: ollama-gemma4-e2b
fabric:
  enabled: true
  routing_policy: local_only
  models:
    local-main:
      provider: ollama
      model: gemma4:e2b
      local: true
      capabilities: {text: true, tool_calling: true}
""", encoding="utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("network"))
    settings = load_settings({"config_path": path, "home": tmp_path / "home"})
    assert settings.fabric_models["local-main"]["model"] == "gemma4:e2b"


def test_duplicate_candidate_alias_is_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("""version: 1
fabric:
  models:
    repeated: {provider: ollama, model: one, local: true}
    repeated: {provider: ollama, model: two, local: true}
""", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key"):
        load_settings({"config_path": path, "home": tmp_path / "home"})


def test_invalid_policy_weights_and_counts_are_clear(tmp_path):
    with pytest.raises(ConfigError, match="routing_policy"):
        load_settings({"home": tmp_path / "p", "fabric_routing_policy": "hidden_cloud"})
    with pytest.raises(ConfigError, match="weights"):
        load_settings({"home": tmp_path / "w", "fabric_weights": {"cost": 2}})
    with pytest.raises(ConfigError, match="min_history_samples"):
        load_settings({"home": tmp_path / "s", "fabric_min_history_samples": -1})


def test_availability_missing_local_model_missing_cloud_key_and_cache(monkeypatch):
    settings = Settings(fabric_models={
        "local": model("ollama", "wanted", local=True),
        "cloud": model("anthropic", "cloud", local=False),
    })
    calls = []
    availability = AvailabilityService(
        settings, probe=lambda _candidate: calls.append(1) or ["other"]
    )
    local, cloud = CandidateRegistry(settings).all()
    assert availability.check(local).reason == "model_not_installed"
    assert availability.check(local).cached is True and len(calls) == 1
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert availability.check(cloud).reason == "unavailable_credentials"


def test_availability_refresh_bypasses_cache():
    calls = []
    svc = service(models={"local": model("ollama", "x", local=True)},
                  probe=lambda _candidate: calls.append(1) or True)
    svc.models(); svc.models(); svc.refresh()
    assert len(calls) == 2


def test_local_only_excludes_cloud_and_local_first_prefers_local():
    models = {
        "local": model("ollama", "local", local=True, preference=0.1),
        "cloud": model("anthropic", "cloud", preference=1.0),
    }
    local_only = service(models=models, policy="local_only").route("hello")
    assert local_only.model_selection.candidate_id == "local"
    cloud_eval = next(x for x in local_only.model_selection.candidates
                      if x.candidate_id == "cloud")
    assert "local_only_policy" in cloud_eval.exclusion_reasons
    assert service(models=models, policy="local_first").route(
        "What is rain?"
    ).model_selection.candidate_id == "local"


def test_sensitive_signal_and_force_local_cannot_route_cloud():
    only_cloud = {"cloud": model("anthropic", "cloud")}
    svc = service(models=only_cloud, policy="balanced")
    with pytest.raises(ModelSelectionError):
        svc.route("inspect this API key")
    with pytest.raises(ModelSelectionError):
        svc.route("hello", overrides=RoutingOverrides(force_local=True))


def test_agent_requires_verified_tool_calling_and_unknown_is_conservative():
    models = {
        "plain": model("ollama", "plain", local=True, tools=False, preference=1),
        "agent": model("ollama", "agent", local=True, tools=True, preference=0),
    }
    decision = service(models=models).route("edit this code file")
    assert decision.model_selection.candidate_id == "agent"
    excluded = next(x for x in decision.model_selection.candidates
                    if x.candidate_id == "plain")
    assert "tool_calling_unsupported_or_unknown" in excluded.exclusion_reasons


def test_deep_context_can_be_verified_by_context_limit():
    candidate = model("ollama", "deep", local=True, extra_caps={"long_context": None})
    candidate["context_limit"] = 32000
    decision = service(models={"deep": candidate}).route(
        "perform a comprehensive repository architecture analysis"
    )
    assert decision.model_selection.candidate_id == "deep"


def test_preference_cost_latency_and_ties_are_deterministic():
    models = {
        "b": model("ollama", "b", local=True, preference=0.9, cost="high", latency="slow"),
        "a": model("ollama", "a", local=True, preference=0.1, cost="free", latency="fast"),
    }
    weights = {"capability": 0, "preference": 1, "performance": 0,
               "latency": 0, "cost": 0, "local": 0}
    svc = service(models=models, weights=weights)
    first = svc.route("hello").model_selection
    second = svc.route("hello").model_selection
    assert first.candidate_id == second.candidate_id == "b"
    assert first.total_score == sum(first.score_breakdown.values())
    cost_svc = service(models=models, weights={
        "capability": 0, "preference": 0, "performance": 0,
        "latency": 0, "cost": 1, "local": 0,
    })
    assert cost_svc.route("hello").model_selection.candidate_id == "a"


class FakeReplay:
    def __init__(self, runs):
        self.runs = runs

    def list_runs(self, **_filters):
        return self.runs

    def get_events(self, run_id, **_options):
        row = next(item for item in self.runs if item["run_id"] == run_id)
        return [{"event_type": "fabric_selection", "safe_payload": {
            "execution_mode": "quick", "task_type": "greeting"
        }}] + row.get("events", [])


def test_history_is_neutral_below_minimum_and_smoothed_after_minimum():
    candidate = SimpleNamespace(provider="ollama", model="x")
    runs = [{"run_id": "one", "provider": "ollama", "model": "x",
             "status": "completed", "latency_ms": 100, "events": []}]
    stats = ModelPerformanceService(FakeReplay(runs), min_samples=5)
    assert stats.score_for(candidate, execution_mode="quick", task_type="greeting") == {
        "sample_count": 1, "sufficient_samples": False, "success_rate": 1.0,
        "average_latency_ms": 100, "median_latency_ms": 100, "score": 0.5,
    }
    runs.extend({"run_id": str(i), "provider": "ollama", "model": "x",
                 "status": "failed" if i == 2 else "completed",
                 "latency_ms": 200, "events": []} for i in range(2, 7))
    enough = ModelPerformanceService(FakeReplay(runs), min_samples=5).score_for(
        candidate, execution_mode="quick", task_type="greeting"
    )
    assert enough["sufficient_samples"] and 0 < enough["score"] < 1
    assert "prompt" not in json.dumps(enough).lower()


def test_structured_selection_has_exclusions_reasons_and_fallback_chain():
    models = {
        "preferred": model("ollama", "p", local=True, preference=1),
        "fallback": model("ollama", "f", local=True, preference=0),
        "disabled": model("ollama", "d", local=True, enabled=False),
    }
    selection = service(models=models).route("hello").model_selection
    assert selection.candidate_id == "preferred"
    assert selection.fallback_chain == ("fallback",)
    assert selection.reasons and selection.excluded_candidates


def test_impossible_preferred_model_is_explained():
    svc = service(models={"plain": model("ollama", "x", local=True, tools=False)})
    with pytest.raises(ModelSelectionError) as caught:
        svc.route("edit this file", overrides=RoutingOverrides(preferred_model="plain"))
    assert caught.value.evaluations[0].exclusion_reasons


def test_multiple_local_candidates_and_explicit_target_client_cache():
    svc = service(models={
        "small": model("ollama", "small", local=True, preference=0.2),
        "main": model("ollama", "main", local=True, preference=0.8),
    })
    selection = svc.route("hello").model_selection
    assert selection.candidate_id == "main"
    candidate = svc.candidate("main")
    router = svc.model_router
    assert router.client_for(candidate) is router.client_for(candidate)


def test_local_failure_reselects_cloud_only_when_policy_allows():
    models = {
        "local": model("ollama", "local", local=True, preference=1),
        "cloud": model("anthropic", "cloud", preference=1),
    }
    balanced = service(models=models, policy="local_first")
    first = balanced.route("hello")
    fallback = balanced.fallback_decision(first, "connection_failure")
    assert fallback.model_selection.candidate_id == "cloud"
    assert fallback.model_selection.fallback_count == 1
    local_only = service(models=models, policy="local_only")
    first = local_only.route("hello")
    with pytest.raises(ModelSelectionError):
        local_only.fallback_decision(first, "connection_failure")


def test_fallback_is_bounded_and_same_candidate_is_not_retried():
    svc = service(models={
        "a": model("ollama", "a", local=True, preference=1),
        "b": model("ollama", "b", local=True, preference=0),
    })
    first = svc.route("hello")
    second = svc.fallback_decision(first, "connection_failure")
    assert second.model_selection.candidate_id == "b"
    with pytest.raises(RuntimeError, match="limit"):
        svc.fallback_decision(second, "provider_timeout")


def test_keyless_local_turn_records_safe_selection_and_stays_sticky(tmp_path):
    app = make_waku(
        tmp_path / "home", client=ScriptedClient([response([text_block("Hi")])]),
        profile="ollama-gemma4-e2b", fabric_enabled=True,
        fabric_models={"local": model("ollama", "gemma4:e2b", local=True)},
    )
    result = app.respond("hello", source="test")
    events = app.replay.get_events(result.run_id)
    selections = [event for event in events if event["event_type"] == "fabric_selection"]
    assert result.reply == "Hi" and len(selections) == 1
    payload = selections[0]["safe_payload"]
    assert payload["candidate_id"] == "local" and "api_key" not in json.dumps(payload).lower()
    summary = app.replay.summarize_run(result.run_id)["fabric"]
    assert summary["initial_model"] == summary["final_model"]


def test_explicit_local_candidate_bootstraps_without_cloud_role_key(tmp_path, monkeypatch):
    from tieru.app import Tieru

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(
        home=tmp_path / "bootstrap", fabric_enabled=True,
        fabric_models={"local": model("ollama", "gemma4:e2b", local=True)},
    )
    app = Tieru(settings=settings)
    try:
        assert app.model_router._shared_client is None
        assert app.memory.provider == "ollama"
        assert app.memory.model == "gemma4:e2b"
    finally:
        app.close()


def test_hard_failure_fallback_executes_next_eligible_target(tmp_path):
    class FailingClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self.create)

        @staticmethod
        def create(**_kwargs):
            raise ConnectionError("endpoint unavailable")

    app = make_waku(
        tmp_path / "fallback", client=ScriptedClient([response([text_block("unused")])]),
        profile="ollama-gemma4-e2b", fabric_enabled=True,
        fabric_routing_policy="local_first",
        fabric_models={
            "local": model("ollama", "local", local=True, preference=1),
            "cloud": model("anthropic", "cloud", preference=1),
        },
    )
    app.model_router._shared_client = None
    app.model_router._clients.update({
        "local": FailingClient(),
        "cloud": ScriptedClient([response([text_block("Recovered")])]),
    })
    result = app.respond("hello", source="test")
    summary = app.replay.summarize_run(result.run_id)["fabric"]
    assert result.reply == "Recovered"
    assert summary["initial_model"]["candidate_id"] == "local"
    assert summary["final_model"]["candidate_id"] == "cloud"
    assert summary["fallback_used"] and summary["fallback_count"] == 1


def test_fallback_never_restarts_after_tool_activity(tmp_path, monkeypatch):
    app = make_waku(
        tmp_path / "unsafe", client=ScriptedClient([response([text_block("unused")])]),
        profile="ollama-gemma4-e2b", fabric_enabled=True,
        fabric_models={
            "local": model("ollama", "local", local=True),
            "backup": model("ollama", "backup", local=True),
        },
    )
    decision = app.fabric.route("hello")
    events = []

    def unsafe(_message, _decision, notify, _stream):
        notify("tool_completed", {"tool": "write", "output": "ok"})
        raise ConnectionError("endpoint unavailable")

    monkeypatch.setattr(app, "_run_profiled_turn", unsafe)
    with pytest.raises(ConnectionError):
        app._run_profiled_with_fallback(
            "hello", decision, lambda kind, event: events.append((kind, event)), False
        )
    fallback = next(event for kind, event in events if kind == "fabric_fallback")
    assert fallback["status"] == "blocked_after_tool_activity_or_stream"


def test_model_selection_never_changes_trust_policy():
    settings = Settings(
        trust_policy={"default": "deny"},
        fabric_models={"local": model("ollama", "x", local=True)},
    )
    before = json.dumps(settings.trust_policy, sort_keys=True)
    service(models=settings.fabric_models).route("edit this code file")
    assert json.dumps(settings.trust_policy, sort_keys=True) == before


def test_cli_surface_parses_without_executing():
    from tieru.__main__ import _parser

    args = _parser().parse_args([
        "fabric", "score", "edit this file", "--local", "--model", "local",
        "--mode", "agent",
    ])
    assert args.fabric_command == "score" and args.local and args.mode == "agent"

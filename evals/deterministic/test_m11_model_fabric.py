"""M11 deterministic acceptance coverage for Model Fabric v1."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from tieru.__main__ import _parser
from tieru.config import ConfigError, Settings, load_settings
from tieru.fabric import (
    ExecutionMode,
    ModelFabric,
    RouteDecision,
    TaskAnalyzer,
)
from tieru.loop.models import ModelRouter


def fabric(**overrides):
    settings = Settings(fabric_enabled=True, **overrides)
    return ModelFabric(settings, ModelRouter(settings, shared_client=object()))


@pytest.mark.parametrize(
    ("message", "task_type", "mode"),
    [
        ("hello", "greeting", "quick"),
        ("What causes rain?", "lookup", "standard"),
        ("edit this repository file", "coding", "agent"),
        ("perform a comprehensive repository architecture analysis", "analysis", "deep"),
        ("xyzzy", "unknown", "standard"),
    ],
)
def test_deterministic_task_analysis_routes_expected_modes(message, task_type, mode):
    decision = fabric().route(message)
    assert decision.task_profile.task_type == task_type
    assert decision.mode.value == mode
    assert decision.reason_codes
    assert decision.classifier_source == "deterministic"


def test_analyzer_needs_no_model_network_or_key():
    analyzer = TaskAnalyzer()
    assert not hasattr(analyzer, "client")
    assert analyzer.analyze("hello").task_type == "greeting"
    settings = Settings(
        profile="ollama-gemma4-e2b", provider="ollama", model="gemma4:e2b",
        small_model="gemma4:e2b", api_key="", fabric_enabled=True,
    )
    decision = ModelFabric(settings, ModelRouter(settings, shared_client=object())).route("hello")
    assert decision.provider == "ollama" and decision.model == "gemma4:e2b"


def test_profiles_are_finite_and_standard_preserves_settings():
    settings = Settings(
        fabric_enabled=True, max_tokens=3000, max_iterations=7, history_turns=9,
        fabric_agent_max_tokens=4500, fabric_agent_max_iterations=11,
        fabric_deep_max_tokens=9000, fabric_deep_max_iterations=13,
        fabric_deep_history_turns=18,
    )
    modes = {item["mode"]: item for item in ModelFabric(
        settings, ModelRouter(settings, shared_client=object())
    ).modes()}
    assert modes["quick"] | {
        "max_tokens": 512, "max_iterations": 1, "tools_enabled": False,
        "memory_enabled": False, "verification_enabled": False,
    } == modes["quick"]
    assert modes["standard"]["max_tokens"] == 3000
    assert modes["standard"]["max_iterations"] == 7
    assert modes["standard"]["history_turns"] == 9
    assert modes["agent"]["tools_enabled"] and modes["agent"]["max_tokens"] == 4500
    assert modes["deep"]["max_tokens"] == 9000
    assert modes["deep"]["max_iterations"] == 13
    assert modes["deep"]["history_turns"] == 18
    assert modes["deep"]["verification_enabled"]
    assert all(0 < item["max_tokens"] < 100_000 for item in modes.values())
    assert all(0 < item["max_iterations"] < 100 for item in modes.values())


def test_route_decision_is_structured_and_resolved_by_existing_router():
    decision = fabric().route("edit the repository file")
    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.AGENT
    assert decision.profile.role == decision.role == "main"
    assert decision.model and decision.provider
    assert decision.public()["task_profile"]["reason_codes"]


def test_optional_classifier_failure_and_malformed_output_fall_back():
    settings = Settings(fabric_enabled=True, fabric_use_small_classifier=True)
    broken = ModelFabric(
        settings, ModelRouter(settings, shared_client=object()),
        classifier=lambda _message: (_ for _ in ()).throw(OSError("offline")),
    ).route("xyzzy")
    malformed = ModelFabric(
        settings, ModelRouter(settings, shared_client=object()),
        classifier=lambda _message: "not json",
    ).route("xyzzy")
    assert broken.mode is malformed.mode is ExecutionMode.STANDARD
    assert broken.fallback_used and malformed.fallback_used
    assert broken.classifier_source == malformed.classifier_source == "deterministic_fallback"


def test_optional_classifier_is_only_a_signal_and_cannot_override_agent_policy():
    calls = []
    settings = Settings(fabric_enabled=True, fabric_use_small_classifier=True)
    service = ModelFabric(
        settings, ModelRouter(settings, shared_client=object()),
        classifier=lambda message: calls.append(message) or {
            "task_type": "greeting", "complexity": "low"
        },
    )
    decision = service.route("delete this repository file")
    assert decision.mode is ExecutionMode.AGENT
    assert calls == []


def test_analyzer_exception_returns_safe_standard_fallback():
    class BrokenAnalyzer:
        def analyze(self, _message):
            raise RuntimeError("bad analyzer")

    decision = ModelFabric(
        Settings(fabric_enabled=True), ModelRouter(Settings(), shared_client=object()),
        analyzer=BrokenAnalyzer(),
    ).route("anything")
    assert decision.mode is ExecutionMode.STANDARD
    assert decision.fallback_used and "analyzer_failure" in decision.reason_codes


def test_unknown_is_standard_even_with_a_nonstandard_configured_default():
    assert fabric(fabric_default_mode="deep").route("xyzzy").mode is ExecutionMode.STANDARD


class RecordingClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


def test_quick_is_one_call_without_memory_tools_or_verification(tmp_path):
    client = RecordingClient([response([text_block("Hello!")])])
    app = make_waku(tmp_path / "home", client=client, fabric_enabled=True)
    app.memory.gated_retrieve = lambda *_args, **_kwargs: pytest.fail("memory called")
    result = app.respond("hello", source="test")
    assert result.reply == "Hello!" and result.iterations == 1
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == app.model_router.model("small")
    assert client.calls[0]["tools"] == []
    assert client.calls[0]["max_tokens"] == app.settings.fabric_quick_max_tokens


def test_standard_preserves_memory_tools_and_normal_budgets(tmp_path):
    client = RecordingClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "ordinary"}')]),
        response([text_block("Normal answer")]),
    ])
    app = make_waku(
        tmp_path / "home", client=client, fabric_enabled=True,
        max_tokens=2345, max_iterations=6,
    )
    result = app.respond("What causes rain?", source="test")
    assert result.reply == "Normal answer"
    loop_call = client.calls[-1]
    assert loop_call["tools"]
    assert loop_call["max_tokens"] == 2345


def test_agent_exposes_tools_but_trust_still_denies(tmp_path):
    script = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "action"}')]),
        response([tool_block("create_event", {
            "title": "Private", "start": "2026-08-11T10:00:00",
            "end": "2026-08-11T11:00:00",
        })]),
        response([text_block("Could not create it.")]),
    ])
    app = make_waku(
        tmp_path / "home", client=script, fabric_enabled=True,
        trust={"capabilities": {"local_write": "deny"}},
    )
    policy = copy.deepcopy(app.settings.trust_policy)
    result = app.respond("create a calendar event", source="test")
    assert result.tool_calls and "tool_permission_denied" in result.tool_calls[0]["output"]
    assert app.settings.trust_policy == policy
    assert not (app.settings.home / "calendar.ics").exists()


def test_deep_runs_bounded_verification_and_records_fabric_metrics(tmp_path):
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "analysis"}')]),
        response([text_block("Architecture answer")]),
        response([text_block('{"verified": true, "issue_count": 0}')]),
    ])
    app = make_waku(tmp_path / "home", client=client, fabric_enabled=True)
    result = app.respond(
        "perform a comprehensive repository architecture analysis", source="test"
    )
    assert result.reply == "Architecture answer"
    run = app.replay.inspect(result.run_id)
    assert run["role"] == "main"
    assert run["summary"]["fabric"]["execution_mode"] == "deep"
    events = run["events"]
    assert {item["event_type"] for item in events} >= {
        "fabric_analysis", "fabric_route", "fabric_verification"
    }
    route = next(item for item in events if item["event_type"] == "fabric_route")
    stored = json.dumps(route)
    assert "comprehensive repository architecture" not in stored
    assert route["safe_payload"]["max_iterations"] == app.settings.fabric_deep_max_iterations


def test_verification_failure_preserves_original_safe_result(tmp_path):
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "analysis"}')]),
        response([text_block("Original answer")]),
    ])
    app = make_waku(tmp_path / "home", client=client, fabric_enabled=True)
    result = app.respond(
        "perform a comprehensive repository architecture analysis", source="test"
    )
    assert result.reply == "Original answer"
    event = next(
        item for item in app.replay.get_events(result.run_id)
        if item["event_type"] == "fabric_verification"
    )
    assert event["safe_payload"]["status"] == "degraded"


def test_route_is_chosen_once_and_sticky_for_turn(tmp_path):
    client = ScriptedClient([response([text_block("Hi")])])
    app = make_waku(tmp_path / "home", client=client, fabric_enabled=True)
    calls = []
    original = app.fabric.analyzer

    class CountingAnalyzer:
        def analyze(self, message):
            calls.append(message)
            return original.analyze(message)

    app.fabric.analyzer = CountingAnalyzer()
    app.respond("hello", source="test")
    assert calls == ["hello"]


def test_fabric_metadata_alone_does_not_create_shadow_pattern(tmp_path):
    client = ScriptedClient([response([text_block("Hi")])])
    app = make_waku(
        tmp_path / "home", client=client, fabric_enabled=True, shadow_enabled=True,
    )
    app.respond("hello", source="test")
    assert app.shadow.patterns() == []


def test_default_disabled_preserves_graph_compatibility(tmp_path):
    settings = load_settings({"home": tmp_path / "home"})
    assert settings.fabric_enabled is False
    assert settings.fabric_default_mode == "standard"
    with pytest.raises(ConfigError):
        load_settings({"home": tmp_path / "bad", "fabric_default_mode": "adaptive"})


def test_cli_dashboard_and_explain_are_read_only_surfaces():
    args = _parser().parse_args(["fabric", "explain", "analyze this repository"])
    assert args.fabric_command == "explain" and args.message == "analyze this repository"
    html = Path("tieru/ops/static/index.html").read_text(encoding="utf-8")
    views = Path("tieru/ops/static/js/views.js").read_text(encoding="utf-8")
    dashboard = Path("tieru/ops/dashboard.py").read_text(encoding="utf-8")
    assert 'href="#fabric"' in html
    assert "Model Fabric v1 uses execution-mode routing" in views
    assert "does not adaptively select cloud providers" in views
    assert '"fabric": fabric' in dashboard


def test_explain_contains_no_message_and_does_not_call_optional_classifier():
    calls = []
    settings = Settings(fabric_enabled=True, fabric_use_small_classifier=True)
    service = ModelFabric(
        settings, ModelRouter(settings, shared_client=object()),
        classifier=lambda message: calls.append(message),
    )
    message = "private-authorization-value"
    result = service.explain(message)
    assert calls == []
    assert message not in json.dumps(result)
    assert result["mode"] == "standard"

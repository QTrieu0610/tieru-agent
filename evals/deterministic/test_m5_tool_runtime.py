"""Deterministic contracts for the M5 structured tool runtime foundation."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from evals.helpers import response, text_block, tool_block
from tieru.config import Settings
from tieru.db import connect
from tieru.loop.agent import run_loop
from tieru.tools import build_registry
from tieru.tools.registry import Tool, ToolExecutor, ToolRegistry


def _tool(name="read_value", fn=lambda value="ok": value, **changes):
    fields = {
        "name": name,
        "description": "Read a test value.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        "fn": fn,
        "risk": "low",
        "read_only": True,
        "capabilities": ("local_read",),
        "default_policy": "allow",
    }
    fields.update(changes)
    return Tool(**fields)


def test_registry_rejects_invalid_and_duplicate_declarations():
    registry = ToolRegistry()
    registry.register(_tool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_tool())
    with pytest.raises(ValueError, match="tool name"):
        registry.register(_tool(name="bad tool"))
    with pytest.raises(ValueError, match="input_schema"):
        registry.register(_tool(name="bad_schema", input_schema={"type": "string"}))

    assert registry.get("read_value") is not None
    assert registry.schemas() == [registry.get("read_value").to_api()]


def test_schema_validation_rejects_bad_arguments_before_authorization_or_call():
    calls = []
    events = []
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "minLength": 1},
            "days": {"type": "integer", "minimum": 1, "maximum": 7},
            "units": {"type": "string", "enum": ["metric", "imperial"]},
        },
        "required": ["city"],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register(
        _tool(
            name="forecast",
            input_schema=schema,
            fn=lambda **kwargs: calls.append(kwargs) or "wrong",
        )
    )

    output = json.loads(
        registry.execute(
            "forecast",
            {"city": "", "days": "two", "units": "kelvin", "extra": True},
            notify=lambda kind, event: events.append((kind, event)),
        )
    )

    assert output["error"]["code"] == "invalid_arguments"
    assert calls == []
    assert [kind for kind, _event in events] == ["tool_failed"]
    assert "trust_request" not in [kind for kind, _event in events]


def test_executor_contains_unknown_name_timeout_and_exception():
    events = []
    registry = ToolRegistry()
    registry.register(_tool(name="slow", fn=lambda: time.sleep(0.05) or "late"))
    registry.register(_tool(name="broken", fn=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
    registry.register(_tool(name="healthy", fn=lambda: "ok"))
    executor = ToolExecutor(registry, timeout_seconds=0.01)

    assert json.loads(executor.execute("bad name", {}))["error"]["code"] == "invalid_tool_name"
    unknown = json.loads(executor.execute("missing", {}))
    assert unknown["error"]["code"] == "tool_permission_denied"
    assert unknown["error"]["reason"] == "unknown tool"
    assert json.loads(executor.execute("slow", {}, notify=lambda *event: events.append(event)))[
        "error"
    ]["code"] == "tool_timeout"
    assert [kind for kind, _event in events][-1] == "tool_failed"
    assert events[-1][1]["error_code"] == "tool_timeout"
    assert json.loads(executor.execute("broken", {}))["error"]["code"] == "tool_execution_error"
    assert executor.execute("healthy", {}) == "ok"


class _RecordingClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


def test_loop_only_executes_structured_tool_calls():
    fired = []
    registry = ToolRegistry()
    registry.register(_tool(name="weather", fn=lambda value="ok": fired.append(value) or value))

    text_client = _RecordingClient([response([text_block("I will use weather for Hanoi")])])
    text_result = run_loop(text_client, "model", "system", [], registry)
    assert text_result.tool_calls == [] and fired == []
    assert text_client.calls[0]["tools"] == registry.schemas()

    structured_client = _RecordingClient(
        [
            response([tool_block("weather", {"value": "Hanoi"})], "tool_use"),
            response([text_block("Done")]),
        ]
    )
    result = run_loop(structured_client, "model", "system", [], registry)
    assert fired == ["Hanoi"]
    assert result.tool_calls[0]["args"] == {"value": "Hanoi"}


def test_weather_is_registered_and_uses_structured_location(monkeypatch, tmp_path):
    payload = {
        "current_condition": [
            {
                "temp_C": "31",
                "FeelsLikeC": "35",
                "humidity": "70",
                "weatherDesc": [{"value": "Partly cloudy"}],
            }
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("tieru.tools.weather.urllib.request.urlopen", fake_urlopen)
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    registry = build_registry(connect(tmp_path), settings)

    assert registry.get("weather").to_api()["input_schema"]["required"] == ["location"]
    output = registry.execute("weather", {"location": "Ho Chi Minh City"})
    assert "Partly cloudy" in output and "31°C" in output
    assert "Ho%20Chi%20Minh%20City" in seen["url"]
    assert seen["timeout"] > 0

"""Deterministic M8 contracts for Tieru Replay."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from evals.helpers import ScriptedClient, make_waku, response, text_block
from tieru.config import Settings, load_settings
from tieru.db import connect
from tieru.replay import ReplayNormalizer, ReplayRecorder, ReplayService
from tieru.replay.cli import run_replay_cli
from tieru.tools.registry import Tool, ToolRegistry


def service(tmp_path, **limits) -> ReplayService:
    settings = Settings(home=tmp_path, **limits)
    settings.ensure_home()
    return ReplayService(connect(tmp_path), settings)


def start(replay: ReplayService, message: str = "hello") -> str:
    return replay.start_run(
        session_id="session-a",
        source="test",
        role="main",
        model="main-model",
        provider="offline",
        user_input=message,
    ).run_id


def complete(replay: ReplayService, run_id: str, output: str = "done"):
    return replay.complete_run(
        run_id,
        output=output,
        iterations=2,
        latency_ms=42,
        role="main",
        model="main-model",
        provider="offline",
    )


def test_run_lifecycle_ids_timestamps_and_status(tmp_path):
    replay = service(tmp_path)
    first, second = start(replay), start(replay)
    assert first != second
    assert first.startswith("run_") and "hello" not in first
    running = replay.get_run(first)
    assert running["status"] == "running"
    assert running["started_at"].endswith("+00:00")

    finished = complete(replay, first)
    assert finished.status == "completed"
    assert finished.completed_at and finished.latency_ms == 42
    assert finished.iterations == 2

    failed = replay.fail_run(
        second,
        error_code="ProviderError",
        error_summary="provider failed",
        latency_ms=7,
        role="main",
        model="main-model",
        provider="offline",
    )
    assert failed.status == "failed"
    assert failed.error_code == "ProviderError"


def test_event_order_is_monotonic_and_retrieval_is_deterministic(tmp_path):
    replay = service(tmp_path)
    run_id = start(replay)
    for kind in ("gate", "model_call_started", "llm", "final_output"):
        replay.record_event(run_id, kind, {"decision": "skip", "iteration": 1})
    complete(replay, run_id)
    events = replay.get_events(run_id)
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(events) + 1))
    assert [event["sequence"] for event in replay.get_events(run_id)] == sequences
    assert len({event["event_id"] for event in events}) == len(events)


def test_normalizer_covers_loop_memory_trust_tool_graph_and_error_events():
    normalizer = ReplayNormalizer()
    cases = {
        "llm": ("model", "model_call_completed"),
        "gate": ("memory", "memory_gate"),
        "trust_decision": ("trust", "trust_decision"),
        "tool_completed": ("tool", "tool_completed"),
        "route": ("routing", "graph_route"),
        "graph_end": ("error", "graph_failed"),
    }
    for kind, expected in cases.items():
        event = normalizer.normalize(kind, {"error": "boom"} if kind == "graph_end" else {})
        assert event is not None
        assert (event.category, event.event_type) == expected


def test_private_model_fields_and_memory_content_are_not_recorded():
    normalizer = ReplayNormalizer()
    model = normalizer.normalize(
        "llm",
        {"reasoning": "private", "thinking": "hidden", "messages": ["scratch"],
         "usage": {"in": 1, "out": 2}},
    )
    memory = normalizer.normalize(
        "memory_retrieval", {"content": "private memory body", "count": 1}
    )
    assert model.safe_payload == {"usage": {"in": 1, "out": 2}}
    assert "private memory body" not in json.dumps(memory.safe_payload)
    assert memory.safe_payload["content_omitted"] is True


def test_run_previews_and_configured_bounds_are_secret_safe(tmp_path):
    settings = load_settings(
        {
            "home": tmp_path,
            "replay_max_runs": 7,
            "replay_max_age_days": 3,
            "replay_max_event_payload_bytes": 512,
            "replay_max_tool_output_bytes": 128,
        }
    )
    replay = ReplayService(connect(tmp_path), settings)
    run_id = start(replay, "authorization: Bearer sk-abcdefghijklmnop")
    complete(replay, run_id, "api_key=sk-qrstuvwxyzabcdef")
    run = replay.get_run(run_id)
    assert "abcdefghijklmnop" not in run["input_preview"]
    assert "qrstuvwxyzabcdef" not in run["output_preview"]
    assert settings.redacted()["replay"]["max_runs"] == 7


def test_model_metadata_records_small_gate_and_actual_main_turn(tmp_path):
    client = ScriptedClient(
        [
            response([text_block('{"retrieve": false, "query": "", "reason": "not needed"}')]),
            response([text_block("answer")]),
        ]
    )
    app = make_waku(
        tmp_path / "home",
        client=client,
        main_provider="anthropic",
        small_provider="anthropic",
        main_model="main-model",
        small_model="small-model",
    )
    observed = []
    result = app.respond("hello", source="test",
                         observer=lambda kind, event: observed.append((kind, event)))
    assert result.run_id.startswith("run_")
    assert observed and all(event["run_id"] == result.run_id for _, event in observed)
    detail = app.replay.inspect(result.run_id)
    model_events = [event for event in detail["events"] if event["category"] == "model"]
    assert any(event["role"] == "small" and event["model"] == "small-model"
               for event in model_events)
    assert any(event["role"] == "main" and event["model"] == "main-model"
               for event in model_events)
    assert detail["model"] == "main-model"
    assert detail["provider"] == "anthropic"

    trace = next((app.settings.home / "traces").glob("*.jsonl"))
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert records[0]["run_id"] == result.run_id
    assert records[-1]["run_id"] == result.run_id


def test_quick_graph_run_records_small_model_as_the_answering_role(tmp_path):
    client = ScriptedClient(
        [
            response([text_block('{"route": "quick", "reason": "simple greeting"}')]),
            response([text_block("quick answer")]),
        ]
    )
    app = make_waku(
        tmp_path / "home",
        client=client,
        graph_workflows=True,
        main_provider="anthropic",
        small_provider="anthropic",
        main_model="main-model",
        small_model="small-model",
    )
    result = app.respond("hello", source="test")
    detail = app.replay.inspect(result.run_id)
    assert detail["role"] == "small"
    assert detail["model"] == "small-model"
    assert any(
        event["event_type"] == "graph_route"
        and event["safe_payload"].get("target") == "quick_reply"
        for event in detail["events"]
    )


def _tool(name, fn, policy="allow") -> Tool:
    return Tool(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        fn=fn,
        risk="low",
        read_only=True,
        capabilities=("filesystem.read",),
        default_policy=policy,
        sensitive_args=("authorization",),
    )


def test_trust_and_tool_lifecycle_allowed_denied_failed_and_secret_safe(tmp_path):
    replay = service(tmp_path, replay_max_tool_output_bytes=128)
    run_id = start(replay)
    recorder = ReplayRecorder(replay)
    recorder.run_id = run_id
    called = {"denied": 0}

    def denied_fn(**_kwargs):
        called["denied"] += 1
        return "wrong"

    def failed_fn(**_kwargs):
        raise RuntimeError("authorization: Bearer secret-value")

    registry = ToolRegistry()
    registry.register(_tool("ok", lambda **_kwargs: "x" * 500))
    registry.register(_tool("denied", denied_fn, "deny"))
    registry.register(_tool("failed", failed_fn))

    secret = "Bearer sk-abcdefghijklmnop"
    for name in ("ok", "denied", "failed"):
        args = {"authorization": secret} if name == "denied" else {}
        safe = registry.redact_args(name, args)
        recorder.event("tool_requested", {"tool": name, "args": safe})
        registry.execute(name, args, notify=recorder.event)

    events = replay.get_events(run_id)
    types = [event["event_type"] for event in events]
    assert "tool_completed" in types
    assert "tool_denied" in types
    assert "tool_failed" in types
    assert any(event["event_type"] == "trust_decision" for event in events)
    assert called["denied"] == 0
    stored = json.dumps(events)
    assert secret not in stored and "secret-value" not in stored
    completed = next(event for event in events if event["event_type"] == "tool_completed")
    assert completed["safe_payload"]["output_size"] == 500
    assert completed["safe_payload"]["truncated"] is True


def test_summary_is_deterministic_and_needs_no_model(tmp_path):
    replay = service(tmp_path)
    run_id = start(replay)
    replay.record_event(run_id, "memory_retrieval", {"count": 2})
    replay.record_event(run_id, "trust_decision", {"allowed": True})
    replay.record_event(run_id, "tool_completed", {"tool": "read", "output": "ok"})
    complete(replay, run_id)
    summary = replay.summarize_run(run_id)
    assert summary["sentence"] == "Run completed successfully."
    assert summary["memory"] == ["memory_retrieval"]
    assert summary["tools"] == {"successful": 1, "denied": 0, "failed": 0}
    assert summary["trust"] == {"allowed": 1, "denied": 0}


def test_sqlite_schema_indexes_and_migration_are_idempotent(tmp_path):
    home = tmp_path / "old-home"
    home.mkdir()
    sqlite3.connect(home / "state.db").close()
    first = connect(home)
    second = connect(home)
    tables = {row[0] for row in second.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    indexes = {row[0] for row in second.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert {"replay_runs", "replay_events"} <= tables
    assert {
        "replay_events_run_sequence_idx",
        "replay_runs_session_idx",
        "replay_runs_started_idx",
        "replay_runs_status_idx",
    } <= indexes
    first.close()
    second.close()


def test_retention_bounds_runs_without_deleting_memory_or_sessions(tmp_path):
    replay = service(tmp_path, replay_max_runs=2, replay_max_age_days=1)
    conn = replay.store.conn
    conn.execute("INSERT INTO facts (subject, content) VALUES ('Tieru', 'stays')")
    conn.execute("INSERT INTO chat_log (role, content, session_id) VALUES ('user','hello','s1')")
    conn.commit()
    ids = [start(replay, f"run {index}") for index in range(3)]
    assert len(replay.list_runs(limit=10)) == 2
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 1

    old = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE replay_runs SET started_at=? WHERE id=?", (old, ids[-1]))
    conn.commit()
    replay.store.cleanup(max_runs=2, max_age_days=1)
    assert all(run["run_id"] != ids[-1] for run in replay.list_runs(limit=10))
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_cli_lists_and_inspects_runs_as_text_and_json(tmp_path, capsys):
    replay = service(tmp_path)
    run_id = start(replay)
    complete(replay, run_id)
    settings = replay.settings

    args = SimpleNamespace(replay_selector="list", limit=10, json=False, events=False)
    assert run_replay_cli(args, settings) == 0
    assert run_id in capsys.readouterr().out

    args = SimpleNamespace(replay_selector=run_id, limit=10, json=True, events=True)
    assert run_replay_cli(args, settings) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == run_id
    assert payload["events"][0]["sequence"] == 1


def test_replay_failure_isolated_and_cannot_bypass_trust():
    class BrokenService:
        def start_run(self, **_fields):
            raise sqlite3.OperationalError("disk unavailable")

        def record_event(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("disk unavailable")

    recorder = ReplayRecorder(BrokenService())
    recorder.start(run_id="run_safe", session_id="s", source="test", role="main",
                   model="m", provider="p", user_input="hello")
    called = {"count": 0}

    def forbidden():
        called["count"] += 1
        return "wrong"

    registry = ToolRegistry()
    registry.register(_tool("forbidden", forbidden, "deny"))
    output = registry.execute("forbidden", {}, notify=recorder.event)
    assert "tool_permission_denied" in output
    assert called["count"] == 0
    assert recorder.degraded


def test_dashboard_contains_read_only_replay_view_and_no_execution_controls():
    html = Path("tieru/ops/static/index.html").read_text(encoding="utf-8")
    views = Path("tieru/ops/static/js/views.js").read_text(encoding="utf-8")
    assert 'href="#replay"' in html
    assert "Ordered timeline" in views
    assert "private chain-of-thought" in views
    for forbidden in ("replay rerun", "fork from event", "retry from tool"):
        assert forbidden not in views.lower()

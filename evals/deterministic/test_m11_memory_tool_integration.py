"""Deterministic contracts for M11 direct-agent memory tool integration."""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, response, text_block, tool_block
from tieru.app import Tieru
from tieru.config import Settings
from tieru.db import connect
from tieru.loop.agent import LoopResult, run_loop
from tieru.memory import Memory
from tieru.replay import ReplayRecorder, ReplayService, new_run_id
from tieru.tools import build_registry


def runtime(tmp_path, *, permissions=None, approve=True):
    settings = Settings(home=tmp_path, tool_permissions=permissions or {})
    settings.ensure_home()
    conn = connect(tmp_path)
    memory = Memory(conn, settings, client=None)
    registry = build_registry(
        conn,
        settings,
        memory,
        approval_handler=lambda _request: approve,
    )
    return settings, conn, memory, registry


def execute(registry, name, args, request, notify=None):
    return json.loads(
        registry.execute(
            name,
            args,
            notify=notify,
            context={"user_request": request},
        )
    )


def remember(registry, content="The M11 codename is amber-quartz"):
    return execute(
        registry,
        "memory_remember",
        {"subject": "m11-smoke", "content": content, "importance": 0.8},
        "Please remember this synthetic project fact.",
    )


def test_remember_then_search_returns_exact_id_and_provenance(tmp_path):
    _settings, _conn, _memory, registry = runtime(tmp_path)
    saved = remember(registry)
    found = execute(
        registry,
        "memory_search",
        {"query": "m11-smoke", "top_k": 4},
        "What do you remember about my m11-smoke project?",
    )

    assert saved["ok"] is True and saved["created"] is True
    assert found["count"] == 1
    assert found["memories"][0]["id"] == saved["memory"]["id"]
    assert found["memories"][0]["provenance"] == (
        "explicit user request via memory_remember"
    )
    assert found["memories"][0]["importance"] == 0.8
    assert found["memories"][0]["trusted"] is True


def test_update_keeps_id_and_only_new_value_is_active(tmp_path):
    _settings, _conn, memory, registry = runtime(tmp_path)
    saved = remember(registry)
    memory_id = saved["memory"]["id"]
    updated = execute(
        registry,
        "memory_update",
        {"memory_id": memory_id, "content": "The M11 codename is cobalt-lantern"},
        f"Update memory {memory_id} to the new synthetic codename.",
    )

    assert updated["memory"]["id"] == memory_id
    assert updated["memory"]["content"] == "The M11 codename is cobalt-lantern"
    assert "memory_update" in updated["memory"]["provenance"]
    assert memory.store.search("amber-quartz") == []
    active = memory.store.search("m11-smoke")
    assert [(item.id, item.content) for item in active] == [
        (memory_id, "The M11 codename is cobalt-lantern")
    ]


def test_forget_is_destructive_and_removes_retrieval(tmp_path):
    _settings, _conn, memory, registry = runtime(tmp_path)
    memory_id = remember(registry)["memory"]["id"]
    forgotten = execute(
        registry,
        "memory_forget",
        {"memory_id": memory_id},
        f"Forget and delete memory {memory_id}.",
    )

    assert forgotten == {
        "memory_id": memory_id,
        "ok": True,
        "status": "forgotten",
    }
    assert memory.store.search("m11-smoke") == []


def test_unrelated_query_and_normal_statement_do_not_call_memory(tmp_path):
    _settings, _conn, _memory, registry = runtime(tmp_path)
    for request in ("What is the capital of France?", "My synthetic color is blue."):
        client = ScriptedClient([response([text_block("No memory action.")])])
        result = run_loop(
            client,
            "offline-model",
            "system",
            [{"role": "user", "content": request}],
            registry,
        )
        assert result.tool_calls == []


def test_memory_write_update_and_forget_without_explicit_intent_are_blocked(tmp_path):
    _settings, _conn, memory, registry = runtime(tmp_path)
    blocked_save = execute(
        registry,
        "memory_remember",
        {"content": "My synthetic color is blue"},
        "My synthetic color is blue.",
    )
    assert blocked_save["error"]["code"] == "memory_intent_required"
    legacy = execute(
        registry,
        "save_note",
        {"subject": "user", "content": "My synthetic color is blue"},
        "My synthetic color is blue.",
    )
    assert legacy["error"]["code"] == "memory_intent_required"
    assert memory.store.list() == []

    memory_id = remember(registry)["memory"]["id"]
    blocked_update = execute(
        registry,
        "memory_update",
        {"memory_id": memory_id, "content": "unapproved replacement"},
        "The replacement value is available.",
    )
    blocked_forget = execute(
        registry,
        "memory_forget",
        {"memory_id": memory_id},
        "This record is old.",
    )
    assert blocked_update["error"]["code"] == "memory_intent_required"
    assert blocked_forget["error"]["code"] == "memory_intent_required"
    assert memory.store.get(memory_id).content == "The M11 codename is amber-quartz"


def test_unrelated_memory_search_attempt_is_blocked(tmp_path):
    _settings, _conn, _memory, registry = runtime(tmp_path)
    output = execute(
        registry,
        "memory_search",
        {"query": "capital France"},
        "What is the capital of France?",
    )
    assert output["error"]["code"] == "memory_query_not_relevant"


def test_negated_memory_intent_never_authorizes_a_mutation(tmp_path):
    _settings, _conn, memory, registry = runtime(tmp_path)
    save = execute(
        registry,
        "memory_remember",
        {"content": "negative-intent synthetic fact"},
        "Do not save or remember this synthetic fact.",
    )
    assert save["error"]["code"] == "memory_intent_required"
    memory_id = remember(registry)["memory"]["id"]
    update = execute(
        registry,
        "memory_update",
        {"memory_id": memory_id, "content": "negative update"},
        f"Do not update memory {memory_id}.",
    )
    forget = execute(
        registry,
        "memory_forget",
        {"memory_id": memory_id},
        f"Don't forget or delete memory {memory_id}.",
    )
    assert update["error"]["code"] == "memory_intent_required"
    assert forget["error"]["code"] == "memory_intent_required"
    assert memory.store.get(memory_id).content == "The M11 codename is amber-quartz"


def test_permission_deny_cannot_be_bypassed(tmp_path):
    permissions = {"tools": {"memory_update": "deny", "memory_forget": "deny"}}
    _settings, _conn, memory, registry = runtime(tmp_path, permissions=permissions)
    memory_id = remember(registry)["memory"]["id"]

    update = execute(
        registry,
        "memory_update",
        {"memory_id": memory_id, "content": "denied value"},
        f"Update memory {memory_id} to denied value.",
    )
    forget = execute(
        registry,
        "memory_forget",
        {"memory_id": memory_id},
        f"Forget memory {memory_id}.",
    )
    assert update["error"]["code"] == "tool_permission_denied"
    assert forget["error"]["code"] == "tool_permission_denied"
    assert memory.store.get(memory_id).content == "The M11 codename is amber-quartz"


def test_secret_policy_and_replay_redaction_are_preserved(tmp_path):
    settings, _conn, memory, registry = runtime(tmp_path)
    secret = "sk-abcdefghijklmnop"
    replay = ReplayService(memory.conn, settings)
    recorder = ReplayRecorder(replay)
    recorder.start(
        run_id=new_run_id(),
        session_id="m11-secret",
        source="test",
        role="main",
        model="offline",
        provider="offline",
        user_input="remember a synthetic credential",
    )
    refused = execute(
        registry,
        "memory_remember",
        {"content": f"api_key={secret}"},
        "Remember this synthetic credential.",
        notify=recorder.event,
    )
    recorder.complete(
        output="refused", iterations=1, latency_ms=1,
        role="main", model="offline", provider="offline",
    )

    assert refused["error"]["code"] == "memory_write_refused"
    assert memory.store.list() == []
    stored = json.dumps(replay.get_events(recorder.run_id))
    assert secret not in stored
    assert "api_key" not in stored


def test_duplicate_remember_reuses_one_active_memory(tmp_path):
    _settings, conn, _memory, registry = runtime(tmp_path)
    first = remember(registry)
    second = remember(registry)
    assert first["memory"]["id"] == second["memory"]["id"]
    assert second["created"] is False
    assert second["status"] == "already_exists"
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_update_refuses_collision_with_another_active_memory(tmp_path):
    _settings, _conn, memory, registry = runtime(tmp_path)
    remember(registry, "The first synthetic value")
    second = remember(registry, "The second synthetic value")
    result = execute(
        registry,
        "memory_update",
        {"memory_id": second["memory"]["id"], "content": "The first synthetic value"},
        f"Update memory {second['memory']['id']} to the first synthetic value.",
    )
    assert result["error"]["code"] == "memory_update_refused"
    assert "duplicate active memory" in result["error"]["message"]
    assert len(memory.store.list()) == 2


def test_replay_records_every_memory_action_without_memory_bodies(tmp_path):
    settings, conn, _memory, registry = runtime(tmp_path)
    replay = ReplayService(conn, settings)
    recorder = ReplayRecorder(replay)
    recorder.start(
        run_id=new_run_id(), session_id="m11", source="test", role="main",
        model="offline", provider="offline", user_input="memory lifecycle",
    )
    saved = execute(
        registry, "memory_remember", {"subject": "m11", "content": "synthetic replay fact"},
        "Remember this synthetic replay fact.", notify=recorder.event,
    )
    memory_id = saved["memory"]["id"]
    execute(
        registry, "memory_search", {"query": "m11"},
        "What do you remember about my m11 project?", notify=recorder.event,
    )
    execute(
        registry, "memory_update", {"memory_id": memory_id, "content": "updated replay fact"},
        f"Update memory {memory_id}.", notify=recorder.event,
    )
    execute(
        registry, "memory_forget", {"memory_id": memory_id},
        f"Forget memory {memory_id}.", notify=recorder.event,
    )
    recorder.complete(
        output="done", iterations=4, latency_ms=1,
        role="main", model="offline", provider="offline",
    )

    events = replay.get_events(recorder.run_id)
    completed = {
        event["tool"]
        for event in events
        if event["event_type"] == "tool_completed"
    }
    assert completed == {
        "memory_search", "memory_remember", "memory_update", "memory_forget"
    }
    memory_events = [event for event in events if event["tool"] in completed]
    stored = json.dumps(memory_events)
    assert "synthetic replay fact" not in stored
    assert any(event["safe_payload"].get("content_omitted") for event in memory_events)


def test_memory_only_final_is_grounded_in_exact_observation(tmp_path):
    _settings, _conn, _memory, registry = runtime(tmp_path)
    saved = remember(registry)
    client = ScriptedClient(
        [
            response([tool_block("memory_search", {"query": "m11-smoke"})], "tool_use"),
            response([text_block("The invented codename is hallucinated-ruby.")]),
        ]
    )
    result = run_loop(
        client,
        "offline-model",
        "system",
        [{"role": "user", "content": "What do you remember about my m11-smoke project?"}],
        registry,
    )
    assert saved["memory"]["id"] in result.reply
    assert "amber-quartz" in result.reply
    assert "hallucinated-ruby" not in result.reply


def test_default_app_still_calls_direct_loop_not_m10_controller(tmp_path, monkeypatch):
    app = Tieru(
        settings=Settings(home=tmp_path),
        client=ScriptedClient([]),
        approval_handler=lambda _request: True,
    )
    app.memory.gated_retrieve = lambda *_args, **_kwargs: ""
    called = []

    def direct(**_kwargs):
        called.append("direct")
        return LoopResult(reply="direct", iterations=1)

    monkeypatch.setattr("tieru.app.run_loop", direct)
    result = app.respond("ordinary direct-loop request")
    assert result.reply == "direct"
    assert called == ["direct"]
    assert app.settings.graph_workflows is False

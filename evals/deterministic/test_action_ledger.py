"""Deterministic contracts for M14 local action idempotency."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

from tieru.db import connect
from tieru.execution import ClaimOutcome, ExecutionStatus, ExecutionStore
from tieru.replay import ReplayNormalizer
from tieru.tools.registry import Tool, ToolRegistry


def _tool(
    fn,
    *,
    name: str = "write_value",
    read_only: bool = False,
    policy: str = "allow",
    sensitive_args: tuple[str, ...] = (),
) -> Tool:
    capability = "local_read" if read_only else "local_write"
    return Tool(
        name=name,
        description="Test action ledger behavior.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
                "api_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
        fn=fn,
        risk="low" if read_only else "medium",
        read_only=read_only,
        capabilities=(capability,),
        default_policy=policy,
        operation="read" if read_only else "write",
        target_arg="to",
        sensitive_args=sensitive_args,
    )


def _registry(
    conn, tool: Tool, *, trust_policy=None, **store_options
) -> tuple[ToolRegistry, ExecutionStore]:
    store = ExecutionStore(conn, **store_options)
    registry = ToolRegistry(execution_store=store, trust_policy=trust_policy)
    registry.register(tool)
    return registry, store


def test_identical_side_effect_executes_once_and_replays_safe_result(tmp_path):
    calls = {"count": 0}
    events = []

    def write(**_args):
        calls["count"] += 1
        return "done"

    registry, store = _registry(connect(tmp_path), _tool(write))
    notify = lambda kind, event: events.append((kind, event))

    assert registry.execute("write_value", {"to": "A", "body": "X"}, notify=notify) == "done"
    assert registry.execute("write_value", {"to": "A", "body": "X"}, notify=notify) == "done"

    assert calls["count"] == 1
    assert store.count() == 1
    assert "tool_execution_claimed" in [kind for kind, _event in events]
    assert "tool_execution_completed" in [kind for kind, _event in events]
    assert "tool_idempotency_hit" in [kind for kind, _event in events]


def test_different_arguments_receive_distinct_trust_fingerprints(tmp_path):
    calls = {"count": 0}
    registry, store = _registry(
        connect(tmp_path),
        _tool(lambda **_args: calls.__setitem__("count", calls["count"] + 1) or "done"),
    )

    assert registry.execute("write_value", {"to": "A", "body": "X"}) == "done"
    assert registry.execute("write_value", {"to": "A", "body": "Y"}) == "done"

    rows = store.conn.execute(
        "SELECT action_fingerprint FROM tool_executions ORDER BY action_fingerprint"
    ).fetchall()
    assert calls["count"] == 2
    assert len(rows) == 2
    assert rows[0]["action_fingerprint"] != rows[1]["action_fingerprint"]


def test_read_only_action_is_not_cached(tmp_path):
    calls = {"count": 0}

    def read(**_args):
        calls["count"] += 1
        return f"value-{calls['count']}"

    registry, store = _registry(connect(tmp_path), _tool(read, read_only=True))
    assert registry.execute("write_value", {"to": "A"}) == "value-1"
    assert registry.execute("write_value", {"to": "A"}) == "value-2"
    assert calls["count"] == 2
    assert store.count() == 0


def test_trust_denial_never_claims_or_executes(tmp_path):
    calls = {"count": 0}
    registry, store = _registry(
        connect(tmp_path),
        _tool(lambda **_args: calls.__setitem__("count", 1) or "wrong", policy="deny"),
    )

    output = registry.execute("write_value", {"to": "A", "body": "X"})
    assert json.loads(output)["error"]["code"] == "tool_permission_denied"
    assert calls["count"] == 0
    assert store.count() == 0


def test_tightened_trust_policy_wins_over_completed_cache(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    store = ExecutionStore(conn)

    allowed = ToolRegistry(execution_store=store)
    allowed.register(
        _tool(lambda **_args: calls.__setitem__("count", calls["count"] + 1) or "done")
    )
    args = {"to": "A", "body": "X"}
    assert allowed.execute("write_value", args) == "done"

    denied = ToolRegistry(execution_store=store)
    denied.register(_tool(lambda **_args: "wrong", policy="deny"))
    events = []
    output = denied.execute("write_value", args, notify=lambda *event: events.append(event))

    assert json.loads(output)["error"]["code"] == "tool_permission_denied"
    assert calls["count"] == 1
    assert "tool_idempotency_hit" not in [kind for kind, _event in events]


def test_concurrent_identical_requests_execute_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = {"count": 0}
    outputs: list[str] = []

    def write(**_args):
        with counter_lock:
            calls["count"] += 1
        entered.set()
        assert release.wait(3)
        return "done"

    first_conn = connect(tmp_path, check_same_thread=False)
    second_conn = connect(tmp_path, check_same_thread=False)
    first, _first_store = _registry(first_conn, _tool(write))
    second, _second_store = _registry(second_conn, _tool(write))
    args = {"to": "A", "body": "X"}

    one = threading.Thread(target=lambda: outputs.append(first.execute("write_value", args)))
    two = threading.Thread(target=lambda: outputs.append(second.execute("write_value", args)))
    one.start()
    assert entered.wait(3)
    two.start()
    two.join(3)
    assert not two.is_alive()
    release.set()
    one.join(3)
    assert not one.is_alive()

    assert calls["count"] == 1
    assert "done" in outputs
    duplicate = next(output for output in outputs if output != "done")
    assert json.loads(duplicate)["error"]["code"] == "tool_execution_in_progress"
    first_conn.close()
    second_conn.close()


def test_non_retryable_failure_is_persisted_and_not_reexecuted(tmp_path):
    calls = {"count": 0}

    def fail(**_args):
        calls["count"] += 1
        raise RuntimeError("boom")

    registry, store = _registry(connect(tmp_path), _tool(fail))
    args = {"to": "A", "body": "X"}
    first = registry.execute("write_value", args)
    second = registry.execute("write_value", args)
    record = store.conn.execute("SELECT * FROM tool_executions").fetchone()

    assert json.loads(first)["error"]["code"] == "tool_execution_error"
    assert second == first
    assert calls["count"] == 1
    assert record["status"] == "failed"
    assert record["retryable"] == 0
    assert record["attempt_count"] == 1


def test_ledger_result_is_bounded_and_secret_safe(tmp_path):
    secret = "super-secret-value"
    replacement_secret = "other-secret-value"
    registry, store = _registry(
        connect(tmp_path),
        _tool(
            lambda **_args: f"api_key={secret} " + ("x" * 500),
            sensitive_args=("api_key",),
        ),
        trust_policy={"tools": {"write_value": "allow"}},
        max_result_bytes=128,
    )
    events = []
    output = registry.execute(
        "write_value",
        {"to": "A", "body": "X", "api_key": secret},
        notify=lambda *event: events.append(event),
    )
    replayed = registry.execute(
        "write_value",
        {"to": "A", "body": "X", "api_key": replacement_secret},
        notify=lambda *event: events.append(event),
    )
    record = store.conn.execute("SELECT * FROM tool_executions").fetchone()
    persisted = "|".join(str(value) for value in record)

    assert secret not in output
    assert secret not in replayed
    assert replacement_secret not in replayed
    assert secret not in persisted
    assert replacement_secret not in persisted
    assert secret not in json.dumps(events)
    assert replacement_secret not in json.dumps(events)
    assert store.count() == 1
    assert len(record["result"].encode("utf-8")) <= 128
    assert record["result_size"] > 128
    assert record["result_truncated"] == 1


def test_completed_execution_survives_connection_and_registry_restart(tmp_path):
    calls = {"count": 0}

    def write(**_args):
        calls["count"] += 1
        return "done"

    args = {"to": "A", "body": "X"}
    first_conn = connect(tmp_path)
    first, _store = _registry(first_conn, _tool(write))
    assert first.execute("write_value", args) == "done"
    first_conn.close()

    second_conn = connect(tmp_path)
    second, second_store = _registry(second_conn, _tool(write))
    assert second.execute("write_value", args) == "done"
    assert calls["count"] == 1
    assert second_store.count() == 1
    second_conn.close()


def test_stale_in_progress_claim_becomes_uncertain_without_retry(tmp_path):
    store = ExecutionStore(connect(tmp_path), stale_after_seconds=1)
    fingerprint = "f" * 64
    assert store.claim(fingerprint, "write_value").outcome is ClaimOutcome.CLAIMED
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="milliseconds")
    store.conn.execute(
        "UPDATE tool_executions SET started_at=?, updated_at=? WHERE action_fingerprint=?",
        (old, old, fingerprint),
    )
    store.conn.commit()

    claim = store.claim(fingerprint, "write_value")
    assert claim.outcome is ClaimOutcome.UNCERTAIN
    assert claim.record.status is ExecutionStatus.UNCERTAIN
    assert claim.record.attempt_count == 1
    assert claim.record.retryable is False


def test_schema_is_additive_and_idempotent(tmp_path):
    first = connect(tmp_path)
    second = connect(tmp_path)
    tables = {
        row[0]
        for row in second.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    indexes = {
        row[0]
        for row in second.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    assert "tool_executions" in tables
    assert "tool_executions_status_updated_idx" in indexes
    first.close()
    second.close()


def test_replay_normalizes_action_ledger_events_without_arguments():
    normalizer = ReplayNormalizer()
    for kind in (
        "tool_execution_claimed",
        "tool_idempotency_hit",
        "tool_execution_in_progress",
        "tool_execution_completed",
        "tool_execution_failed",
        "tool_execution_uncertain",
    ):
        event = normalizer.normalize(
            kind,
            {"tool": "write_value", "action_fingerprint": "f" * 64, "status": "completed"},
        )
        assert event is not None
        assert event.category == "tool" and event.event_type == kind
        assert "args" not in event.safe_payload

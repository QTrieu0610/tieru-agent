"""Deterministic M17 human-recovery and intervention contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from tieru.__main__ import _parser
from tieru.config import Settings
from tieru.db import connect
from tieru.execution import ExecutionStatus, ExecutionStore
from tieru.recovery import (
    RecoveryError,
    RecoveryResolution,
    RecoveryService,
    RecoveryStore,
)
from tieru.recovery.cli import run_recovery_cli
from tieru.replay import ReplayRecorder, ReplayService
from tieru.tasks.cli import run_task_cli
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    PlanStep,
    StepExecution,
    StepStatus,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.planner import CallableTaskPlanner
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import LayeredTaskVerifier
from tieru.tools.command import CommandPolicy, CommandRunner
from tieru.tools.command import make_tool as command_tool
from tieru.tools.registry import Tool, ToolRegistry, _build_action

ARGS = {"to": "destination", "body": "payload"}


def write_tool(fn, *, policy="allow") -> Tool:
    return Tool(
        "external_write",
        "Synthetic external write.",
        {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "body"],
            "additionalProperties": False,
        },
        fn,
        risk="high",
        read_only=False,
        capabilities=("external.write",),
        default_policy=policy,
        operation="send",
        target_arg="to",
        resource_type="message",
    )


def uncertain_execution(conn, fn=lambda **_args: "done", *, policy="allow"):
    store = ExecutionStore(conn)
    tool = write_tool(fn, policy=policy)
    registry = ToolRegistry(execution_store=store)
    registry.register(tool)
    action = _build_action(tool, ARGS, ARGS)
    decision = registry.kernel.authorize(action, default_policy="allow")
    fingerprint = decision.action_fingerprint
    store.claim(fingerprint, tool.name)
    store.mark_uncertain(fingerprint, '{"ok":false,"error":{"code":"uncertain"}}')
    return fingerprint, store


def recovery(conn, settings=None):
    replay = ReplayService(conn, settings) if settings else None
    return RecoveryService(RecoveryStore(conn), replay=replay)


class FixedVerifier:
    def __init__(self, status):
        self.status = status

    def verify(self, _task, _step, _execution):
        return VerificationResult(self.status, f"recovery verification {self.status.value}")


class NoopRunner:
    def __init__(self):
        self.calls = 0

    def __call__(self, _task, _step, _context, _observer):
        self.calls += 1
        return StepExecution("unused")


def blocked_task(conn, replay, fingerprint, verifier, runner=None):
    run = replay.start_run(
        session_id="task",
        source="task",
        role="main",
        model="offline",
        provider="offline",
        user_input="",
    )
    replay.record_event(
        run.run_id,
        "tool_execution_uncertain",
        {
            "tool": "external_write",
            "action_fingerprint": fingerprint,
            "status": "uncertain",
        },
    )
    replay.complete_run(
        run.run_id,
        output="blocked",
        iterations=1,
        latency_ms=1,
        role="main",
        model="offline",
        provider="offline",
    )
    store = TaskStore(conn)
    task = store.create_task(
        "recover external write",
        [PlanStep("Write", "Perform the write.", "Verify the write completed.")],
        source="test",
    )
    claim = store.claim_next_step(task.task_id)
    store.attach_run_id(claim.step.step_id, run.run_id)
    store.finish_step(
        claim.step.step_id,
        result="uncertain",
        verification=VerificationResult(VerificationStatus.BLOCKED, "uncertain action"),
    )
    actual_runner = runner or NoopRunner()
    service = TaskService(
        store,
        CallableTaskPlanner(lambda _goal: []),
        TaskExecutor(store, actual_runner, verifier, replay=replay),
    )
    return service, actual_runner, task.task_id


def test_uncertain_execution_is_safely_inspectable(tmp_path):
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(conn)
    service = recovery(conn)
    listed = service.list_uncertain()
    shown = service.show_execution(fingerprint)
    assert listed[0]["action_fingerprint"] == fingerprint
    assert shown["execution"]["status"] == "uncertain"
    assert set(shown["execution"]) >= {
        "tool_name", "attempt_count", "started_at", "updated_at", "result"
    }
    assert "body" not in json.dumps(shown)


def test_runtime_timeout_is_classified_as_blocked_uncertainty():
    verifier = LayeredTaskVerifier()
    execution = StepExecution(
        "timeout",
        tool_calls=(
            {
                "tool": "external_write",
                "output": json.dumps(
                    {"ok": False, "error": {"code": "tool_timeout"}}
                ),
            },
        ),
    )
    result = verifier.verify(None, None, execution)
    assert result.status is VerificationStatus.BLOCKED


def test_confirmed_completed_reconciles_without_tool_execution(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    fingerprint, store = uncertain_execution(
        conn, lambda **_args: calls.__setitem__("count", calls["count"] + 1)
    )
    result = recovery(conn).resolve_execution(
        fingerprint, RecoveryResolution.CONFIRMED_COMPLETED
    )
    record = store.get(fingerprint)
    assert calls["count"] == 0
    assert record.status is ExecutionStatus.COMPLETED
    assert record.completion_source == "human_reconciliation"
    assert json.loads(record.result)["reconciled"] is True
    assert result["decision"]["resolution"] is RecoveryResolution.CONFIRMED_COMPLETED


def test_completed_reconciliation_survives_restart(tmp_path):
    first = connect(tmp_path)
    fingerprint, _store = uncertain_execution(first)
    recovery(first).resolve_execution(fingerprint, "confirmed_completed")
    first.close()
    second = connect(tmp_path)
    shown = recovery(second).show_execution(fingerprint)
    assert shown["execution"]["status"] == "completed"
    assert shown["execution"]["completion_source"] == "human_reconciliation"
    assert len(shown["recovery_decisions"]) == 1


def test_confirmed_not_executed_creates_permit_but_does_not_execute(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(
        conn, lambda **_args: calls.__setitem__("count", calls["count"] + 1)
    )
    recovery(conn).resolve_execution(fingerprint, "confirmed_not_executed")
    permit = RecoveryStore(conn).get_permit(fingerprint)
    assert calls["count"] == 0
    assert permit is not None and permit.consumed_at is None


def test_manual_retry_still_requires_current_trust_and_preserves_permit(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    fingerprint, store = uncertain_execution(conn)
    recovery(conn).resolve_execution(fingerprint, "confirmed_not_executed")
    denied = ToolRegistry(execution_store=store)
    denied.register(
        write_tool(lambda **_args: calls.__setitem__("count", 1) or "wrong", policy="deny")
    )
    output = json.loads(denied.execute("external_write", ARGS))
    assert output["error"]["code"] == "tool_permission_denied"
    assert calls["count"] == 0
    assert RecoveryStore(conn).get_permit(fingerprint).consumed_at is None


def test_allowed_manual_retry_executes_once_and_consumes_permit(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    fingerprint, store = uncertain_execution(conn)
    recovery(conn).resolve_execution(fingerprint, "confirmed_not_executed")
    registry = ToolRegistry(execution_store=store)
    registry.register(
        write_tool(
            lambda **_args: calls.__setitem__("count", calls["count"] + 1) or "retried"
        )
    )
    assert registry.execute("external_write", ARGS) == "retried"
    assert registry.execute("external_write", ARGS) == "retried"
    assert calls["count"] == 1
    assert store.get(fingerprint).attempt_count == 2
    assert RecoveryStore(conn).get_permit(fingerprint).consumed_at is not None


def test_manual_retry_survives_restart(tmp_path):
    first = connect(tmp_path)
    fingerprint, _store = uncertain_execution(first)
    recovery(first).resolve_execution(fingerprint, "confirmed_not_executed")
    first.close()
    calls = {"count": 0}
    second = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(second))
    registry.register(
        write_tool(lambda **_args: calls.__setitem__("count", 1) or "after restart")
    )
    assert registry.execute("external_write", ARGS) == "after restart"
    assert calls["count"] == 1
    assert RecoveryStore(second).get_permit(fingerprint).consumed_at is not None


def test_concurrent_manual_retry_consumption_executes_once(tmp_path):
    setup = connect(tmp_path)
    fingerprint, _store = uncertain_execution(setup)
    recovery(setup).resolve_execution(fingerprint, "confirmed_not_executed")
    setup.close()
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = {"count": 0}

    def body(**_args):
        with lock:
            calls["count"] += 1
        entered.set()
        assert release.wait(3)
        return "retried"

    registries = []
    connections = []
    for _index in range(2):
        conn = connect(tmp_path, check_same_thread=False)
        registry = ToolRegistry(execution_store=ExecutionStore(conn))
        registry.register(write_tool(body))
        connections.append(conn)
        registries.append(registry)
    outputs = []
    one = threading.Thread(target=lambda: outputs.append(registries[0].execute("external_write", ARGS)))
    two = threading.Thread(target=lambda: outputs.append(registries[1].execute("external_write", ARGS)))
    one.start()
    assert entered.wait(3)
    two.start()
    two.join(3)
    assert not two.is_alive()
    release.set()
    one.join(3)
    assert calls["count"] == 1
    assert any("tool_execution_in_progress" in output for output in outputs)
    for conn in connections:
        conn.close()


def test_conflicting_resolution_rejected_and_same_resolution_idempotent(tmp_path):
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(conn)
    service = recovery(conn)
    first = service.resolve_execution(fingerprint, "confirmed_completed")
    duplicate = service.resolve_execution(fingerprint, "confirmed_completed")
    with pytest.raises(RecoveryError, match="different recovery decision"):
        service.resolve_execution(fingerprint, "confirmed_not_executed")
    assert first["decision"]["recovery_id"] == duplicate["decision"]["recovery_id"]
    assert duplicate["created"] is False
    assert len(RecoveryStore(conn).get_decisions("execution", fingerprint)) == 1


def test_recovery_decision_rows_are_database_enforced_append_only(tmp_path):
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(conn)
    decision = recovery(conn).resolve_execution(fingerprint, "confirmed_completed")["decision"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE recovery_decisions SET note='changed' WHERE recovery_id=?",
            (decision["recovery_id"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM recovery_decisions WHERE recovery_id=?",
            (decision["recovery_id"],),
        )
    conn.rollback()


def test_recovery_note_and_replay_are_secret_safe(tmp_path):
    secret = "SUPER_SECRET_RECOVERY_VALUE"
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(conn)
    recovery(conn, settings).resolve_execution(
        fingerprint,
        "confirmed_completed",
        note=f"api_key={secret}",
    )
    rows = conn.execute(
        "SELECT note FROM recovery_decisions UNION ALL SELECT payload_json FROM replay_events"
    ).fetchall()
    assert secret not in json.dumps([tuple(row) for row in rows])


def test_completed_action_task_recovery_requires_and_passes_verification(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    fingerprint, _store = uncertain_execution(conn)
    service = recovery(conn, settings)
    service.resolve_execution(fingerprint, "confirmed_completed")
    tasks, runner, task_id = blocked_task(
        conn, replay, fingerprint, FixedVerifier(VerificationStatus.PASS)
    )
    result = tasks.recover(task_id)
    assert runner.calls == 0
    assert result["step"]["status"] is StepStatus.SUCCEEDED
    assert result["task"]["status"] is TaskStatus.COMPLETED


def test_inconclusive_recovery_verification_keeps_task_blocked(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    fingerprint, _store = uncertain_execution(conn)
    recovery(conn, settings).resolve_execution(fingerprint, "confirmed_completed")
    tasks, runner, task_id = blocked_task(
        conn, replay, fingerprint, FixedVerifier(VerificationStatus.UNKNOWN)
    )
    result = tasks.recover(task_id)
    assert runner.calls == 0
    assert result["code"] == "verification_blocked"
    assert tasks.store.get_task(task_id).status is TaskStatus.BLOCKED


def test_not_executed_task_recovery_prepares_but_does_not_run_step(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    fingerprint, _store = uncertain_execution(conn)
    recovery(conn, settings).resolve_execution(fingerprint, "confirmed_not_executed")
    tasks, runner, task_id = blocked_task(
        conn, replay, fingerprint, FixedVerifier(VerificationStatus.PASS)
    )
    result = tasks.recover(task_id)
    assert result["code"] == "retry_prepared"
    assert runner.calls == 0
    assert tasks.store.list_steps(task_id)[0].status is StepStatus.PENDING


def test_explicit_task_run_after_recovery_uses_normal_trust_and_ledger_path(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    calls = {"count": 0}
    fingerprint, ledger = uncertain_execution(conn)
    recovery(conn, settings).resolve_execution(fingerprint, "confirmed_not_executed")
    registry = ToolRegistry(execution_store=ledger)
    registry.register(
        write_tool(
            lambda **_args: calls.__setitem__("count", calls["count"] + 1) or "retried"
        )
    )

    class GovernedRunner:
        def __call__(self, _task, _step, _context, _observer):
            output = registry.execute("external_write", ARGS)
            return StepExecution(
                "observable retry",
                "",
                ({"tool": "external_write", "output": output},),
            )

    tasks, _runner, task_id = blocked_task(
        conn,
        replay,
        fingerprint,
        LayeredTaskVerifier(),
        runner=GovernedRunner(),
    )
    assert tasks.recover(task_id)["code"] == "retry_prepared"
    assert calls["count"] == 0
    result = tasks.run(task_id)[0]
    assert calls["count"] == 1
    assert result.step.status is StepStatus.SUCCEEDED
    assert result.task.status is TaskStatus.COMPLETED
    assert RecoveryStore(conn).get_permit(fingerprint).consumed_at is not None


def test_trust_denied_task_recovery_reblocks_without_side_effect(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task(
        "denied task",
        [PlanStep("Write", "Write.", "Verify.")],
        source="test",
    )
    claim = store.claim_next_step(task.task_id)
    store.finish_step(
        claim.step.step_id,
        result="denied",
        verification=VerificationResult(VerificationStatus.BLOCKED, "Trust denied"),
    )
    calls = {"count": 0}
    registry = ToolRegistry(execution_store=ExecutionStore(conn))
    registry.register(
        write_tool(lambda **_args: calls.__setitem__("count", 1) or "wrong", policy="deny")
    )

    class DeniedRunner:
        def __call__(self, _task, _step, _context, _observer):
            output = registry.execute("external_write", ARGS)
            return StepExecution("denied", "", ({"tool": "external_write", "output": output},))

    tasks = TaskService(
        store,
        CallableTaskPlanner(lambda _goal: []),
        TaskExecutor(store, DeniedRunner(), LayeredTaskVerifier()),
    )
    tasks.recover(task.task_id)
    result = tasks.run(task.task_id)[0]
    assert result.task.status is TaskStatus.BLOCKED
    assert calls["count"] == 0


def test_abandoned_action_keeps_task_blocked_and_has_no_permit(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    fingerprint, _store = uncertain_execution(conn)
    recovery(conn, settings).resolve_execution(fingerprint, "abandoned")
    tasks, runner, task_id = blocked_task(
        conn, replay, fingerprint, FixedVerifier(VerificationStatus.PASS)
    )
    result = tasks.recover(task_id)
    assert result["code"] == "action_abandoned"
    assert runner.calls == 0
    assert tasks.store.get_task(task_id).status is TaskStatus.BLOCKED
    assert RecoveryStore(conn).get_permit(fingerprint) is None


def test_healthy_completed_record_cannot_be_reconciled(tmp_path):
    conn = connect(tmp_path)
    fingerprint, store = uncertain_execution(conn)
    store.complete(fingerprint, "late completion")
    with pytest.raises(RecoveryError, match="Only an uncertain"):
        recovery(conn).resolve_execution(fingerprint, "confirmed_completed")


def test_human_completed_record_never_bypasses_tightened_trust(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    fingerprint, store = uncertain_execution(conn)
    recovery(conn).resolve_execution(fingerprint, "confirmed_completed")
    registry = ToolRegistry(execution_store=store)
    registry.register(
        write_tool(lambda **_args: calls.__setitem__("count", 1) or "wrong", policy="deny")
    )
    output = json.loads(registry.execute("external_write", ARGS))
    assert output["error"]["code"] == "tool_permission_denied"
    assert calls["count"] == 0


def test_m16_runtime_scoped_rerun_semantics_are_unchanged(tmp_path):
    conn = connect(tmp_path)
    registry = ToolRegistry(
        execution_store=ExecutionStore(conn),
        trust_policy={"tools": {"run_command": "allow"}},
    )
    registry.register(command_tool(CommandRunner(CommandPolicy(tmp_path))))
    args = {
        "argv": [
            __import__("sys").executable,
            "-c",
            (
                "from pathlib import Path; p=Path('m17.txt'); "
                "p.write_text((p.read_text() if p.exists() else '')+'x')"
            ),
        ],
        "cwd": str(tmp_path),
    }
    registry.execute("run_command", args, context={"execution_scope": "first"})
    registry.execute("run_command", args, context={"execution_scope": "second"})
    assert (tmp_path / "m17.txt").read_text() == "xx"


def test_m14_healthy_global_external_write_still_executes_once(tmp_path):
    calls = {"count": 0}
    conn = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(conn))
    registry.register(
        write_tool(lambda **_args: calls.__setitem__("count", calls["count"] + 1) or "done")
    )
    assert registry.execute("external_write", ARGS) == "done"
    assert registry.execute("external_write", ARGS) == "done"
    assert calls["count"] == 1


def test_replay_records_recovery_and_manual_retry_provenance(tmp_path):
    settings = Settings(home=tmp_path)
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    fingerprint, store = uncertain_execution(conn)
    service = recovery(conn, settings)
    resolved = service.resolve_execution(fingerprint, "confirmed_not_executed")
    decision_run = resolved["decision"]["replay_run_id"]
    decision_events = {event["event_type"] for event in replay.get_events(decision_run)}
    assert {"recovery_decision", "execution_reconciled", "manual_retry_authorized"} <= decision_events

    recorder = ReplayRecorder(replay)
    retry_run = recorder.start(
        session_id="retry",
        source="test",
        role="main",
        model="offline",
        provider="offline",
        user_input="",
    )
    registry = ToolRegistry(execution_store=store)
    registry.register(write_tool(lambda **_args: "retried"))
    registry.execute("external_write", ARGS, notify=recorder.event)
    recorder.complete(
        output="done", iterations=1, latency_ms=1, role="main", model="offline", provider="offline"
    )
    assert "manual_retry_consumed" in {
        event["event_type"] for event in replay.get_events(retry_run)
    }


def test_recovery_subsystem_has_no_direct_tool_or_process_invocation():
    root = Path(__file__).parents[2] / "tieru" / "recovery"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert ".fn(" not in source
    assert "subprocess." not in source


def test_recovery_cli_inspection_resolution_and_json(tmp_path, capsys):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(conn)
    conn.close()
    parser = _parser()
    list_args = parser.parse_args(["recovery", "list", "--json"])
    assert run_recovery_cli(list_args, settings) == 0
    assert json.loads(capsys.readouterr().out)[0]["action_fingerprint"] == fingerprint
    check = connect(tmp_path)
    assert check.execute("SELECT COUNT(*) FROM recovery_decisions").fetchone()[0] == 0
    check.close()
    show_args = parser.parse_args(["recovery", "show", fingerprint, "--json"])
    assert run_recovery_cli(show_args, settings) == 0
    assert json.loads(capsys.readouterr().out)["execution"]["status"] == "uncertain"
    resolve_args = parser.parse_args(
        [
            "recovery", "resolve-execution", fingerprint,
            "--resolution", "abandoned", "--yes", "--json",
        ]
    )
    assert run_recovery_cli(resolve_args, settings) == 0
    assert json.loads(capsys.readouterr().out)["execution"]["status"] == "uncertain"


def test_recovery_cli_requires_explicit_yes(tmp_path, capsys):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    conn = connect(tmp_path)
    fingerprint, _store = uncertain_execution(conn)
    conn.close()
    args = _parser().parse_args(
        ["recovery", "resolve-execution", fingerprint, "--resolution", "completed"]
    )
    assert run_recovery_cli(args, settings) == 2
    assert "requires --yes" in capsys.readouterr().err
    check = connect(tmp_path)
    assert check.execute("SELECT COUNT(*) FROM recovery_decisions").fetchone()[0] == 0
    check.close()


@pytest.mark.parametrize("resolution", ["completed", "not-executed", "abandoned"])
def test_recovery_cli_accepts_all_explicit_resolution_names(resolution):
    args = _parser().parse_args(
        ["recovery", "resolve-execution", "fingerprint", "--resolution", resolution, "--yes"]
    )
    assert args.recovery_command == "resolve-execution"
    assert args.yes is True


def test_task_recover_cli_is_explicit_and_separate_from_inspection():
    parser = _parser()
    recover_args = parser.parse_args(["task", "recover", "task-id", "--yes", "--json"])
    show_args = parser.parse_args(["task", "show", "task-id", "--json"])
    assert recover_args.task_command == "recover" and recover_args.yes is True
    assert show_args.task_command == "show" and not hasattr(show_args, "yes")


def test_task_recover_cli_prepares_step_without_executing(tmp_path, capsys, monkeypatch):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    setup = connect(tmp_path)
    store = TaskStore(setup)
    task = store.create_task(
        "recover CLI task",
        [PlanStep("Blocked", "Do work.", "Verify work.")],
        source="test",
    )
    claim = store.claim_next_step(task.task_id)
    store.finish_step(
        claim.step.step_id,
        result="denied",
        verification=VerificationResult(VerificationStatus.BLOCKED, "Trust denied"),
    )
    setup.close()
    runner = NoopRunner()

    class FakeTieru:
        def __init__(self, *, settings):
            self.conn = connect(settings.home)
            task_store = TaskStore(self.conn)
            self.tasks = TaskService(
                task_store,
                CallableTaskPlanner(lambda _goal: []),
                TaskExecutor(task_store, runner, FixedVerifier(VerificationStatus.PASS)),
            )

        def close(self):
            return None

    monkeypatch.setattr("tieru.app.Tieru", FakeTieru)
    args = _parser().parse_args(["task", "recover", task.task_id, "--yes", "--json"])
    assert run_task_cli(args, settings) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["code"] == "retry_prepared"
    assert output["step"]["status"] == "pending"
    assert runner.calls == 0

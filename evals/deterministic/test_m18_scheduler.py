"""Deterministic M18 local persistent scheduler contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from evals.helpers import ScriptedClient, response, text_block, tool_block
from tieru.__main__ import _parser
from tieru.config import Settings
from tieru.db import connect
from tieru.execution import ExecutionStore
from tieru.loop.agent import run_loop
from tieru.replay import ReplayNormalizer, ReplayService
from tieru.scheduler import (
    SchedulerRunner,
    SchedulerService,
    ScheduleRunStatus,
    ScheduleStatus,
    ScheduleStore,
    ScheduleValidationError,
    TriggerType,
)
from tieru.scheduler.cli import run_schedule_cli
from tieru.scheduler.recurrence import latest_due, parse_interval
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    PlanStep,
    StepExecution,
    TaskLimits,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.planner import CallableTaskPlanner
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import LayeredTaskVerifier
from tieru.tools.registry import Tool, ToolRegistry

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)
PLAN = [PlanStep("Act", "Perform the bounded action.", "Observable result passed.")]


class FixedVerifier:
    def __init__(self, status=VerificationStatus.PASS):
        self.status = status

    def verify(self, _task, _step, _execution):
        return VerificationResult(self.status, f"verification {self.status.value}")


class CountingStepRunner:
    def __init__(self):
        self.calls = 0

    def __call__(self, _task, _step, _context, _observer):
        self.calls += 1
        return StepExecution("done", "", ())


def task_service(conn, *, plan=None, runner=None, verifier=None, replay=None):
    store = TaskStore(conn, limits=TaskLimits(running_step_stale_after_seconds=1))
    actual = runner or CountingStepRunner()
    return (
        TaskService(
            store,
            CallableTaskPlanner(lambda _goal: list(plan or PLAN)),
            TaskExecutor(
                store, actual, verifier or FixedVerifier(), replay=replay,
                limits=store.limits,
            ),
        ),
        actual,
    )


def setup(tmp_path, *, now=NOW, plan=None, runner=None, verifier=None, replay=None):
    conn = connect(tmp_path)
    tasks, step_runner = task_service(
        conn, plan=plan, runner=runner, verifier=verifier, replay=replay
    )
    store = ScheduleStore(conn)
    service = SchedulerService(store, clock=lambda: now)
    tick = SchedulerRunner(store, tasks, replay=replay, clock=lambda: now)
    return conn, tasks, step_runner, store, service, tick


def due_once(service, *, name="once", goal="do it"):
    return service.create_once(name=name, goal=goal, at=NOW - timedelta(seconds=1))


def test_01_scheduler_schema_is_additive_and_inspectable(tmp_path):
    conn = connect(tmp_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schedules", "schedule_runs", "tasks"} <= tables
    assert "source_id" in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}


def test_02_create_once_persists_aware_time_and_timezone(tmp_path):
    _c, _t, _r, _s, service, _tick = setup(tmp_path)
    schedule = service.create_once(
        name="brief", goal="make brief", at="2026-07-02T08:00:00",
        timezone_name="Asia/Saigon",
    )
    assert schedule.trigger_type is TriggerType.ONCE
    assert schedule.timezone == "Asia/Saigon"
    assert schedule.next_run_at.endswith("+00:00")


def test_03_create_interval_persists_fixed_elapsed_trigger(tmp_path):
    _c, _t, _r, _s, service, _tick = setup(tmp_path)
    schedule = service.create_interval(
        name="scan", goal="scan", every="6h", anchor=NOW
    )
    assert json.loads(schedule.trigger_spec)["seconds"] == 21600
    assert schedule.overlap_policy == "forbid" and schedule.misfire_policy == "latest"


def test_04_invalid_timezone_is_rejected_before_persistence(tmp_path):
    conn, _t, _r, store, service, _tick = setup(tmp_path)
    with pytest.raises(ScheduleValidationError, match="unknown timezone"):
        service.create_once(name="x", goal="x", at=NOW, timezone_name="Mars/Olympus")
    assert store.list() == []
    assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0


def test_05_interval_bounds_are_enforced():
    with pytest.raises(ScheduleValidationError):
        parse_interval("1m")
    with pytest.raises(ScheduleValidationError):
        parse_interval("9999d")
    assert parse_interval("5m") == 300


def test_06_schedule_creation_does_not_create_or_run_task(tmp_path):
    conn, _tasks, runner, _store, service, _tick = setup(tmp_path)
    due_once(service)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert runner.calls == 0


def test_07_due_once_materializes_fresh_task_and_completes(tmp_path):
    _c, _tasks, runner, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    result = tick.tick()
    occurrence = store.list_runs(schedule.schedule_id)[0]
    assert result.materialized == result.advanced == result.reconciled == 1
    assert occurrence.status is ScheduleRunStatus.COMPLETED and runner.calls == 1
    assert store.get(schedule.schedule_id).status is ScheduleStatus.COMPLETED


def test_08_scheduled_task_has_durable_source_provenance(tmp_path):
    _c, tasks, _r, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    tick.tick()
    occurrence = store.list_runs(schedule.schedule_id)[0]
    task = tasks.store.get_task(occurrence.task_id)
    assert task.source == "scheduled" and task.source_id == occurrence.run_id
    assert task.session_id == f"schedule:{schedule.schedule_id}"


def test_09_task_source_identity_is_idempotent(tmp_path):
    conn = connect(tmp_path)
    tasks, _runner = task_service(conn)
    first = tasks.create(goal="x", source="scheduled", source_id="occurrence-1")
    second = tasks.create(goal="x", source="scheduled", source_id="occurrence-1")
    assert first.task_id == second.task_id
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_10_occurrence_identity_is_unique(tmp_path):
    conn, _t, _r, store, service, _tick = setup(tmp_path)
    schedule = due_once(service)
    store.claim_due(NOW, limit=1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO schedule_runs
               (run_id,schedule_id,scheduled_for,status,claimed_at,created_at,updated_at)
               SELECT 'duplicate',schedule_id,scheduled_for,'claimed',claimed_at,created_at,updated_at
               FROM schedule_runs"""
        )
    assert len(store.list_runs(schedule.schedule_id)) == 1


def test_11_concurrent_ticks_claim_one_occurrence(tmp_path):
    setup_conn = connect(tmp_path)
    store = ScheduleStore(setup_conn)
    SchedulerService(store).create_once(name="race", goal="race", at=NOW - timedelta(seconds=1))
    setup_conn.close()
    first = ScheduleStore(connect(tmp_path, check_same_thread=False))
    second = ScheduleStore(connect(tmp_path, check_same_thread=False))
    barrier = threading.Barrier(2)
    results = []

    def claim(candidate):
        barrier.wait()
        results.append(candidate.claim_due(NOW, limit=1))

    threads = [threading.Thread(target=claim, args=(candidate,)) for candidate in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sum(len(value) for value in results) == 1
    assert first.conn.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()[0] == 1


def test_12_claimed_without_task_is_materialized_after_crash(tmp_path):
    _c, _t, runner, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    claimed = store.claim_due(NOW, limit=1)[0]
    assert claimed.task_id is None
    tick.tick()
    assert store.get_run(claimed.run_id).task_id
    assert runner.calls == 1 and len(store.list_runs(schedule.schedule_id)) == 1


def test_13_task_created_before_link_is_reattached_without_duplicate(tmp_path):
    conn, tasks, runner, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    occurrence = store.claim_due(NOW, limit=1)[0]
    task = tasks.create(
        goal=schedule.goal, source="scheduled", source_id=occurrence.run_id,
        session_id=f"schedule:{schedule.schedule_id}",
    )
    tick.tick()
    assert store.get_run(occurrence.run_id).task_id == task.task_id
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert runner.calls == 1


def test_14_completed_task_with_missing_link_reconciles_without_rerun(tmp_path):
    conn, tasks, runner, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    occurrence = store.claim_due(NOW, limit=1)[0]
    task = tasks.create(
        goal=schedule.goal, source="scheduled", source_id=occurrence.run_id
    )
    tasks.run(task.task_id)
    assert runner.calls == 1
    tick.tick()
    assert store.get_run(occurrence.run_id).status is ScheduleRunStatus.COMPLETED
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert runner.calls == 1


def test_15_restart_recovers_persisted_occurrence(tmp_path):
    first, _tasks, _runner, store, service, _tick = setup(tmp_path)
    schedule = due_once(service)
    occurrence = store.claim_due(NOW, limit=1)[0]
    first.close()
    second = connect(tmp_path)
    tasks, runner = task_service(second)
    reopened = ScheduleStore(second)
    SchedulerRunner(reopened, tasks, clock=lambda: NOW).tick()
    assert reopened.get_run(occurrence.run_id).status is ScheduleRunStatus.COMPLETED
    assert len(reopened.list_runs(schedule.schedule_id)) == 1 and runner.calls == 1


def test_16_pause_prevents_occurrence_creation(tmp_path):
    _c, _t, _r, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    service.pause(schedule.schedule_id)
    tick.tick()
    assert store.list_runs(schedule.schedule_id) == []


def test_17_resume_coalesces_paused_misfire(tmp_path):
    current = {"now": NOW}
    conn = connect(tmp_path)
    tasks, _runner = task_service(conn)
    store = ScheduleStore(conn)
    service = SchedulerService(store, clock=lambda: current["now"])
    schedule = service.create_interval(name="x", goal="x", every="6h", anchor=NOW)
    service.pause(schedule.schedule_id)
    current["now"] = NOW + timedelta(hours=25)
    service.resume(schedule.schedule_id)
    SchedulerRunner(store, tasks, clock=lambda: current["now"]).tick()
    runs = store.list_runs(schedule.schedule_id)
    assert len(runs) == 1 and runs[0].scheduled_for == (NOW + timedelta(hours=24)).isoformat(timespec="milliseconds")


def test_18_cancel_prevents_future_occurrences(tmp_path):
    _c, _t, _r, store, service, tick = setup(tmp_path)
    schedule = due_once(service)
    service.cancel(schedule.schedule_id)
    tick.tick()
    assert store.get(schedule.schedule_id).status is ScheduleStatus.CANCELLED
    assert store.list_runs(schedule.schedule_id) == []


def test_19_misfire_policy_runs_latest_at_most_once(tmp_path):
    current = NOW + timedelta(hours=25)
    _c, _t, _r, store, service, tick = setup(tmp_path, now=current)
    schedule = service.create_interval(
        name="x", goal="x", every="6h", anchor=NOW
    )
    tick.tick()
    runs = store.list_runs(schedule.schedule_id)
    assert len(runs) == 1
    assert runs[0].scheduled_for == (NOW + timedelta(hours=24)).isoformat(timespec="milliseconds")


def test_20_interval_advances_from_logical_time_without_drift(tmp_path):
    current = NOW + timedelta(hours=25, minutes=37)
    _c, _t, _r, store, service, tick = setup(tmp_path, now=current)
    schedule = service.create_interval(name="x", goal="x", every="6h", anchor=NOW)
    tick.tick()
    assert store.get(schedule.schedule_id).next_run_at == (
        NOW + timedelta(hours=30)
    ).isoformat(timespec="milliseconds")
    dst = service.create_interval(
        name="dst", goal="x", every="1h",
        anchor="2026-03-08T01:30:00", timezone_name="America/New_York",
    )
    assert dst.next_run_at == "2026-03-08T06:30:00.000+00:00"
    latest, successor = latest_due(
        TriggerType.INTERVAL, dst.trigger_spec, dst.next_run_at,
        datetime(2026, 3, 8, 7, 31, tzinfo=UTC),
    )
    assert latest.hour == 7 and successor.hour == 8


def test_21_overlap_forbid_records_skip_and_no_second_task(tmp_path):
    current = {"now": NOW}
    conn, _tasks, runner, store, service, _tick = setup(
        tmp_path, verifier=FixedVerifier(VerificationStatus.BLOCKED)
    )
    schedule = service.create_interval(name="x", goal="x", every="6h", anchor=NOW)
    tick = SchedulerRunner(store, _tasks, clock=lambda: current["now"])
    tick.tick()
    current["now"] = NOW + timedelta(hours=6)
    result = tick.tick()
    assert result.skipped_overlap == 1
    assert {run.status for run in store.list_runs(schedule.schedule_id)} == {
        ScheduleRunStatus.BLOCKED, ScheduleRunStatus.SKIPPED_OVERLAP
    }
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert runner.calls == 1


def test_22_trust_denial_blocks_scheduled_task_without_side_effect(tmp_path):
    counter = {"count": 0}
    conn = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(conn))
    registry.register(_write_tool(counter, policy="deny"))
    loop_runner = LoopStepRunner(registry)
    tasks, _ = task_service(
        conn, runner=loop_runner, verifier=LayeredTaskVerifier()
    )
    store = ScheduleStore(conn)
    schedule = SchedulerService(store).create_once(
        name="deny", goal="write", at=NOW - timedelta(seconds=1)
    )
    SchedulerRunner(store, tasks, clock=lambda: NOW).tick()
    occurrence = store.list_runs(schedule.schedule_id)[0]
    assert occurrence.status is ScheduleRunStatus.BLOCKED
    assert counter["count"] == 0


def test_23_confirmation_is_rechecked_at_occurrence_time(tmp_path):
    counter = {"count": 0}
    conn = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(conn))
    registry.register(_write_tool(counter, policy="confirm"))
    tasks, _ = task_service(
        conn, runner=LoopStepRunner(registry), verifier=LayeredTaskVerifier()
    )
    store = ScheduleStore(conn)
    schedule = SchedulerService(store).create_once(
        name="confirm", goal="write later", at=NOW - timedelta(seconds=1)
    )
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    SchedulerRunner(store, tasks, clock=lambda: NOW).tick()
    assert store.list_runs(schedule.schedule_id)[0].status is ScheduleRunStatus.BLOCKED
    assert counter["count"] == 0


def test_24_blocked_occurrence_is_not_automatically_force_retried(tmp_path):
    conn, _tasks, runner, store, service, tick = setup(
        tmp_path, verifier=FixedVerifier(VerificationStatus.BLOCKED)
    )
    schedule = due_once(service)
    tick.tick()
    tick.tick()
    assert store.list_runs(schedule.schedule_id)[0].status is ScheduleRunStatus.BLOCKED
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert runner.calls == 1


def test_25_explicit_task_recovery_allows_later_tick_progress(tmp_path):
    conn, tasks, runner, store, service, tick = setup(
        tmp_path, verifier=FixedVerifier(VerificationStatus.BLOCKED)
    )
    schedule = due_once(service)
    tick.tick()
    occurrence = store.list_runs(schedule.schedule_id)[0]
    tasks.store.prepare_blocked_step_retry(occurrence.task_id)
    tasks.executor.verifier = FixedVerifier(VerificationStatus.PASS)
    tick.tick()
    assert store.get_run(occurrence.run_id).status is ScheduleRunStatus.COMPLETED
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert runner.calls == 2


def test_26_action_ledger_suppresses_duplicate_scheduled_side_effect(tmp_path):
    counter = {"count": 0}
    current = {"now": NOW}
    conn = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(conn))
    registry.register(_write_tool(counter, policy="allow"))
    tasks, _ = task_service(
        conn, runner=LoopStepRunner(registry), verifier=LayeredTaskVerifier()
    )
    store = ScheduleStore(conn)
    schedule = SchedulerService(store).create_interval(
        name="ledger", goal="same write", every="6h", anchor=NOW
    )
    tick = SchedulerRunner(store, tasks, clock=lambda: current["now"])
    tick.tick()
    current["now"] = NOW + timedelta(hours=6)
    tick.tick()
    assert len(store.list_runs(schedule.schedule_id)) == 2
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert counter["count"] == 1


def test_27_recurring_occurrences_materialize_fresh_tasks(tmp_path):
    current = {"now": NOW}
    conn, tasks, _runner, store, service, _tick = setup(tmp_path)
    schedule = service.create_interval(name="fresh", goal="x", every="6h", anchor=NOW)
    tick = SchedulerRunner(store, tasks, clock=lambda: current["now"])
    tick.tick()
    current["now"] += timedelta(hours=6)
    tick.tick()
    ids = {run.task_id for run in store.list_runs(schedule.schedule_id)}
    assert len(ids) == 2 and None not in ids
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_28_tick_honors_max_occurrence_bound(tmp_path):
    _c, _t, _r, store, service, tick = setup(tmp_path)
    for index in range(3):
        due_once(service, name=f"x-{index}")
    first = tick.tick(max_occurrences=2)
    assert first.claimed == 2
    assert sum(len(store.list_runs(item.schedule_id)) for item in store.list()) == 2


def test_29_replay_records_safe_scheduler_events(tmp_path):
    conn = connect(tmp_path)
    replay = ReplayService(conn, Settings(home=tmp_path))
    tasks, _runner = task_service(conn, replay=replay)
    store = ScheduleStore(conn)
    service = SchedulerService(store)
    due_once(service, goal="secret goal api_key=hidden-value")
    SchedulerRunner(store, tasks, replay=replay, clock=lambda: NOW).tick()
    run = next(item for item in replay.list_runs(limit=10) if item["source"] == "scheduler")
    events = replay.get_events(run["run_id"])
    assert any(event["category"] == "scheduler" for event in events)
    assert "hidden-value" not in json.dumps(events)


def test_30_replay_normalizer_omits_scheduler_goal_contents():
    event = ReplayNormalizer().normalize(
        "schedule_task_materialized", {"schedule_id": "x", "goal": "api_key=hidden"}
    )
    assert event.category == "scheduler"
    assert "goal" not in event.safe_payload and "hidden" not in json.dumps(event.safe_payload)


def test_31_cli_parser_exposes_required_schedule_commands():
    parser = _parser()
    for command in ("create", "list", "show", "pause", "resume", "cancel", "runs", "tick"):
        argv = ["schedule", command]
        if command == "create":
            argv += ["goal", "--name", "name", "--every", "6h"]
        elif command in {"show", "pause", "resume", "cancel", "runs"}:
            argv += ["schedule-id"]
        args = parser.parse_args(argv)
        assert args.schedule_command == command


def test_32_cli_create_list_show_pause_resume_cancel_and_runs(tmp_path, capsys):
    settings = Settings(home=tmp_path)
    parser = _parser()
    create = parser.parse_args([
        "schedule", "create", "goal", "--name", "nightly", "--every", "6h", "--json"
    ])
    assert run_schedule_cli(create, settings) == 0
    schedule_id = json.loads(capsys.readouterr().out)["schedule_id"]
    for command in ("list", "show", "pause", "resume", "runs", "cancel"):
        argv = ["schedule", command]
        if command not in {"list"}:
            argv.append(schedule_id)
        argv.append("--json")
        assert run_schedule_cli(parser.parse_args(argv), settings) == 0
        assert capsys.readouterr().out.strip()


def _write_tool(counter, *, policy):
    return Tool(
        "scheduled_write", "Synthetic scheduled write.",
        {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        },
        lambda value: counter.__setitem__("count", counter["count"] + 1) or f"wrote:{value}",
        risk="high", read_only=False, capabilities=("external.write",),
        default_policy=policy, operation="write",
    )


class LoopStepRunner:
    def __init__(self, registry):
        self.registry = registry

    def __call__(self, _task, _step, context, _observer):
        client = ScriptedClient([
            response([tool_block("scheduled_write", {"value": "same"})], "tool_use"),
            response([text_block("done")]),
        ])
        result = run_loop(
            client, "offline", "system", [{"role": "user", "content": context}],
            self.registry,
        )
        return StepExecution(result.reply, "scheduled-task-run", tuple(result.tool_calls))

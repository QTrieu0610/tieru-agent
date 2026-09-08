"""Deterministic M15 durable-task state-machine contracts."""

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
from tieru.tasks.cli import run_task_cli
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import (
    PlanStep,
    PlanValidationError,
    StepExecution,
    StepStatus,
    TaskLimits,
    TaskStateError,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.planner import CallableTaskPlanner, parse_plan_output
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import LayeredTaskVerifier
from tieru.tools.registry import Tool, ToolRegistry

PLAN = [
    PlanStep("First", "Execute first bounded step.", "Observable evidence says first passed."),
    PlanStep("Second", "Execute second bounded step.", "Observable evidence says second passed."),
]


class FixedVerifier:
    def __init__(self, status=VerificationStatus.PASS):
        self.status = status

    def verify(self, _task, _step, _execution):
        return VerificationResult(self.status, f"verification {self.status.value}")


class CountingRunner:
    def __init__(self, *, tool_calls=True):
        self.calls: list[int] = []
        self.tool_calls = tool_calls

    def __call__(self, _task, step, _context, _observer):
        self.calls.append(step.position)
        calls = ({"tool": "fake", "output": "ok"},) if self.tool_calls else ()
        return StepExecution(f"result-{step.position}", f"run-{step.position}", calls)


def service(conn, runner=None, verifier=None, *, plan=None, limits=None, replay=None):
    bounds = limits or TaskLimits()
    store = TaskStore(conn, limits=bounds)
    planner = CallableTaskPlanner(lambda _goal: list(plan or PLAN))
    actual_runner = runner or CountingRunner()
    executor = TaskExecutor(
        store,
        actual_runner,
        verifier or FixedVerifier(),
        replay=replay,
        limits=bounds,
    )
    return TaskService(store, planner, executor), actual_runner


def test_create_persists_ordered_plan_across_restart(tmp_path):
    first = connect(tmp_path)
    tasks, _runner = service(first)
    task = tasks.create(goal="durable goal", source="test", session_id="session")
    steps = tasks.store.list_steps(task.task_id)
    assert task.status is TaskStatus.PLANNED
    assert [step.position for step in steps] == [1, 2]
    first.close()

    second = connect(tmp_path)
    reopened = TaskStore(second)
    assert reopened.get_task(task.task_id).goal == "durable goal"
    assert [step.title for step in reopened.list_steps(task.task_id)] == ["First", "Second"]
    second.close()


def test_task_creation_is_one_transaction(tmp_path):
    conn = connect(tmp_path)
    conn.executescript(
        """CREATE TRIGGER fail_second_task_step BEFORE INSERT ON task_steps
           WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'forced failure'); END;"""
    )
    store = TaskStore(conn)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_task("goal", PLAN, source="test")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_steps").fetchone()[0] == 0


def test_run_next_checkpoints_exactly_one_step(tmp_path):
    tasks, runner = service(connect(tmp_path))
    task = tasks.create(goal="goal")
    result = tasks.run(task.task_id)[0]
    steps = tasks.store.list_steps(task.task_id)

    assert result.executed is True
    assert runner.calls == [1]
    assert steps[0].status is StepStatus.SUCCEEDED
    assert steps[0].attempt_count == 1
    assert steps[0].result == "result-1"
    assert steps[0].verification_status is VerificationStatus.PASS
    assert steps[1].status is StepStatus.PENDING
    assert result.task.status is TaskStatus.RUNNING


def test_checkpoint_survives_restart_and_resume_runs_next_step(tmp_path):
    first = connect(tmp_path)
    initial, first_runner = service(first)
    task = initial.create(goal="goal")
    initial.run(task.task_id)
    assert first_runner.calls == [1]
    first.close()

    second = connect(tmp_path)
    resumed, second_runner = service(second)
    before = resumed.store.list_steps(task.task_id)
    assert before[0].status is StepStatus.SUCCEEDED
    resumed.resume(task.task_id)
    assert second_runner.calls == [2]
    after = resumed.store.list_steps(task.task_id)
    assert [step.status for step in after] == [StepStatus.SUCCEEDED, StepStatus.SUCCEEDED]
    assert resumed.store.get_task(task.task_id).status is TaskStatus.COMPLETED
    second.close()


def _write_tool(counter, *, policy="allow"):
    return Tool(
        name="write_once",
        description="Write once for a durable-task test.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        fn=lambda value: counter.__setitem__("count", counter["count"] + 1) or f"wrote:{value}",
        risk="medium",
        read_only=False,
        capabilities=("local_write",),
        default_policy=policy,
        operation="write",
    )


class LoopRunner:
    def __init__(self, registry, scripts):
        self.registry = registry
        self.scripts = list(scripts)
        self.calls = 0

    def __call__(self, _task, step, context, _observer):
        self.calls += 1
        client = ScriptedClient(self.scripts.pop(0))
        result = run_loop(
            client,
            "offline-model",
            "system",
            [{"role": "user", "content": context}],
            self.registry,
        )
        return StepExecution(result.reply, f"run-{step.position}", tuple(result.tool_calls))


def test_restart_does_not_duplicate_real_action_ledger_side_effect(tmp_path):
    counter = {"count": 0}
    first_conn = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(first_conn))
    registry.register(_write_tool(counter))
    loop_runner = LoopRunner(
        registry,
        [
            [
                response([tool_block("write_once", {"value": "x"})], "tool_use"),
                response([text_block("first complete")]),
            ]
        ],
    )
    first, _ = service(first_conn, runner=loop_runner)
    task = first.create(goal="write and then summarize")
    first.run(task.task_id)
    assert counter["count"] == 1
    assert first_conn.execute("SELECT status FROM tool_executions").fetchone()[0] == "completed"
    first_conn.close()

    second_conn = connect(tmp_path)
    second_runner = CountingRunner()
    second, _ = service(second_conn, runner=second_runner)
    second.resume(task.task_id)
    assert counter["count"] == 1
    assert second_runner.calls == [2]
    second_conn.close()


def test_concurrent_sqlite_step_claim_executes_body_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    body_count = {"count": 0}
    lock = threading.Lock()

    class BlockingRunner:
        def __call__(self, _task, step, _context, _observer):
            with lock:
                body_count["count"] += 1
            entered.set()
            assert release.wait(3)
            return StepExecution("done", f"run-{step.position}", ({"tool": "fake", "output": "ok"},))

    setup = connect(tmp_path)
    created, _ = service(setup, plan=[PLAN[0]])
    task = created.create(goal="race")
    setup.close()
    first, _ = service(connect(tmp_path, check_same_thread=False), runner=BlockingRunner())
    second, _ = service(connect(tmp_path, check_same_thread=False), runner=BlockingRunner())
    results = []
    one = threading.Thread(target=lambda: results.append(first.run(task.task_id)[0]))
    two = threading.Thread(target=lambda: results.append(second.run(task.task_id)[0]))
    one.start()
    assert entered.wait(3)
    two.start()
    two.join(3)
    assert not two.is_alive()
    release.set()
    one.join(3)
    assert not one.is_alive()
    assert body_count["count"] == 1
    assert {result.executed for result in results} == {False, True}
    assert "task_step_in_progress" in {result.code for result in results}


def test_real_trust_denial_blocks_task_without_side_effect(tmp_path):
    counter = {"count": 0}
    conn = connect(tmp_path)
    registry = ToolRegistry(execution_store=ExecutionStore(conn))
    registry.register(_write_tool(counter, policy="deny"))
    runner = LoopRunner(
        registry,
        [[response([tool_block("write_once", {"value": "x"})], "tool_use"), response([text_block("blocked")])]],
    )
    tasks, _ = service(conn, runner=runner, plan=[PLAN[0]], verifier=LayeredTaskVerifier())
    task = tasks.create(goal="denied write")
    result = tasks.run(task.task_id)[0]
    assert counter["count"] == 0
    assert result.step.status is StepStatus.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED
    assert conn.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 0


def test_uncertain_m14_action_blocks_without_rerun(tmp_path):
    counter = {"count": 0}
    conn = connect(tmp_path)
    ledger = ExecutionStore(conn)
    registry = ToolRegistry(execution_store=ledger)
    registry.register(_write_tool(counter))
    args = {"value": "x"}
    events = []
    registry.execute("write_once", args, notify=lambda kind, event: events.append((kind, event)))
    fingerprint = conn.execute("SELECT action_fingerprint FROM tool_executions").fetchone()[0]
    conn.execute(
        "UPDATE tool_executions SET status='uncertain', result=NULL WHERE action_fingerprint=?",
        (fingerprint,),
    )
    conn.commit()
    runner = LoopRunner(
        registry,
        [[response([tool_block("write_once", args)], "tool_use"), response([text_block("done")])]],
    )
    tasks, _ = service(conn, runner=runner, plan=[PLAN[0]], verifier=LayeredTaskVerifier())
    task = tasks.create(goal="resume ambiguous write")
    result = tasks.resume(task.task_id)[0]
    assert counter["count"] == 1
    assert result.step.status is StepStatus.BLOCKED
    assert result.task.status is TaskStatus.BLOCKED


def test_verification_failure_never_marks_success_or_complete(tmp_path):
    tasks, _runner = service(
        connect(tmp_path), plan=[PLAN[0]], verifier=FixedVerifier(VerificationStatus.FAIL)
    )
    task = tasks.create(goal="goal")
    result = tasks.run(task.task_id)[0]
    assert result.step.status is StepStatus.FAILED
    assert result.task.status is TaskStatus.FAILED
    assert result.step.verification_status is VerificationStatus.FAIL


def test_task_completes_only_after_every_step_verifies(tmp_path):
    tasks, _runner = service(connect(tmp_path))
    task = tasks.create(goal="goal")
    assert tasks.run(task.task_id)[0].task.status is TaskStatus.RUNNING
    assert tasks.run(task.task_id)[0].task.status is TaskStatus.COMPLETED


def test_cancellation_preserves_completed_steps_and_prevents_claims(tmp_path):
    tasks, runner = service(connect(tmp_path))
    task = tasks.create(goal="goal")
    tasks.run(task.task_id)
    cancelled = tasks.cancel(task.task_id)
    stopped = tasks.run(task.task_id)[0]
    assert cancelled.status is TaskStatus.CANCELLED
    assert stopped.executed is False
    assert runner.calls == [1]
    assert tasks.store.list_steps(task.task_id)[0].status is StepStatus.SUCCEEDED


def test_illegal_transition_is_rejected_without_database_change(tmp_path):
    store = TaskStore(connect(tmp_path))
    task = store.create_task("goal", [PLAN[0]], source="test")
    with pytest.raises(TaskStateError, match="illegal task transition"):
        store.transition_task(task.task_id, TaskStatus.COMPLETED)
    assert store.get_task(task.task_id).status is TaskStatus.PLANNED


def test_secret_safe_task_persistence_and_replay_normalization(tmp_path):
    secret = "super-secret-value"
    plan = [
        PlanStep(
            "Use api_key=" + secret,
            "instruction api_key=" + secret,
            "verify api_key=" + secret,
        )
    ]
    runner = CountingRunner()
    tasks, _ = service(connect(tmp_path), runner=runner, plan=plan)
    task = tasks.create(goal="goal api_key=" + secret)
    tasks.run(task.task_id)
    rows = tasks.store.conn.execute(
        "SELECT goal FROM tasks UNION ALL SELECT title FROM task_steps UNION ALL "
        "SELECT instruction FROM task_steps UNION ALL SELECT result FROM task_steps"
    ).fetchall()
    assert secret not in json.dumps([tuple(row) for row in rows])

    event = ReplayNormalizer().normalize(
        "task_step_succeeded",
        {
            "task_id": task.task_id,
            "step_id": "step",
            "result": "api_key=" + secret,
            "goal": "api_key=" + secret,
        },
    )
    assert event.category == "task"
    assert secret not in json.dumps(event.safe_payload)
    assert "result" not in event.safe_payload and "goal" not in event.safe_payload


def test_task_step_replay_run_linkage_is_inspectable(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    conn = connect(tmp_path)
    replay = ReplayService(conn, settings)
    run_id = replay.start_run(
        session_id="task-session",
        source="task",
        role="main",
        model="offline",
        provider="offline",
        user_input="bounded task context",
    ).run_id
    replay.complete_run(
        run_id,
        output="done",
        iterations=1,
        latency_ms=1,
        role="main",
        model="offline",
        provider="offline",
    )

    runner = lambda _task, _step, _context, _observer: StepExecution(
        "done", run_id, ({"tool": "fake", "output": "ok"},)
    )
    tasks, _ = service(conn, runner=runner, plan=[PLAN[0]], replay=replay)
    task = tasks.create(goal="trace me")
    tasks.run(task.task_id)
    step = tasks.store.list_steps(task.task_id)[0]
    events = replay.get_events(run_id)
    task_events = [event for event in events if event["category"] == "task"]
    assert step.execution_run_id == run_id
    assert {event["event_type"] for event in task_events} >= {
        "task_step_claimed",
        "task_step_started",
        "task_verification",
        "task_step_succeeded",
        "task_completed",
    }
    assert all(event["safe_payload"]["task_id"] == task.task_id for event in task_events)


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        '{"steps":[]}',
        json.dumps(
            {
                "steps": [
                    {"title": str(index), "instruction": "x", "verification": "x"}
                    for index in range(9)
                ]
            }
        ),
        json.dumps(
            {"steps": [{"title": "x" * 300, "instruction": "x", "verification": "x"}]}
        ),
    ],
)
def test_malformed_planner_output_is_rejected_before_persistence(tmp_path, output):
    store = TaskStore(connect(tmp_path))
    with pytest.raises(PlanValidationError):
        plan = parse_plan_output(output)
        store.create_task("goal", plan, source="test")
    assert store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_execution_invocation_is_bounded_to_one_checkpoint(tmp_path):
    limits = TaskLimits(max_execution_steps_per_invocation=1)
    tasks, runner = service(connect(tmp_path), limits=limits)
    task = tasks.create(goal="bounded")
    results = tasks.run(task.task_id, max_steps=999)
    assert len(results) == 1
    assert runner.calls == [1]
    assert tasks.store.list_steps(task.task_id)[1].status is StepStatus.PENDING


def test_cli_create_is_plan_only_and_show_is_inspectable_json(tmp_path, capsys):
    create_args = _parser().parse_args(["task", "create", "inspect then fix", "--json"])
    assert create_args.task_command == "create"
    assert not hasattr(create_args, "max_steps")

    settings = Settings(home=tmp_path)
    settings.ensure_home()
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create_task("inspect then fix", PLAN, source="test")
    conn.close()
    show_args = _parser().parse_args(["task", "show", task.task_id, "--json"])
    assert run_task_cli(show_args, settings) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["task"]["status"] == "planned"
    assert [step["position"] for step in shown["steps"]] == [1, 2]


def test_orphaned_running_step_blocks_on_resume_without_execution(tmp_path):
    conn = connect(tmp_path)
    tasks, runner = service(conn, plan=[PLAN[0]])
    task = tasks.create(goal="crash recovery")
    claim = tasks.store.claim_next_step(task.task_id)
    assert claim.step.status is StepStatus.RUNNING
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
    conn.execute(
        "UPDATE task_steps SET updated_at=? WHERE step_id=?", (stale, claim.step.step_id)
    )
    conn.commit()
    result = tasks.resume(task.task_id)[0]
    assert result.code == "orphaned_running_step"
    assert result.task.status is TaskStatus.BLOCKED
    assert result.step.status is StepStatus.BLOCKED
    assert runner.calls == []

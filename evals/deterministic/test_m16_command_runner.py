"""Deterministic M16 governed command-runner security contracts."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, response, text_block, tool_block
from tieru.config import Settings
from tieru.db import connect
from tieru.execution import ExecutionStore
from tieru.loop.agent import run_loop
from tieru.replay import ReplayRecorder, ReplayService, new_run_id
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.models import PlanStep, StepExecution, StepStatus, TaskStatus
from tieru.tasks.planner import CallableTaskPlanner
from tieru.tasks.service import TaskService
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import LayeredTaskVerifier, ModelResultVerifier
from tieru.tools.command import (
    DENIED_PRIVILEGE_WRAPPERS,
    DENIED_SHELLS,
    CommandPolicy,
    CommandRunner,
    make_tool,
)
from tieru.tools.registry import Tool, ToolRegistry


def command_registry(root: Path, *, allow=True, output_bytes=2048):
    root.mkdir(parents=True, exist_ok=True)
    conn = connect(root)
    runner = CommandRunner(
        CommandPolicy(
            root,
            max_stdout_bytes=output_bytes,
            max_stderr_bytes=output_bytes,
        )
    )
    registry = ToolRegistry(
        execution_store=ExecutionStore(conn, max_result_bytes=max(128, output_bytes)),
        trust_policy={"tools": {"run_command": "allow" if allow else "deny"}},
    )
    registry.register(make_tool(runner))
    return conn, registry, runner


def execute(registry, args, *, scope="scope-a", notify=None):
    return json.loads(
        registry.execute(
            "run_command",
            args,
            notify=notify,
            context={"execution_scope": scope, "user_request": "run the test command"},
        )
    )


def python_args(root: Path, source: str, *, timeout=30):
    return {
        "argv": [sys.executable, "-c", source],
        "cwd": str(root),
        "timeout_seconds": timeout,
    }


def test_basic_successful_command_returns_structured_result(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    result = execute(registry, python_args(tmp_path, "print('hello')"))
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["timed_out"] is False


def test_tool_is_argv_only_and_runtime_scope_is_not_model_controlled(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    tool = registry.get("run_command")
    assert tool.input_schema["required"] == ["argv"]
    assert tool.input_schema["properties"]["argv"]["type"] == "array"
    assert "command" not in tool.input_schema["properties"]
    assert "execution_scope" not in tool.input_schema["properties"]
    raw = execute(registry, {"argv": f"{sys.executable} -c print(1)"})
    bypass = execute(
        registry,
        {"argv": [sys.executable, "-c", "print(1)"], "execution_scope": "model"},
    )
    assert raw["error"]["code"] == "invalid_arguments"
    assert bypass["error"]["code"] == "invalid_arguments"


def test_subprocess_launch_explicitly_disables_shell(tmp_path, monkeypatch):
    _conn, registry, _runner = command_registry(tmp_path)
    observed = {}

    def launch(**kwargs):
        observed.update(kwargs)
        raise OSError("synthetic launch stop")

    monkeypatch.setattr("tieru.tools.command.subprocess.Popen", launch)
    result = execute(registry, python_args(tmp_path, "print(1)"))
    assert result["error"]["code"] == "command_launch_error"
    assert observed["shell"] is False
    assert observed["stdin"] is not None


@pytest.mark.parametrize("executable", sorted(DENIED_SHELLS))
def test_shell_interpreters_are_denied_before_launch(tmp_path, executable):
    _conn, registry, _runner = command_registry(tmp_path)
    result = execute(registry, {"argv": [executable, "-c", "echo unsafe"], "cwd": str(tmp_path)})
    assert result["error"]["code"] == "executable_denied"


def test_package_install_automation_is_denied_before_launch(tmp_path, monkeypatch):
    _conn, registry, _runner = command_registry(tmp_path)
    launched = False

    def launch(**_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("package installer must not launch")

    monkeypatch.setattr("tieru.tools.command.subprocess.Popen", launch)
    result = execute(
        registry,
        {"argv": [sys.executable, "-m", "pip", "install", "example"], "cwd": "."},
    )
    assert result["error"]["code"] == "executable_denied"
    assert launched is False


def test_workspace_traversal_escape_is_rejected(tmp_path):
    root = tmp_path / "root" / "project"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    _conn, registry, _runner = command_registry(root)
    escaped = root / ".." / ".." / "outside"
    result = execute(registry, python_args(escaped, "print('wrong')"))
    assert result["error"]["code"] == "cwd_outside_workspace"


def test_symlink_workspace_escape_is_rejected_when_supported(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    _conn, registry, _runner = command_registry(root)
    result = execute(registry, python_args(link, "print('wrong')"))
    assert result["error"]["code"] == "cwd_outside_workspace"


def test_nonexistent_cwd_is_safe_and_does_not_launch(tmp_path, monkeypatch):
    _conn, registry, _runner = command_registry(tmp_path)
    launched = {"count": 0}
    monkeypatch.setattr(
        "tieru.tools.command.subprocess.Popen",
        lambda **_kwargs: launched.__setitem__("count", launched["count"] + 1),
    )
    result = execute(registry, python_args(tmp_path / "missing", "print(1)"))
    assert result["error"]["code"] == "cwd_not_found"
    assert launched["count"] == 0


def test_parent_credentials_are_absent_from_child(tmp_path, monkeypatch):
    secret_a = "SUPER_SECRET_OPENAI_VALUE"
    secret_b = "SECOND_SECRET_ANTHROPIC_VALUE"
    monkeypatch.setenv("OPENAI_API_KEY", secret_a)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_b)
    _conn, registry, _runner = command_registry(tmp_path)
    source = (
        "import os; print(repr(os.getenv('OPENAI_API_KEY'))); "
        "print(repr(os.getenv('ANTHROPIC_API_KEY')))"
    )
    raw = registry.execute(
        "run_command",
        python_args(tmp_path, source),
        context={"execution_scope": "credential-test"},
    )
    result = json.loads(raw)
    assert result["ok"] is True
    assert "None" in result["stdout"]
    assert secret_a not in raw and secret_b not in raw


def test_minimum_safe_environment_still_launches_python(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    source = "import os,sys; print(bool(os.getenv('PATH')), sys.executable == '')"
    result = execute(registry, python_args(tmp_path, source))
    assert result["ok"] is True
    assert "True False" in result["stdout"]


def test_timeout_is_enforced_and_direct_process_is_stopped(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    started = time.perf_counter()
    result = execute(
        registry,
        python_args(tmp_path, "import time; time.sleep(5)", timeout=1),
    )
    elapsed = time.perf_counter() - started
    assert result["error"]["code"] == "command_timeout"
    assert result["timed_out"] is True
    assert elapsed < 4


def test_stdout_is_bounded(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path, output_bytes=128)
    result = execute(registry, python_args(tmp_path, "print('x' * 5000)"))
    assert result["truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= 128
    assert result["stdout"].endswith("[TRUNCATED]")


def test_stderr_is_bounded(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path, output_bytes=128)
    source = "import sys; sys.stderr.write('e' * 5000)"
    result = execute(registry, python_args(tmp_path, source))
    assert result["truncated"] is True
    assert len(result["stderr"].encode("utf-8")) <= 128
    assert result["stderr"].endswith("[TRUNCATED]")


def test_secret_output_is_redacted_from_result_ledger_and_replay(tmp_path):
    secret = "super-secret-value"
    settings = Settings(home=tmp_path, replay_max_tool_output_bytes=4096)
    settings.ensure_home()
    conn = connect(tmp_path)
    runner = CommandRunner(CommandPolicy(tmp_path, max_stdout_bytes=4096, max_stderr_bytes=4096))
    registry = ToolRegistry(
        execution_store=ExecutionStore(conn, max_result_bytes=4096),
        trust_policy={"tools": {"run_command": "allow"}},
    )
    registry.register(make_tool(runner))
    replay = ReplayService(conn, settings)
    recorder = ReplayRecorder(replay)
    run_id = new_run_id()
    recorder.start(
        run_id=run_id,
        session_id="m16",
        source="test",
        role="main",
        model="offline",
        provider="offline",
        user_input="print a synthetic secret",
    )
    raw = registry.execute(
        "run_command",
        python_args(tmp_path, f"print('api_key={secret}')"),
        notify=recorder.event,
        context={"execution_scope": run_id},
    )
    recorder.complete(
        output="done",
        iterations=1,
        latency_ms=1,
        role="main",
        model="offline",
        provider="offline",
    )
    ledger = "|".join(
        str(value) for value in conn.execute("SELECT * FROM tool_executions").fetchone()
    )
    replay_payload = json.dumps(replay.get_events(run_id))
    assert secret not in raw
    assert secret not in ledger
    assert secret not in replay_payload
    assert "REDACTED" in raw


def test_trust_denial_occurs_before_process_and_ledger(tmp_path, monkeypatch):
    conn, registry, _runner = command_registry(tmp_path, allow=False)
    launched = {"count": 0}

    def launch(**_kwargs):
        launched["count"] += 1
        raise AssertionError("process must not launch")

    monkeypatch.setattr("tieru.tools.command.subprocess.Popen", launch)
    result = execute(registry, python_args(tmp_path, "print('wrong')"))
    assert result["error"]["code"] == "tool_permission_denied"
    assert launched["count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 0


def test_explicit_allow_runs_and_same_scope_duplicate_starts_once(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    source = (
        "from pathlib import Path; p=Path('count.txt'); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x')"
    )
    args = python_args(tmp_path, source)
    first = execute(registry, args, scope="one-agent-action")
    duplicate = execute(registry, args, scope="one-agent-action")
    assert first["ok"] is True and duplicate["ok"] is True
    assert (tmp_path / "count.txt").read_text() == "x"


def test_later_runtime_owned_scope_can_intentionally_rerun(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    source = (
        "from pathlib import Path; p=Path('count.txt'); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x')"
    )
    args = python_args(tmp_path, source)
    assert execute(registry, args, scope="turn-one")["ok"] is True
    assert execute(registry, args, scope="turn-two")["ok"] is True
    assert (tmp_path / "count.txt").read_text() == "xx"


def test_run_loop_owns_scope_for_retry_and_later_intentional_turn(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    source = (
        "from pathlib import Path; p=Path('loop-count.txt'); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x')"
    )
    args = python_args(tmp_path, source)
    first = ScriptedClient(
        [
            response([tool_block("run_command", args)], "tool_use"),
            response([tool_block("run_command", args)], "tool_use"),
            response([text_block("done")]),
        ]
    )
    run_loop(
        first,
        "offline",
        "system",
        [{"role": "user", "content": "run the command once"}],
        registry,
    )
    assert (tmp_path / "loop-count.txt").read_text() == "x"

    later = ScriptedClient(
        [
            response([tool_block("run_command", args)], "tool_use"),
            response([text_block("done again")]),
        ]
    )
    run_loop(
        later,
        "offline",
        "system",
        [{"role": "user", "content": "intentionally run it again now"}],
        registry,
    )
    assert (tmp_path / "loop-count.txt").read_text() == "xx"


def test_workspace_local_python_script_is_supported(tmp_path):
    script = tmp_path / "generated.py"
    script.write_text("print('generated-script-ok')\n", encoding="utf-8")
    _conn, registry, _runner = command_registry(tmp_path)
    result = execute(
        registry,
        {"argv": [str(script)], "cwd": str(tmp_path), "timeout_seconds": 30},
    )
    assert result["ok"] is True
    assert "generated-script-ok" in result["stdout"]


def test_global_external_write_idempotency_is_unchanged(tmp_path):
    conn = connect(tmp_path)
    calls = {"count": 0}
    registry = ToolRegistry(
        execution_store=ExecutionStore(conn),
        trust_policy={"tools": {"external_write": "allow"}},
    )
    registry.register(
        Tool(
            "external_write",
            "Synthetic external write.",
            {
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"],
                "additionalProperties": False,
            },
            lambda body: calls.__setitem__("count", calls["count"] + 1) or body,
            risk="high",
            read_only=False,
            capabilities=("external.write",),
            default_policy="allow",
            operation="send",
            resource_type="message",
        )
    )
    for scope in ("turn-one", "turn-two"):
        assert (
            registry.execute(
                "external_write", {"body": "same"}, context={"execution_scope": scope}
            )
            == "same"
        )
    assert calls["count"] == 1


def test_durable_task_uses_loop_trust_ledger_command_and_replay(tmp_path):
    settings = Settings(home=tmp_path, replay_max_tool_output_bytes=4096)
    settings.ensure_home()
    conn, registry, _command_runner = command_registry(tmp_path, output_bytes=4096)
    replay = ReplayService(conn, settings)

    class LoopTaskRunner:
        def __call__(self, _task, step, context, observer):
            run_id = new_run_id()
            recorder = ReplayRecorder(replay)
            recorder.start(
                run_id=run_id,
                session_id=f"task:{step.task_id}",
                source="task",
                role="main",
                model="offline",
                provider="offline",
                user_input=context,
            )

            def notify(kind, event):
                recorder.event(kind, event)
                if observer:
                    observer(kind, event)

            client = ScriptedClient(
                [
                    response(
                        [
                            tool_block(
                                "run_command",
                                {
                                    "argv": [sys.executable, "-c", "print('task-command-ok')"],
                                    "cwd": str(tmp_path),
                                },
                            )
                        ],
                        "tool_use",
                    ),
                    response([text_block("validated")]),
                ]
            )
            result = run_loop(
                client,
                "offline",
                "system",
                [{"role": "user", "content": context}],
                registry,
                observer=notify,
            )
            recorder.complete(
                output=result.reply,
                iterations=result.iterations,
                latency_ms=1,
                role="main",
                model="offline",
                provider="offline",
            )
            return StepExecution(result.reply, run_id, tuple(result.tool_calls))

    store = TaskStore(conn)
    executor = TaskExecutor(
        store,
        LoopTaskRunner(),
        LayeredTaskVerifier(),
        replay=replay,
    )
    tasks = TaskService(
        store,
        CallableTaskPlanner(
            lambda _goal: [
                PlanStep("Run validation", "Run the validation command.", "Exit code must be zero.")
            ]
        ),
        executor,
    )
    task = tasks.create(goal="validate command integration")
    result = tasks.run(task.task_id)[0]
    assert result.task.status is TaskStatus.COMPLETED
    assert result.step.status is StepStatus.SUCCEEDED
    assert result.step.execution_run_id
    ledger = conn.execute("SELECT tool_name, status FROM tool_executions").fetchall()
    assert [(row["tool_name"], row["status"]) for row in ledger] == [
        ("run_command", "completed")
    ]
    events = replay.get_events(result.step.execution_run_id)
    kinds = {event["event_type"] for event in events}
    assert {"trust_decision", "tool_execution_claimed", "tool_completed"} <= kinds
    assert {"task_step_succeeded", "task_completed"} <= kinds


def test_model_verifier_is_tool_free_and_never_launches_command(monkeypatch):
    calls = []

    class Client:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"status":"pass","summary":"observable evidence passed"}',
                    )
                ]
            )

    router = SimpleNamespace(
        client=lambda _role: SimpleNamespace(messages=Client()),
        model=lambda _role: "offline-judge",
    )
    launched = {"count": 0}
    monkeypatch.setattr(
        "tieru.tools.command.subprocess.Popen",
        lambda **_kwargs: launched.__setitem__("count", launched["count"] + 1),
    )
    verifier = ModelResultVerifier(router)
    task = SimpleNamespace(goal="verify")
    step = SimpleNamespace(instruction="check", verification_instruction="evidence")
    result = verifier.verify(task, step, StepExecution("observable result"))
    assert result.status.value == "pass"
    assert calls[0]["tools"] == []
    assert launched["count"] == 0


def test_cancelled_task_never_launches_future_command_step(tmp_path):
    conn = connect(tmp_path)
    calls = {"count": 0}

    def task_runner(_task, _step, _context, _observer):
        calls["count"] += 1
        return StepExecution("wrong")

    store = TaskStore(conn)
    tasks = TaskService(
        store,
        CallableTaskPlanner(
            lambda _goal: [PlanStep("Command", "Run command.", "Exit zero.")]
        ),
        TaskExecutor(store, task_runner, LayeredTaskVerifier()),
    )
    task = tasks.create(goal="cancel before command")
    tasks.cancel(task.task_id)
    result = tasks.run(task.task_id)[0]
    assert result.executed is False
    assert result.task.status is TaskStatus.CANCELLED
    assert calls["count"] == 0


def test_missing_executable_returns_safe_error(tmp_path):
    _conn, registry, _runner = command_registry(tmp_path)
    result = execute(
        registry,
        {"argv": ["tieru-definitely-missing-executable-12345"], "cwd": str(tmp_path)},
    )
    assert result["error"]["code"] == "command_not_found"
    assert "Traceback" not in json.dumps(result)


def test_global_environment_is_not_mutated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "SUPER_SECRET_GLOBAL_VALUE")
    before = dict(os.environ)
    _conn, registry, _runner = command_registry(tmp_path)
    result = execute(registry, python_args(tmp_path, "print('ok')"))
    assert result["ok"] is True
    assert dict(os.environ) == before


@pytest.mark.parametrize("executable", sorted(DENIED_PRIVILEGE_WRAPPERS))
def test_privilege_wrappers_are_denied(tmp_path, executable):
    _conn, registry, _runner = command_registry(tmp_path)
    result = execute(registry, {"argv": [executable, sys.executable], "cwd": str(tmp_path)})
    assert result["error"]["code"] == "executable_denied"

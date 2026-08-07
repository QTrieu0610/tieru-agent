"""Deterministic M4 contracts: portability and dashboard approval security."""

from __future__ import annotations

import inspect
import threading
import time

from tieru.ops.approvals import DashboardApprovalService
from tieru.ops.dashboard_security import COOKIE, DashboardSecurity
from tieru.runtime.subprocesses import executable_argv, verify_argv
from tieru.tools.registry import PermissionRequest, Tool, ToolRegistry


def _request(argument_hash: str = "hash") -> PermissionRequest:
    return PermissionRequest(
        tool="browser_click",
        risk="medium",
        read_only=False,
        capabilities=("browser", "network.read"),
        args={"selector": "#save", "token": "[REDACTED]"},
        argument_hash=argument_hash,
        reason="tool has side effects or elevated access",
    )


def test_python_commands_are_portable_and_verify_never_needs_a_shell(tmp_path):
    script = tmp_path / "extensionless"
    script.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    assert executable_argv(script)[-1] == str(script)
    assert executable_argv(script)[0] == verify_argv(["{python}", "-c", "pass"])[0]
    assert verify_argv("python3 -c \"print('ok')\"")[1:] == ["-c", "print('ok')"]

    from tieru.ops import coding_eval

    assert "shell=True" not in inspect.getsource(coding_eval)


def test_dashboard_approval_is_bound_single_use_and_exact():
    service = DashboardApprovalService(ttl_seconds=1)
    result = []

    def ask():
        with service.session("chat-1"):
            result.append(service.confirm(_request()))

    thread = threading.Thread(target=ask)
    thread.start()
    for _ in range(50):
        pending = service.list_pending()
        if pending:
            break
        time.sleep(0.01)
    item = pending[0]
    wrong = {**item, "decision": "approve", "argument_hash": "changed"}
    assert not service.resolve(wrong)
    thread.join(2)
    assert result == [False]

    result.clear()
    thread = threading.Thread(target=ask)
    thread.start()
    for _ in range(50):
        pending = service.list_pending()
        if pending:
            break
        time.sleep(0.01)
    exact = {**pending[0], "decision": "approve"}
    assert service.resolve(exact)
    assert not service.resolve(exact)
    thread.join(2)
    assert result == [True]


def test_dashboard_approval_timeout_and_disconnect_deny():
    timed = DashboardApprovalService(ttl_seconds=0.02)
    with timed.session("chat-timeout"):
        assert timed.confirm(_request()) is False

    service = DashboardApprovalService(ttl_seconds=1)
    result = []

    def ask():
        with service.session("chat-disconnect"):
            result.append(service.confirm(_request()))

    thread = threading.Thread(target=ask)
    thread.start()
    for _ in range(50):
        if service.list_pending():
            break
        time.sleep(0.01)
    service.deny_session("chat-disconnect")
    thread.join(2)
    assert result == [False]


def test_registry_argument_fingerprint_changes_without_exposing_secret():
    seen = []
    registry = ToolRegistry(approval_handler=lambda request: seen.append(request) or False)
    registry.register(Tool(
        "write", "write", {"type": "object"}, lambda **kwargs: "bad",
        risk="high", read_only=False, capabilities=("filesystem.write",),
        default_policy="confirm", sensitive_args=("token",),
    ))
    registry.execute("write", {"token": "first"})
    registry.execute("write", {"token": "second"})
    assert seen[0].args == seen[1].args == {"token": "[REDACTED]"}
    assert seen[0].argument_hash != seen[1].argument_hash
    assert "first" not in seen[0].argument_hash


def test_dashboard_mutations_require_same_origin_session_and_csrf():
    security = DashboardSecurity()
    sid, csrf, _ = security.issue("")
    valid = {
        "Cookie": f"{COOKIE}={sid}",
        "X-Tieru-CSRF": csrf,
        "Host": "127.0.0.1:7777",
        "Origin": "http://127.0.0.1:7777",
    }
    assert security.verify(valid)
    assert not security.verify({**valid, "Origin": "https://evil.example"})
    assert not security.verify({**valid, "X-Tieru-CSRF": "wrong"})
    assert not security.verify({key: value for key, value in valid.items() if key != "Origin"})


def test_approval_ui_uses_redacted_json_as_text_not_html():
    from pathlib import Path

    util = (Path(__file__).resolve().parents[2] / "tieru/ops/static/js/util.js").read_text(
        encoding="utf-8"
    )
    assert 'document.getElementById("approval-args").textContent' in util
    assert "Approve once" in util and 'resolveApproval("deny")' in util
    assert "secureFetch" in util and "X-Tieru-CSRF" in util


def test_dashboard_startup_messages_are_safe_for_redirected_windows_output():
    from tieru.ops import dashboard

    source = inspect.getsource(dashboard.main)
    source.encode("cp1252")

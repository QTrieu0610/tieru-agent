"""Deterministic M7 contracts for Tieru Trust Kernel."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tieru.config import Settings, load_settings
from tieru.db import connect
from tieru.memory import Memory
from tieru.tools.browser import RestrictedBrowser
from tieru.tools.browser import make_tools as make_browser_tools
from tieru.tools.memory_admin import make_manage_memory_tool
from tieru.tools.registry import Tool, ToolRegistry
from tieru.trust import (
    ActionRequest,
    Capability,
    RiskClassifier,
    RiskLevel,
    TrustKernel,
    action_fingerprint,
    canonical_capability,
)


def action(
    capability: Capability,
    *,
    operation: str = "read",
    target: str = "",
    destructive: bool = False,
    scope: str = "",
) -> ActionRequest:
    return ActionRequest(
        tool_name="test_tool",
        capabilities=(capability,),
        operation=operation,
        target=target,
        scope=scope,
        destructive=destructive,
        read_only=operation == "read",
    )


def authorize(kernel: TrustKernel, request: ActionRequest, default: str = "deny"):
    return kernel.authorize(request, default_policy=default)


def test_risk_classifier_represents_all_four_levels():
    classifier = RiskClassifier()
    assert classifier.classify(action(Capability.LOCAL_READ))[0] is RiskLevel.LOW
    assert classifier.classify(
        action(Capability.LOCAL_WRITE, operation="write")
    )[0] is RiskLevel.MEDIUM
    assert classifier.classify(
        action(Capability.EXTERNAL_WRITE, operation="send")
    )[0] is RiskLevel.HIGH
    assert classifier.classify(
        action(
            Capability.DESTRUCTIVE,
            operation="delete",
            target="*",
            destructive=True,
            scope="broad",
        )
    )[0] is RiskLevel.CRITICAL

    browser_read = ActionRequest(
        "browser_read",
        (Capability.BROWSER_AUTOMATION,),
        "extract",
        read_only=True,
        browser_action=True,
    )
    browser_click = ActionRequest(
        "browser_click",
        (Capability.BROWSER_AUTOMATION,),
        "click",
        read_only=False,
        browser_action=True,
    )
    assert classifier.classify(browser_read)[0] is RiskLevel.LOW
    assert classifier.classify(browser_click)[0] is RiskLevel.HIGH


def test_unknown_capability_is_unclassified_and_denied():
    request = ActionRequest("mystery", (), "execute")
    decision = authorize(TrustKernel(), request, "allow")
    assert not decision.allowed
    assert decision.risk is RiskLevel.CRITICAL
    assert "unclassified_action" in decision.reason_codes


def test_critical_action_cannot_inherit_a_broad_default_allow():
    request = action(
        Capability.DESTRUCTIVE,
        operation="delete",
        target="*",
        destructive=True,
        scope="broad",
    )
    decision = authorize(TrustKernel({"default": "allow"}), request, "allow")
    assert not decision.allowed
    assert "critical_requires_explicit_allow" in decision.reason_codes


def test_explicit_deny_wins_over_scoped_allow(tmp_path):
    kernel = TrustKernel(
        {"capabilities": {"local_write": {"mode": "allow", "paths": [str(tmp_path)]}}},
        {"capabilities": {"local_write": "deny"}},
    )
    decision = authorize(
        kernel,
        action(Capability.LOCAL_WRITE, operation="write", target=str(tmp_path / "x")),
    )
    assert not decision.allowed
    assert decision.reason_codes[-1] == "explicit_deny"


def test_scoped_path_allow_and_normalized_traversal_denial(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    kernel = TrustKernel(
        {"capabilities": {"local_write": {"mode": "allow", "paths": [str(root)]}}},
        context={"base_path": str(root)},
    )
    inside = action(Capability.LOCAL_WRITE, operation="write", target="reports/a.md")
    escape = action(Capability.LOCAL_WRITE, operation="write", target="../outside.md")
    assert authorize(kernel, inside).allowed
    denied = authorize(kernel, escape)
    assert not denied.allowed and "target_out_of_scope" in denied.reason_codes


def test_default_deny_and_legacy_permission_mapping_are_fail_safe():
    request = action(Capability.LOCAL_WRITE, operation="write", target="note")
    assert not authorize(TrustKernel({"default": "deny"}), request).allowed
    compatible = TrustKernel(
        legacy_policy={"capabilities": {"filesystem.write": "allow"}}
    )
    assert compatible.authorize(
        request, default_policy="deny", legacy_capabilities=("filesystem.write",)
    ).allowed
    denied = TrustKernel(
        legacy_policy={"capabilities": {"filesystem.write": "deny"}}
    )
    assert not denied.authorize(
        request, default_policy="allow", legacy_capabilities=("filesystem.write",)
    ).allowed


def test_new_trust_config_loads_without_invalidating_existing_configs(tmp_path):
    config = tmp_path / "tieru.yaml"
    config.write_text(
        """version: 1
trust:
  default: deny
  capabilities:
    local_read: allow
tool_permissions:
  capabilities:
    process.execute: deny
""",
        encoding="utf-8",
    )
    settings = load_settings({"config_path": config, "home": tmp_path / "home"})
    assert settings.trust_policy["default"] == "deny"
    assert settings.tool_permissions["capabilities"]["process.execute"] == "deny"

    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("version: 1\ntool_permissions: {}\n", encoding="utf-8")
    assert load_settings({"config_path": legacy, "home": tmp_path / "old"}).trust_policy == {}


def test_all_shipped_builtin_capabilities_have_canonical_mapping():
    declared = {
        "calendar.write",
        "calendar.read",
        "filesystem.write",
        "memory.read",
        "memory.write",
        "persona.write",
        "instructions.write",
        "message.draft",
        "network.read",
        "network.write",
        "web.untrusted",
        "process.read",
        "process.execute",
        "browser",
        "mail.read",
        "reminder.write",
        "notes.write",
    }
    assert all(canonical_capability(item) is not None for item in declared)


def test_approval_allow_once_is_not_a_permanent_rule():
    approvals = []
    kernel = TrustKernel(
        {"capabilities": {"local_write": "confirm"}},
        approval_handler=lambda request: approvals.append(request) or True,
    )
    request = action(Capability.LOCAL_WRITE, operation="write", target="note")
    assert authorize(kernel, request).allowed
    assert authorize(kernel, request).allowed
    assert len(approvals) == 2
    assert all(item.argument_hash == approvals[0].argument_hash for item in approvals)


def test_deny_once_missing_handler_and_approval_error_fail_closed():
    calls = []
    request = action(Capability.LOCAL_WRITE, operation="write", target="note")
    denied = TrustKernel(
        {"capabilities": {"local_write": "confirm"}},
        approval_handler=lambda item: calls.append(item) or False,
    )
    assert not authorize(denied, request).allowed
    assert not authorize(denied, request).allowed
    assert len(calls) == 1

    unavailable = authorize(
        TrustKernel({"capabilities": {"local_write": "confirm"}}), request
    )
    assert not unavailable.allowed and "approval_unavailable" in unavailable.reason_codes

    def broken(_request):
        raise RuntimeError("approval transport failed")

    error = authorize(
        TrustKernel(
            {"capabilities": {"local_write": "confirm"}}, approval_handler=broken
        ),
        request,
    )
    assert not error.allowed and "approval_error" in error.reason_codes


def test_action_fingerprints_are_stable_distinct_and_secret_safe():
    first = action(Capability.EXTERNAL_WRITE, operation="send", target="repo-a")
    same = action(Capability.EXTERNAL_WRITE, operation="send", target="repo-a")
    other = action(Capability.EXTERNAL_WRITE, operation="send", target="repo-b")
    secret_a = action(
        Capability.EXTERNAL_WRITE, operation="authenticate", target="token=abc123456789"
    )
    secret_b = action(
        Capability.EXTERNAL_WRITE, operation="authenticate", target="token=xyz987654321"
    )
    assert action_fingerprint(first) == action_fingerprint(same)
    assert action_fingerprint(first) != action_fingerprint(other)
    assert action_fingerprint(secret_a) == action_fingerprint(secret_b)
    assert "abc123456789" not in action_fingerprint(secret_a)


def test_registry_executes_allowed_and_never_executes_denied_or_unclassified():
    calls = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            "read",
            "read",
            {"type": "object"},
            lambda: calls.append("read") or "ok",
            risk="low",
            read_only=True,
            capabilities=("local_read",),
            default_policy="allow",
        )
    )
    registry.register(
        Tool(
            "write",
            "write",
            {"type": "object"},
            lambda: calls.append("write") or "bad",
            risk="medium",
            read_only=False,
            capabilities=("local_write",),
            default_policy="deny",
        )
    )
    registry.register(
        Tool(
            "unknown",
            "unknown",
            {"type": "object"},
            lambda: calls.append("unknown") or "bad",
            risk="high",
            read_only=False,
            capabilities=("not.classified",),
            default_policy="allow",
        )
    )
    assert registry.execute("read", {}) == "ok"
    assert json.loads(registry.execute("write", {}))["ok"] is False
    unknown = json.loads(registry.execute("unknown", {}))
    assert unknown["error"]["risk"] == "CRITICAL"
    assert calls == ["read"]


def test_trust_events_are_structured_and_never_include_sensitive_args():
    events = []
    registry = ToolRegistry(approval_handler=lambda _request: False)
    registry.register(
        Tool(
            "send",
            "send",
            {"type": "object"},
            lambda **_kw: "bad",
            risk="high",
            read_only=False,
            capabilities=("external_write",),
            default_policy="confirm",
            sensitive_args=("token", "body"),
            operation="send",
            target_arg="recipient",
        )
    )
    registry.execute(
        "send",
        {"recipient": "alex", "token": "token=abc123456789", "body": "private"},
        notify=lambda kind, event: events.append((kind, event)),
    )
    trust = [(kind, event) for kind, event in events if kind.startswith("trust_")]
    assert [kind for kind, _event in trust] == [
        "trust_request",
        "trust_approval",
        "trust_decision",
    ]
    serialized = json.dumps(trust)
    assert "abc123456789" not in serialized and "private" not in serialized


def test_classified_and_unclassified_mcp_tools_use_same_kernel():
    calls = []
    registry = ToolRegistry(trust_policy={"capabilities": {"network_read": "allow"}})
    registry.register(
        Tool(
            "docs_search",
            "MCP read",
            {"type": "object"},
            lambda: calls.append("safe") or "result",
            risk="low",
            read_only=True,
            capabilities=("network_read",),
            default_policy="deny",
            resource_type="mcp",
            fixed_target="docs/search",
        )
    )
    registry.register(
        Tool(
            "mystery_action",
            "MCP unknown",
            {"type": "object"},
            lambda: calls.append("unsafe") or "bad",
            risk="critical",
            read_only=False,
            capabilities=("unclassified",),
            default_policy="deny",
            resource_type="mcp",
        )
    )
    assert registry.execute("docs_search", {}) == "result"
    assert json.loads(registry.execute("mystery_action", {}))["ok"] is False
    assert calls == ["safe"]


def test_narrow_explicit_mcp_policy_beats_default_but_not_explicit_deny():
    tool = Tool(
        "docs_read",
        "configured MCP read",
        {"type": "object"},
        lambda: "ok",
        risk="low",
        read_only=True,
        capabilities=("network_read",),
        default_policy="allow",
        explicit_policy=True,
        resource_type="mcp",
    )
    registry = ToolRegistry(trust_policy={"default": "deny"})
    registry.register(tool)
    assert registry.execute("docs_read", {}) == "ok"

    denied = ToolRegistry(
        trust_policy={
            "default": "deny",
            "capabilities": {"network_read": "deny"},
        }
    )
    denied.register(tool)
    assert json.loads(denied.execute("docs_read", {}))["ok"] is False


def test_browser_read_and_side_effect_policies_preserve_host_restrictions(tmp_path):
    browser = RestrictedBrowser(
        Settings(home=tmp_path, browser_allowed_domains=("example.com",))
    )
    tools = {item.name: item for item in make_browser_tools(browser)}
    registry = ToolRegistry(trust_context={"browser_domains": ["example.com"]})
    registry.register(tools["browser_open"])
    registry.register(tools["browser_click"])
    assert registry.gate.decide(
        tools["browser_open"], {"url": "https://example.com/page"}
    )[0]
    assert not registry.gate.decide(tools["browser_click"], {"selector": "#buy"})[0]
    assert not registry.gate.decide(
        tools["browser_open"], {"url": "https://outside.example/page"}
    )[0]
    with pytest.raises(Exception, match="not in browser_allowed_domains"):
        browser.validate_url("https://outside.example/page")


def test_process_execution_requires_command_scope_and_never_parses_a_shell():
    calls = []
    registry = ToolRegistry(
        trust_policy={
            "capabilities": {
                "process_execution": {"mode": "allow", "commands": ["git"]}
            }
        }
    )
    registry.register(
        Tool(
            "process",
            "bounded process",
            {"type": "object"},
            lambda command: calls.append(command) or "ok",
            risk="high",
            read_only=False,
            capabilities=("process_execution",),
            default_policy="deny",
            target_arg="command",
            resource_type="process",
        )
    )
    assert registry.execute("process", {"command": "git status"}) == "ok"
    assert json.loads(registry.execute("process", {"command": "powershell -c whoami"}))[
        "ok"
    ] is False
    assert calls == ["git status"]


def test_memory_graph_reads_and_writes_receive_action_specific_trust(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    memory = Memory(connect(tmp_path), settings, client=None)
    relation, _ = memory.graph.remember_relation(
        subject="Tieru", predicate="USES", object="SQLite"
    )
    registry = ToolRegistry(
        trust_policy={"capabilities": {"local_read": "allow", "local_write": "confirm"}}
    )
    registry.register(make_manage_memory_tool(memory))
    inspected = registry.execute(
        "manage_memory", {"action": "inspect_relation", "id": relation.id}
    )
    assert json.loads(inspected)["relation"]["id"] == relation.id
    denied = json.loads(
        registry.execute(
            "manage_memory",
            {
                "action": "remember_relation",
                "subject": "Tieru",
                "predicate": "USES",
                "object": "FTS5",
            },
        )
    )
    assert denied["error"]["policy"] == "confirm"

    events = []
    registry.execute(
        "manage_memory",
        {"action": "delete", "kind": "fact", "id": 999},
        notify=lambda kind, event: events.append((kind, event)),
    )
    decision = next(event for kind, event in events if kind == "trust_decision")
    assert decision["risk"] == "HIGH" and "destructive" in decision["reason_codes"]


def test_classifier_and_policy_errors_fail_closed():
    class BrokenClassifier:
        def classify(self, _action):
            raise RuntimeError("broken")

    request = action(Capability.LOCAL_READ)
    classifier = TrustKernel(classifier=BrokenClassifier())
    assert "classifier_error" in authorize(classifier, request, "allow").reason_codes

    invalid_policy = TrustKernel({"default": "perhaps"})
    decision = authorize(invalid_policy, request)
    assert not decision.allowed and "policy_error" in decision.reason_codes


def test_decision_explanation_is_deterministic_and_secret_free():
    request = action(
        Capability.EXTERNAL_WRITE,
        operation="push",
        target="token=abc123456789",
    )
    decision = authorize(TrustKernel({"default": "deny"}), request)
    assert decision.explanation.startswith("DENIED — HIGH risk")
    assert "trust default" in decision.explanation
    assert "abc123456789" not in decision.explanation


def test_dashboard_exposes_policy_not_secret_arguments():
    dashboard = Path("tieru/ops/dashboard.py").read_text(encoding="utf-8")
    view = Path("tieru/ops/static/js/views.js").read_text(encoding="utf-8")
    assert '"trust": trust' in dashboard
    assert "canonical_capabilities" in view and "Recent safe decisions" in view
    assert "This is operational inspection, not Replay" in view

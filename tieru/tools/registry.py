"""Central tool registry and deny-by-default permission boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from tieru.memory.personal import redact_secrets

PolicyAction = Literal["allow", "confirm", "deny"]
ApprovalHandler = Callable[["PermissionRequest"], bool]
_POLICIES = {"allow", "confirm", "deny"}
_SENSITIVE_KEY = re.compile(
    r"(^|_)(api_?key|authorization|cookie|credential|password|secret|token)($|_)",
    re.IGNORECASE,
)
_ARGUMENT_HASH_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class PermissionRequest:
    tool: str
    risk: str
    read_only: bool
    capabilities: tuple[str, ...]
    args: dict[str, Any]
    argument_hash: str
    reason: str


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., str]
    wants_notify: bool = False
    risk: str | None = None
    read_only: bool | None = None
    capabilities: tuple[str, ...] | None = None
    default_policy: PolicyAction | None = None
    action_field: str = ""
    action_policies: dict[str, PolicyAction] = field(default_factory=dict)
    action_read_only: dict[str, bool] = field(default_factory=dict)
    action_capabilities: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sensitive_args: tuple[str, ...] = ()

    @property
    def classified(self) -> bool:
        return (
            self.risk in {"low", "medium", "high"}
            and self.read_only is not None
            and self.capabilities is not None
            and self.default_policy in _POLICIES
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


class ToolPermissionGate:
    """Resolve policy once, immediately before any registered function runs."""

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.policy = dict(policy or {})
        self.approval_handler = approval_handler

    @staticmethod
    def _policy(value: Any, label: str) -> PolicyAction:
        value = str(value).lower().strip()
        if value not in _POLICIES:
            raise ValueError(f"{label} must be allow, confirm, or deny")
        return value  # type: ignore[return-value]

    def decide(self, tool: Tool, args: dict[str, Any]) -> tuple[bool, PolicyAction, str]:
        if not tool.classified:
            return False, "deny", "tool is unclassified"

        action: PolicyAction = tool.default_policy  # type: ignore[assignment]
        if tool.action_field:
            requested = str(args.get(tool.action_field, "")).lower()
            if requested in tool.action_policies:
                action = tool.action_policies[requested]
        else:
            requested = ""
        read_only = tool.action_read_only.get(requested, bool(tool.read_only))
        capabilities = tool.action_capabilities.get(requested, tool.capabilities or ())

        defaults = self.policy.get("defaults") or {}
        if isinstance(defaults, dict):
            key = "read_only" if read_only else "side_effect"
            if key in defaults:
                action = self._policy(defaults[key], f"tool_permissions.defaults.{key}")

        capability_rules = self.policy.get("capabilities") or {}
        if isinstance(capability_rules, dict):
            resolved = [
                self._policy(capability_rules[name], f"tool_permissions.capabilities.{name}")
                for name in capabilities
                if name in capability_rules
            ]
            if "deny" in resolved:
                action = "deny"
            elif "confirm" in resolved:
                action = "confirm"
            elif resolved:
                action = "allow"

        tool_rules = self.policy.get("tools") or {}
        if isinstance(tool_rules, dict) and tool.name in tool_rules:
            action = self._policy(tool_rules[tool.name], f"tool_permissions.tools.{tool.name}")

        if action == "allow":
            return True, action, "allowed by policy"
        if action == "deny":
            return False, action, "denied by policy"
        if self.approval_handler is None:
            return False, action, "confirmation required but no interactive handler is available"
        request = PermissionRequest(
            tool=tool.name,
            risk=tool.risk or "high",
            read_only=read_only,
            capabilities=capabilities,
            args={
                key: ("[REDACTED]" if key in tool.sensitive_args else _redact(value, key))
                for key, value in args.items()
            },
            argument_hash=hmac.new(
                _ARGUMENT_HASH_KEY,
                json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
            reason="tool has side effects or elevated access",
        )
        try:
            approved = bool(self.approval_handler(request))
        except Exception:
            approved = False
        return approved, action, "approved by user" if approved else "confirmation declined"


class ToolRegistry:
    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self.gate = ToolPermissionGate(policy, approval_handler)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_api() for tool in self._tools.values()]

    def redact_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        sensitive = set(tool.sensitive_args if tool is not None else ())
        return {
            key: ("[REDACTED]" if key in sensitive else _redact(value, key))
            for key, value in args.items()
        }

    @staticmethod
    def _denial(name: str, policy: str, reason: str) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "tool_permission_denied",
                    "tool": name,
                    "policy": policy,
                    "reason": reason,
                    "retryable": False,
                },
            },
            sort_keys=True,
        )

    def execute(self, name: str, args: dict[str, Any], notify=None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            if notify:
                notify("permission", {"tool": name, "decision": "deny",
                                      "reason": "unknown tool", "args": _redact(args)})
            return self._denial(name, "deny", "unknown tool")
        allowed, policy, reason = self.gate.decide(tool, args)
        if notify:
            notify(
                "permission",
                {
                    "tool": name,
                    "decision": "allow" if allowed else "deny",
                    "policy": policy,
                    "reason": reason,
                    "args": self.redact_args(name, args),
                },
            )
        if not allowed:
            return self._denial(name, policy, reason)
        try:
            if tool.wants_notify:
                output = tool.fn(**args, _notify=notify or (lambda kind, ev: None))
            else:
                output = tool.fn(**args)
            return redact_secrets(str(output))
        except Exception as exc:
            message = redact_secrets(str(exc))
            return f"Error running {name}: {type(exc).__name__}: {message}"

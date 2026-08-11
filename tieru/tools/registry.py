"""Tool registration and the single Tieru Trust Kernel execution boundary."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from tieru.memory.personal import redact_secrets
from tieru.trust import (
    ActionRequest,
    ApprovalRequest,
    Capability,
    TrustDecision,
    TrustKernel,
    canonical_capability,
)

PolicyAction = Literal["allow", "confirm", "deny"]
PermissionRequest = ApprovalRequest
ApprovalHandler = Callable[[PermissionRequest], bool]
_POLICIES = {"allow", "confirm", "deny"}
_SENSITIVE_KEY = re.compile(
    r"(^|_)(api_?key|authorization|cookie|credential|password|secret|token)($|_)",
    re.IGNORECASE,
)
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
    operation: str = ""
    target_arg: str = ""
    fixed_target: str = ""
    scope: str = ""
    resource_type: str = ""
    reversible: bool | None = None
    destructive_actions: tuple[str, ...] = ("delete", "remove", "wipe", "destroy")
    explicit_policy: bool = False

    @property
    def classified(self) -> bool:
        return (
            self.risk in {"low", "medium", "high", "critical"}
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


def _output_event(text: str, limit_bytes: int = 2048) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    preview = (
        text
        if len(encoded) <= limit_bytes
        else encoded[:limit_bytes].decode("utf-8", errors="ignore")
    )
    return {
        "output_preview": preview,
        "output_size": len(encoded),
        "truncated": len(encoded) > limit_bytes,
    }


def _action_metadata(tool: Tool, args: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    requested = str(args.get(tool.action_field, "")).strip().lower() if tool.action_field else ""
    read_only = tool.action_read_only.get(requested, bool(tool.read_only))
    capabilities = tool.action_capabilities.get(requested, tool.capabilities or ())
    return read_only, tuple(capabilities)


def _build_action(tool: Tool, args: dict[str, Any], safe_args: dict[str, Any]) -> ActionRequest:
    read_only, legacy_capabilities = _action_metadata(tool, args)
    canonical: list[Capability] = []
    has_unknown = False
    for item in legacy_capabilities:
        resolved = canonical_capability(item)
        if resolved is None:
            has_unknown = True
        elif resolved not in canonical:
            canonical.append(resolved)
    if has_unknown:
        canonical = []
    requested = str(args.get(tool.action_field, "")).strip().lower() if tool.action_field else ""
    operation = tool.operation or requested or ("read" if read_only else "write")
    target = tool.fixed_target
    target_arg = tool.target_arg
    if not target_arg:
        for candidate in ("path", "cwd", "url", "repo", "to", "recipient", "id", "selector"):
            if candidate in safe_args and safe_args[candidate] not in (None, ""):
                target_arg = candidate
                break
    if target_arg:
        target = str(safe_args.get(target_arg, ""))
    destructive = operation in tool.destructive_actions or Capability.DESTRUCTIVE in canonical
    resource_type = tool.resource_type
    if not resource_type:
        if any(item.startswith("filesystem.") for item in legacy_capabilities):
            resource_type = "filesystem"
        elif Capability.BROWSER_AUTOMATION in canonical:
            resource_type = "browser"
        elif any(item.startswith("memory.") for item in legacy_capabilities):
            resource_type = "memory"
        elif Capability.PROCESS_EXECUTION in canonical:
            resource_type = "process"
    reversible = tool.reversible if tool.reversible is not None else not destructive
    return ActionRequest(
        tool_name=tool.name,
        capabilities=tuple(canonical),
        operation=operation,
        target=target,
        scope=tool.scope,
        resource_type=resource_type,
        local=bool({Capability.LOCAL_READ, Capability.LOCAL_WRITE} & set(canonical)),
        network=bool({Capability.NETWORK_READ, Capability.EXTERNAL_WRITE} & set(canonical)),
        external_write=Capability.EXTERNAL_WRITE in canonical,
        destructive=destructive,
        process_execution=Capability.PROCESS_EXECUTION in canonical,
        browser_action=Capability.BROWSER_AUTOMATION in canonical,
        reversible=reversible,
        read_only=read_only,
        metadata={
            "declared_risk": tool.risk or "critical",
            "explicit_tool_policy": tool.explicit_policy,
            "credential_related": any(
                key in args and _SENSITIVE_KEY.search(key) for key in tool.sensitive_args
            ),
        },
    )


def _legacy_result(decision: TrustDecision) -> tuple[bool, PolicyAction, str]:
    policy: PolicyAction = (
        "confirm" if decision.approval_required else "allow" if decision.allowed else "deny"
    )
    if decision.allowed:
        reason = "approved by user" if decision.approval_required else "allowed by policy"
    elif "unclassified_action" in decision.reason_codes:
        reason = "tool is unclassified"
    elif "approval_unavailable" in decision.reason_codes:
        reason = "confirmation required but no interactive handler is available"
    elif "approval_denied" in decision.reason_codes:
        reason = "confirmation declined"
    elif "approval_error" in decision.reason_codes:
        reason = "confirmation handler failed"
    else:
        reason = "denied by policy"
    return decision.allowed, policy, reason


class ToolPermissionGate:
    """Compatibility wrapper; TrustKernel owns the actual decision."""

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.policy = dict(policy or {})
        self.approval_handler = approval_handler
        self.kernel = TrustKernel(legacy_policy=self.policy, approval_handler=approval_handler)

    def decide(self, tool: Tool, args: dict[str, Any]) -> tuple[bool, PolicyAction, str]:
        if not tool.classified:
            return False, "deny", "tool is unclassified"
        safe_args = {
            key: ("[REDACTED]" if key in tool.sensitive_args else _redact(value, key))
            for key, value in args.items()
        }
        action = _build_action(tool, args, safe_args)
        requested = str(args.get(tool.action_field, "")).lower() if tool.action_field else ""
        default = tool.action_policies.get(requested, tool.default_policy or "deny")
        decision = self.kernel.authorize(
            action,
            default_policy=default,
            legacy_capabilities=_action_metadata(tool, args)[1],
            approval_args=safe_args,
        )
        return _legacy_result(decision)


class ToolRegistry:
    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
        *,
        trust_policy: dict[str, Any] | None = None,
        trust_context: dict[str, Any] | None = None,
        kernel: TrustKernel | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self.kernel = kernel or TrustKernel(
            trust_policy,
            legacy_policy=policy,
            approval_handler=approval_handler,
            context=trust_context,
        )
        self.gate = ToolPermissionGate(policy, approval_handler)
        self.gate.kernel = self.kernel

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
    def _denial(
        name: str, policy: str, reason: str, decision: TrustDecision | None = None
    ) -> str:
        detail = {
            "code": "tool_permission_denied",
            "tool": name,
            "policy": policy,
            "reason": reason,
            "retryable": False,
        }
        if decision is not None:
            detail.update(
                {
                    "risk": decision.risk.value,
                    "reason_codes": list(decision.reason_codes),
                    "explanation": decision.explanation,
                    "action_fingerprint": decision.action_fingerprint,
                }
            )
        return json.dumps(
            {"ok": False, "error": detail},
            sort_keys=True,
        )

    def execute(self, name: str, args: dict[str, Any], notify=None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            action = ActionRequest(name, (), "unknown")
            decision = self.kernel.authorize(action, default_policy="deny", observer=notify)
            if notify:
                notify("permission", {"tool": name, "decision": "deny",
                                      "reason": "unknown tool", "args": _redact(args)})
                notify("tool_denied", {"tool": name, "args": _redact(args),
                                       "reason": "unknown tool"})
            return self._denial(name, "deny", "unknown tool", decision)
        safe_args = self.redact_args(name, args)
        if not tool.classified:
            action = ActionRequest(name, (), "unclassified")
            decision = self.kernel.authorize(action, default_policy="deny", observer=notify)
        else:
            action = _build_action(tool, args, safe_args)
            requested = str(args.get(tool.action_field, "")).lower() if tool.action_field else ""
            default = tool.action_policies.get(requested, tool.default_policy or "deny")
            decision = self.kernel.authorize(
                action,
                default_policy=default,
                legacy_capabilities=_action_metadata(tool, args)[1],
                approval_args=safe_args,
                observer=notify,
            )
        allowed, policy, reason = _legacy_result(decision)
        if notify:
            notify(
                "permission",
                {
                    "tool": name,
                    "decision": "allow" if allowed else "deny",
                    "policy": policy,
                    "reason": reason,
                    "args": safe_args,
                },
            )
        if not allowed:
            if notify:
                notify("tool_denied", {"tool": name, "args": safe_args,
                                       "reason": reason, "risk": decision.risk.value,
                                       "reason_codes": list(decision.reason_codes)})
            return self._denial(name, policy, reason, decision)
        started = time.perf_counter()
        if notify:
            notify("tool_started", {"tool": name, "args": safe_args})
        try:
            if tool.wants_notify:
                output = tool.fn(**args, _notify=notify or (lambda kind, ev: None))
            else:
                output = tool.fn(**args)
            safe_output = redact_secrets(str(output))
            if notify:
                low = safe_output.lower()
                failed = (low.startswith(("error", "timed out")) or " failed:" in low
                          or " timed out" in low)
                notify("tool_failed" if failed else "tool_completed",
                       {"tool": name, **_output_event(safe_output),
                        "error_code": "tool_reported_failure" if failed else "",
                        "duration_ms": int((time.perf_counter() - started) * 1000)})
            return safe_output
        except Exception as exc:
            message = redact_secrets(str(exc))
            output = f"Error running {name}: {type(exc).__name__}: {message}"
            if notify:
                notify("tool_failed", {"tool": name, **_output_event(output),
                                        "error_code": type(exc).__name__,
                                        "duration_ms": int((time.perf_counter() - started) * 1000)})
            return output

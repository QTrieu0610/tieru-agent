"""Tool registration and the single Tieru Trust Kernel execution boundary."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from tieru.execution import ClaimOutcome, ExecutionClaim, ExecutionStore
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
IntentCheck = Callable[[str, dict[str, Any]], tuple[str, str] | None]
ArgumentPreparer = Callable[[dict[str, Any]], dict[str, Any]]
_POLICIES = {"allow", "confirm", "deny"}
_SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
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
    timeout_seconds: float | None = None
    intent_check: IntentCheck | None = None
    idempotency_guard: bool | None = None
    idempotency_scope: Literal["global", "run"] = "global"
    prepare_args: ArgumentPreparer | None = None
    capability: str = ""
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    always_visible: bool = False

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

    def validate_arguments(self, args: Any) -> list[str]:
        """Return deterministic JSON-schema-subset validation errors."""
        return _schema_errors(self.input_schema, args)


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, str):
        return redact_secrets(value)
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


def _build_action(
    tool: Tool,
    args: dict[str, Any],
    safe_args: dict[str, Any],
    *,
    execution_scope: str = "",
) -> ActionRequest:
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
    argument_identity: Any = args
    if tool.idempotency_scope == "run":
        argument_identity = {
            "arguments": args,
            # Runtime-only context supplied by run_loop, never by a tool schema.
            "runtime_scope": execution_scope or "unscoped",
        }
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
            # Ephemeral only: Trust's fingerprint normalizer removes credential
            # fields and secret-shaped values before hashing. Raw arguments are
            # never persisted by the ledger.
            "argument_identity": argument_identity,
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


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _schema_errors(schema: Any, value: Any, path: str = "arguments") -> list[str]:
    """Validate the small JSON Schema subset used by provider tool APIs."""
    if not isinstance(schema, dict):
        return [f"{path} schema must be an object"]
    declared = schema.get("type")
    expected = declared if isinstance(declared, list) else [declared]
    expected = [item for item in expected if isinstance(item, str)]
    if expected and not any(_matches_type(value, item) for item in expected):
        return [f"{path} must be {' or '.join(expected)}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} must be one of the declared enum values"]

    errors: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return [f"{path} schema properties must be an object"]
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return [f"{path} schema required must be a string array"]
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name} is not allowed")
        for name, item in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                errors.extend(_schema_errors(child_schema, item, f"{path}.{name}"))
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(_schema_errors(schema["items"], item, f"{path}[{index}]"))

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path} is shorter than minLength")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path} is longer than maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{path} is below minimum")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{path} is above maximum")
    return errors


def _schema_definition_errors(schema: Any, path: str = "input_schema") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path} must be an object"]
    declared = schema.get("type")
    if declared is None:
        declared_types = []
    else:
        declared_types = declared if isinstance(declared, list) else [declared]
    if any(item not in _SCHEMA_TYPES for item in declared_types):
        return [f"{path}.type is invalid"]
    if "enum" in schema and not isinstance(schema["enum"], list):
        return [f"{path}.enum must be an array"]
    errors: list[str] = []
    properties = schema.get("properties", {})
    if "object" in declared_types:
        if not isinstance(properties, dict):
            return [f"{path}.properties must be an object"]
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            return [f"{path}.required must be a string array"]
        for name, child in properties.items():
            errors.extend(_schema_definition_errors(child, f"{path}.properties.{name}"))
    if "items" in schema:
        errors.extend(_schema_definition_errors(schema["items"], f"{path}.items"))
    return errors


def _validate_tool(tool: Tool) -> None:
    if not isinstance(tool.name, str) or not _TOOL_NAME.fullmatch(tool.name):
        raise ValueError(
            "tool name must start with a letter and contain at most 64 letters, "
            "numbers, underscores, or hyphens"
        )
    if not isinstance(tool.description, str) or not tool.description.strip():
        raise ValueError(f"tool '{tool.name}' must have a description")
    if not callable(tool.fn):
        raise TypeError(f"tool '{tool.name}' fn must be callable")
    if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
        raise ValueError(f"tool '{tool.name}' input_schema must describe an object")
    schema_errors = _schema_definition_errors(tool.input_schema)
    if schema_errors:
        raise ValueError(f"tool '{tool.name}' has an invalid input_schema: {schema_errors[0]}")
    if tool.timeout_seconds is not None and tool.timeout_seconds <= 0:
        raise ValueError(f"tool '{tool.name}' timeout_seconds must be positive")
    if tool.intent_check is not None and not callable(tool.intent_check):
        raise TypeError(f"tool '{tool.name}' intent_check must be callable")
    if tool.idempotency_guard is not None and not isinstance(tool.idempotency_guard, bool):
        raise TypeError(f"tool '{tool.name}' idempotency_guard must be a boolean")
    if tool.idempotency_scope not in {"global", "run"}:
        raise ValueError(f"tool '{tool.name}' idempotency_scope must be global or run")
    if tool.prepare_args is not None and not callable(tool.prepare_args):
        raise TypeError(f"tool '{tool.name}' prepare_args must be callable")


class ToolRegistry:
    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
        *,
        trust_policy: dict[str, Any] | None = None,
        trust_context: dict[str, Any] | None = None,
        kernel: TrustKernel | None = None,
        execution_store: ExecutionStore | None = None,
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
        self.execution_store = execution_store
        self.executor = ToolExecutor(self)

    def register(self, tool: Tool) -> None:
        _validate_tool(tool)
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def keys(self):
        return self._tools.keys()

    def values(self):
        return self._tools.values()

    def items(self):
        return self._tools.items()

    def __iter__(self):
        return iter(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def schemas(self, names: Sequence[str] | None = None) -> list[dict[str, Any]]:
        if names is None:
            return [tool.to_api() for tool in self._tools.values()]
        allowed = set(names)
        return [tool.to_api() for tool in self._tools.values() if tool.name in allowed]

    def redact_args(self, name: str, args: Any) -> Any:
        tool = self._tools.get(name)
        sensitive = set(tool.sensitive_args if tool is not None else ())
        if not isinstance(args, dict):
            return _redact(args)
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

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        notify=None,
        *,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Compatibility entry point; ToolExecutor owns execution semantics."""
        return self.executor.execute(name, args, notify=notify, context=context)


class ToolExecutor:
    """Validate, authorize, execute, and observe one structured tool call."""

    def __init__(
        self, registry: ToolRegistry, *, timeout_seconds: float | None = None
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.registry = registry
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _error(name: str, code: str, message: str, *, retryable: bool = False) -> str:
        safe_message = redact_secrets(message)
        encoded = safe_message.encode("utf-8")
        if len(encoded) > 1024:
            safe_message = encoded[:1024].decode("utf-8", errors="ignore") + "…"
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "tool": name,
                    "message": safe_message,
                    "retryable": retryable,
                },
            },
            sort_keys=True,
        )

    @staticmethod
    def _notify_failure(notify, name: str, output: str, code: str, started: float) -> None:
        if notify:
            notify(
                "tool_failed",
                {
                    "tool": name,
                    **_output_event(output),
                    "error_code": code,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )

    @staticmethod
    def _notify_execution(notify, kind: str, tool: str, fingerprint: str, **fields) -> None:
        """Ledger observability is best-effort and never changes execution state."""
        if not notify:
            return
        try:
            notify(
                kind,
                {
                    "tool": tool,
                    "action_fingerprint": fingerprint,
                    **fields,
                },
            )
        except Exception:
            pass

    @staticmethod
    def _requires_idempotency(tool: Tool, action: ActionRequest) -> bool:
        if tool.idempotency_guard is not None:
            return tool.idempotency_guard
        return not action.read_only

    @staticmethod
    def _reported_failure(output: str) -> bool:
        low = output.lower()
        if (
            low.startswith(("error", "timed out"))
            or " failed:" in low
            or " timed out" in low
        ):
            return True
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and (
            payload.get("ok") is False or isinstance(payload.get("error"), dict)
        )

    def _claim_execution(
        self,
        tool: Tool,
        action: ActionRequest,
        decision: TrustDecision,
        notify,
    ) -> tuple[ExecutionStore | None, str, str | None]:
        store = self.registry.execution_store
        if store is None or not self._requires_idempotency(tool, action):
            return None, "", None
        fingerprint = decision.action_fingerprint
        try:
            claim = store.claim(fingerprint, tool.name)
        except Exception as exc:
            output = self._error(
                tool.name,
                "tool_execution_ledger_error",
                f"Could not safely claim this action: {type(exc).__name__}",
                retryable=True,
            )
            self._notify_execution(
                notify,
                "tool_execution_failed",
                tool.name,
                fingerprint,
                status="ledger_error",
                error_code="tool_execution_ledger_error",
            )
            return store, fingerprint, output
        return store, fingerprint, self._claimed_result(tool, claim, notify)

    def _claimed_result(self, tool: Tool, claim: ExecutionClaim, notify) -> str | None:
        fingerprint = claim.record.action_fingerprint
        if claim.outcome is ClaimOutcome.CLAIMED:
            if claim.manual_retry_permit_id:
                self._notify_execution(
                    notify,
                    "manual_retry_consumed",
                    tool.name,
                    fingerprint,
                    permit_id=claim.manual_retry_permit_id,
                    recovery_id=claim.manual_retry_recovery_id,
                    status=claim.record.status.value,
                    attempt_count=claim.record.attempt_count,
                )
            self._notify_execution(
                notify,
                "tool_execution_claimed",
                tool.name,
                fingerprint,
                status=claim.record.status.value,
                attempt_count=claim.record.attempt_count,
            )
            return None
        if claim.outcome is ClaimOutcome.COMPLETED:
            self._notify_execution(
                notify,
                "tool_idempotency_hit",
                tool.name,
                fingerprint,
                status=claim.record.status.value,
                result_truncated=claim.record.result_truncated,
            )
            return claim.record.result or ""
        if claim.outcome is ClaimOutcome.IN_PROGRESS:
            self._notify_execution(
                notify,
                "tool_execution_in_progress",
                tool.name,
                fingerprint,
                status=claim.record.status.value,
            )
            return self._error(
                tool.name,
                "tool_execution_in_progress",
                "An identical side-effecting action is already executing.",
                retryable=True,
            )
        if claim.outcome is ClaimOutcome.FAILED:
            self._notify_execution(
                notify,
                "tool_idempotency_hit",
                tool.name,
                fingerprint,
                status=claim.record.status.value,
                retryable=claim.record.retryable,
            )
            return claim.record.result or self._error(
                tool.name,
                "tool_previous_execution_failed",
                "An identical action previously failed and will not be retried automatically.",
            )
        self._notify_execution(
            notify,
            "tool_execution_uncertain",
            tool.name,
            fingerprint,
            status=claim.record.status.value,
        )
        return self._error(
            tool.name,
            "tool_execution_uncertain",
            "An earlier identical action may have completed; manual verification is required.",
        )

    def _finish_execution(
        self,
        store: ExecutionStore | None,
        fingerprint: str,
        tool: Tool,
        output: str,
        *,
        status: str,
        retryable: bool = False,
        notify,
    ) -> str | None:
        if store is None:
            return None
        try:
            if status == "completed":
                record = store.complete(fingerprint, output)
                kind = "tool_execution_completed"
            elif status == "failed":
                record = store.fail(fingerprint, output, retryable=retryable)
                kind = "tool_execution_failed"
            else:
                record = store.mark_uncertain(fingerprint, output)
                kind = "tool_execution_uncertain"
        except Exception as exc:
            self._notify_execution(
                notify,
                "tool_execution_uncertain",
                tool.name,
                fingerprint,
                status="ledger_error",
                error_code=type(exc).__name__,
            )
            return self._error(
                tool.name,
                "tool_execution_uncertain",
                "The action ran, but its execution record could not be finalized; "
                "manual verification is required.",
            )
        self._notify_execution(
            notify,
            kind,
            tool.name,
            fingerprint,
            status=record.status.value,
            attempt_count=record.attempt_count,
            result_truncated=record.result_truncated,
        )
        return None

    @staticmethod
    def _call(tool: Tool, args: dict[str, Any], notify):
        if tool.wants_notify:
            return tool.fn(**args, _notify=notify or (lambda kind, ev: None))
        return tool.fn(**args)

    def _invoke(self, tool: Tool, args: dict[str, Any], notify):
        timeout = tool.timeout_seconds or self.timeout_seconds
        if timeout is None:
            return True, self._call(tool, args, notify)
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result.put((True, self._call(tool, args, notify)))
            except Exception as exc:
                result.put((False, exc))

        worker = threading.Thread(target=run, name=f"tieru-tool-{tool.name}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return False, timeout
        succeeded, value = result.get_nowait()
        if not succeeded:
            raise value
        return True, value

    def execute(
        self,
        name: str,
        args: Any,
        notify=None,
        *,
        context: dict[str, Any] | None = None,
    ) -> str:
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            started = time.perf_counter()
            safe_name = str(name)
            output = self._error(
                safe_name,
                "invalid_tool_name",
                "tool name has an invalid format",
            )
            self._notify_failure(notify, safe_name, output, "invalid_tool_name", started)
            return output
        tool = self.registry.get(name)
        if tool is None:
            action = ActionRequest(name, (), "unknown")
            decision = self.registry.kernel.authorize(
                action, default_policy="deny", observer=notify
            )
            if notify:
                notify("permission", {"tool": name, "decision": "deny",
                                      "reason": "unknown tool", "args": _redact(args)})
                notify("tool_denied", {"tool": name, "args": _redact(args),
                                       "reason": "unknown tool"})
            return self.registry._denial(name, "deny", "unknown tool", decision)
        safe_args = self.registry.redact_args(name, args)
        validation_errors = tool.validate_arguments(args)
        if validation_errors:
            started = time.perf_counter()
            output = self._error(
                name,
                "invalid_arguments",
                "; ".join(validation_errors),
                retryable=True,
            )
            self._notify_failure(notify, name, output, "invalid_arguments", started)
            return output
        typed_args: dict[str, Any] = args
        if tool.intent_check is not None:
            user_request = str((context or {}).get("user_request") or "")
            intent_error = tool.intent_check(user_request, typed_args)
            if intent_error is not None:
                started = time.perf_counter()
                code, message = intent_error
                output = self._error(name, code, message)
                self._notify_failure(notify, name, output, code, started)
                return output
        if tool.prepare_args is not None:
            try:
                typed_args = tool.prepare_args(dict(typed_args))
            except Exception as exc:
                started = time.perf_counter()
                code = str(getattr(exc, "code", "command_invalid"))
                message = str(getattr(exc, "safe_message", "Invalid command request."))
                output = self._error(name, code, message, retryable=True)
                self._notify_failure(notify, name, output, code, started)
                return output
            safe_args = self.registry.redact_args(name, typed_args)
        if not tool.classified:
            action = ActionRequest(name, (), "unclassified")
            decision = self.registry.kernel.authorize(
                action, default_policy="deny", observer=notify
            )
        else:
            action = _build_action(
                tool,
                typed_args,
                safe_args,
                execution_scope=str((context or {}).get("execution_scope") or ""),
            )
            requested = (
                str(typed_args.get(tool.action_field, "")).lower()
                if tool.action_field
                else ""
            )
            default = tool.action_policies.get(requested, tool.default_policy or "deny")
            decision = self.registry.kernel.authorize(
                action,
                default_policy=default,
                legacy_capabilities=_action_metadata(tool, typed_args)[1],
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
            return self.registry._denial(name, policy, reason, decision)
        store, fingerprint, cached = self._claim_execution(
            tool, action, decision, notify
        )
        if cached is not None:
            return cached
        started = time.perf_counter()
        if notify:
            notify("tool_started", {"tool": name, "args": safe_args})
        try:
            completed, output = self._invoke(tool, typed_args, notify)
            if not completed:
                timeout = output
                output = self._error(
                    name,
                    "tool_timeout",
                    f"tool timed out after {timeout:g} seconds",
                    retryable=False,
                )
                ledger_error = self._finish_execution(
                    store,
                    fingerprint,
                    tool,
                    output,
                    status="uncertain",
                    notify=notify,
                )
                self._notify_failure(notify, name, output, "tool_timeout", started)
                return ledger_error or output
            safe_output = redact_secrets(str(output))
            failed = self._reported_failure(safe_output)
            ledger_error = self._finish_execution(
                store,
                fingerprint,
                tool,
                safe_output,
                status="failed" if failed else "completed",
                notify=notify,
            )
            if ledger_error is not None:
                self._notify_failure(
                    notify, name, ledger_error, "tool_execution_uncertain", started
                )
                return ledger_error
            if notify:
                notify("tool_failed" if failed else "tool_completed",
                       {"tool": name, **_output_event(safe_output),
                        "error_code": "tool_reported_failure" if failed else "",
                        "duration_ms": int((time.perf_counter() - started) * 1000)})
            return safe_output
        except Exception as exc:
            message = redact_secrets(str(exc))
            retryable = isinstance(
                exc,
                (FileExistsError, FileNotFoundError, IsADirectoryError, NotADirectoryError),
            )
            code = "tool_precondition_failed" if retryable else "tool_execution_error"
            output = self._error(
                name,
                code,
                f"Error running {name}: {type(exc).__name__}: {message}",
                retryable=retryable,
            )
            ledger_error = self._finish_execution(
                store,
                fingerprint,
                tool,
                output,
                status="failed",
                retryable=retryable,
                notify=notify,
            )
            self._notify_failure(notify, name, output, code, started)
            return ledger_error or output

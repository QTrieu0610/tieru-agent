"""Readable policy normalization, scope matching, and deterministic precedence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from tieru.trust.models import ActionRequest, Capability, RiskLevel

PolicyMode = Literal["allow", "confirm", "deny"]
_MODES = {"allow", "confirm", "deny"}


@dataclass(frozen=True)
class PolicyResult:
    mode: PolicyMode
    matched_policy: str
    reason_codes: tuple[str, ...]


def canonical_capability(value: str) -> Capability | None:
    value = str(value or "").strip().lower().replace("-", "_")
    try:
        return Capability(value)
    except ValueError:
        pass
    dotted = value.replace("_", ".")
    if dotted in {"browser", "browser.automation"}:
        return Capability.BROWSER_AUTOMATION
    if dotted in {"process.execute", "process.execution", "process.read"}:
        return Capability.PROCESS_EXECUTION
    if dotted in {"network.read", "web.untrusted"}:
        return Capability.NETWORK_READ
    if dotted in {"network.write", "mcp.external"}:
        return Capability.EXTERNAL_WRITE
    if dotted == "filesystem.read":
        return Capability.LOCAL_READ
    if dotted in {
        "filesystem.write",
        "memory.write",
        "persona.write",
        "instructions.write",
        "message.draft",
        "notes.write",
    }:
        return Capability.LOCAL_WRITE
    if dotted in {"calendar.write", "reminder.write", "mail.write", "message.send"}:
        return Capability.EXTERNAL_WRITE
    if dotted in {"calendar.read", "mail.read"}:
        return Capability.NETWORK_READ
    if dotted == "destructive" or dotted.endswith(".delete"):
        return Capability.DESTRUCTIVE
    if dotted.endswith(".read"):
        return Capability.LOCAL_READ
    if dotted.endswith(".write"):
        return Capability.LOCAL_WRITE
    return None


class PolicyEvaluator:
    """New Trust policy plus fail-safe normalization of legacy tool_permissions."""

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        legacy_policy: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.policy = dict(policy or {})
        self.legacy = dict(legacy_policy or {})
        self.context = dict(context or {})

    def validate(self) -> None:
        """Validate configured policy structure without evaluating an action."""
        for label, policy, validate_capabilities in (
            ("trust", self.policy, True),
            ("tool_permissions", self.legacy, False),
        ):
            if not isinstance(policy, dict):
                raise TypeError(f"{label} policy must be a mapping")
            if "default" in policy:
                self._mode(policy["default"], f"{label}.default")
            defaults = policy.get("defaults") or {}
            if not isinstance(defaults, dict):
                raise TypeError(f"{label}.defaults must be a mapping")
            for name, value in defaults.items():
                if validate_capabilities and canonical_capability(str(name)) is None:
                    raise ValueError(f"{label}.defaults contains unknown capability {name!r}")
                self._mode(value, f"{label}.defaults.{name}")
            for section in ("tools", "capabilities"):
                rules = policy.get(section) or {}
                if not isinstance(rules, dict):
                    raise TypeError(f"{label}.{section} must be a mapping")
                for name, rule in rules.items():
                    if (
                        section == "capabilities"
                        and validate_capabilities
                        and canonical_capability(str(name)) is None
                    ):
                        raise ValueError(
                            f"{label}.capabilities contains unknown capability {name!r}"
                        )
                    self._mode(rule, f"{label}.{section}.{name}")
                    if not isinstance(rule, dict):
                        continue
                    for scope_name in (
                        "paths", "hosts", "repositories", "commands", "recipients", "targets"
                    ):
                        if scope_name not in rule:
                            continue
                        values = rule[scope_name]
                        if not isinstance(values, (list, tuple)) or not values:
                            raise ValueError(
                                f"{label}.{section}.{name}.{scope_name} must be a non-empty list"
                            )
                        if any(not isinstance(item, str) or not item.strip() for item in values):
                            raise ValueError(
                                f"{label}.{section}.{name}.{scope_name} contains an invalid value"
                            )

    @staticmethod
    def _mode(value: Any, label: str) -> PolicyMode:
        if isinstance(value, dict):
            value = value.get("mode")
        mode = str(value or "").strip().lower()
        if mode not in _MODES:
            raise ValueError(f"{label} must be allow, confirm, or deny")
        return mode  # type: ignore[return-value]

    def _scope_values(self, rule: dict[str, Any]) -> dict[str, tuple[str, ...]]:
        keys = ("paths", "hosts", "repositories", "commands", "recipients", "targets")
        return {
            key: tuple(str(item) for item in value)
            for key in keys
            if isinstance((value := rule.get(key)), (list, tuple)) and value
        }

    def _path_allowed(self, target: str, roots: tuple[str, ...]) -> bool:
        if not target:
            return False
        base = Path(self.context.get("base_path") or Path.cwd()).resolve()
        candidate = Path(target).expanduser()
        candidate = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        for item in roots:
            aliases = self.context.get("path_aliases") or {}
            root = Path(aliases.get(item, item)).expanduser()
            root = (base / root).resolve() if not root.is_absolute() else root.resolve()
            if candidate == root or root in candidate.parents:
                return True
        return False

    @staticmethod
    def _host_allowed(target: str, hosts: tuple[str, ...]) -> bool:
        parsed = urlparse(target if "://" in target else f"//{target}")
        host = (parsed.hostname or "").lower().rstrip(".")
        return bool(host) and any(
            host == allowed.lower().rstrip(".")
            or host.endswith("." + allowed.lower().rstrip("."))
            for allowed in hosts
        )

    def _scope_matches(self, action: ActionRequest, rule: dict[str, Any]) -> bool:
        scopes = self._scope_values(rule)
        if not scopes:
            return True
        checks = []
        if "paths" in scopes:
            checks.append(self._path_allowed(action.target, scopes["paths"]))
        if "hosts" in scopes:
            checks.append(self._host_allowed(action.target, scopes["hosts"]))
        if "repositories" in scopes:
            checks.append(action.target in scopes["repositories"])
        if "commands" in scopes:
            checks.append(action.target.split(maxsplit=1)[0] in scopes["commands"])
        if "recipients" in scopes:
            checks.append(action.target in scopes["recipients"])
        if "targets" in scopes:
            checks.append(action.target in scopes["targets"])
        return bool(checks) and all(checks)

    def evaluate(
        self,
        action: ActionRequest,
        risk: RiskLevel,
        *,
        default_policy: str,
        legacy_capabilities: tuple[str, ...],
    ) -> PolicyResult:
        browser_domains = tuple(str(item) for item in self.context.get("browser_domains", ()))
        if (
            Capability.BROWSER_AUTOMATION in action.capabilities
            and action.operation == "navigate"
            and not self._host_allowed(action.target, browser_domains)
        ):
            return PolicyResult(
                "deny", "browser_allowed_domains", ("browser_target_out_of_scope",)
            )
        candidates: list[tuple[PolicyMode, str, bool]] = []
        if action.metadata.get("explicit_tool_policy"):
            candidates.append(
                (
                    self._mode(default_policy, "tool.explicit_policy"),
                    "tool.explicit_policy",
                    False,
                )
            )
        new_tools = self.policy.get("tools") or {}
        legacy_tools = self.legacy.get("tools") or {}
        for label, rules in (("trust.tools", new_tools), ("tool_permissions.tools", legacy_tools)):
            if isinstance(rules, dict) and action.tool_name in rules:
                raw = rules[action.tool_name]
                scoped = isinstance(raw, dict) and bool(self._scope_values(raw))
                if scoped and not self._scope_matches(action, raw):
                    return PolicyResult(
                        "deny", f"{label}.{action.tool_name}", ("target_out_of_scope",)
                    )
                candidates.append(
                    (
                        self._mode(raw, f"{label}.{action.tool_name}"),
                        f"{label}.{action.tool_name}",
                        scoped,
                    )
                )

        capability_keys = [item.value for item in action.capabilities] + list(legacy_capabilities)
        for label, rules in (
            ("trust.capabilities", self.policy.get("capabilities") or {}),
            ("tool_permissions.capabilities", self.legacy.get("capabilities") or {}),
        ):
            if not isinstance(rules, dict):
                continue
            for key in capability_keys:
                if key not in rules:
                    continue
                raw = rules[key]
                scoped = isinstance(raw, dict) and bool(self._scope_values(raw))
                mode = self._mode(raw, f"{label}.{key}")
                if scoped and not self._scope_matches(action, raw):
                    return PolicyResult("deny", f"{label}.{key}", ("target_out_of_scope",))
                candidates.append((mode, f"{label}.{key}", scoped))

        # A deny at any explicit tool/capability level always wins.
        for mode, label, _scoped in candidates:
            if mode == "deny":
                return PolicyResult("deny", label, ("explicit_deny",))
        # A matching scoped allow is stronger than approval/defaults.
        for mode, label, scoped in candidates:
            if mode == "allow" and scoped:
                return PolicyResult("allow", label, ("scoped_allow",))
        # Explicit unscoped tool/capability settings retain M3 compatibility.
        for wanted in ("allow", "confirm"):
            for mode, label, _scoped in candidates:
                if mode == wanted:
                    code = "explicit_allow" if mode == "allow" else "approval_by_policy"
                    return PolicyResult(mode, label, (code,))

        defaults = self.policy.get("defaults") or {}
        if isinstance(defaults, dict):
            for key in (item.value for item in action.capabilities):
                if key in defaults:
                    return PolicyResult(
                        self._mode(defaults[key], f"trust.defaults.{key}"),
                        f"trust.defaults.{key}",
                        ("capability_default",),
                    )
        legacy_defaults = self.legacy.get("defaults") or {}
        legacy_key = "read_only" if action.read_only else "side_effect"
        if isinstance(legacy_defaults, dict) and legacy_key in legacy_defaults:
            return PolicyResult(
                self._mode(legacy_defaults[legacy_key], f"tool_permissions.defaults.{legacy_key}"),
                f"tool_permissions.defaults.{legacy_key}",
                ("legacy_default",),
            )
        if "default" in self.policy:
            return PolicyResult(
                self._mode(self.policy["default"], "trust.default"),
                "trust.default",
                ("trust_default",),
            )
        if default_policy:
            return PolicyResult(
                self._mode(default_policy, "tool.default_policy"),
                "tool.default_policy",
                ("tool_default",),
            )
        return PolicyResult("deny", "deny_by_default", ("deny_by_default",))

    def public_summary(self) -> dict[str, Any]:
        return {
            "precedence": [
                "explicit deny",
                "matching scoped allow",
                "approval",
                "capability/default policy",
                "deny by default",
            ],
            "default": self.policy.get("default", "tool-declared compatible default"),
            "capabilities": self.policy.get("capabilities") or {},
            "canonical_capabilities": [item.value for item in Capability],
            "risk_baseline": {
                "local_read": "LOW",
                "local_write": "MEDIUM",
                "network_read": "LOW",
                "external_write": "HIGH",
                "process_execution": "HIGH",
                "browser_automation": "LOW read / HIGH side effect",
                "destructive": "HIGH / CRITICAL when broad",
            },
            "legacy_tool_permissions": self.legacy,
        }

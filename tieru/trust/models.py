"""Typed, secret-safe inputs and outputs for Tieru Trust Kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    NETWORK_READ = "network_read"
    EXTERNAL_WRITE = "external_write"
    PROCESS_EXECUTION = "process_execution"
    BROWSER_AUTOMATION = "browser_automation"
    DESTRUCTIVE = "destructive"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class ActionRequest:
    """A normalized action attempt evaluated outside the model prompt."""

    tool_name: str
    capabilities: tuple[Capability, ...]
    operation: str
    target: str = ""
    scope: str = ""
    resource_type: str = ""
    local: bool = False
    network: bool = False
    external_write: bool = False
    destructive: bool = False
    process_execution: bool = False
    browser_action: bool = False
    reversible: bool = True
    read_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def capability(self) -> str:
        """Convenience for single-capability consumers."""
        return self.capabilities[0].value if self.capabilities else "unclassified"


@dataclass(frozen=True)
class TrustDecision:
    allowed: bool
    risk: RiskLevel
    approval_required: bool
    reason_codes: tuple[str, ...]
    explanation: str
    matched_policy: str
    action_fingerprint: str

    def public(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk": self.risk.value,
            "approval_required": self.approval_required,
            "reason_codes": list(self.reason_codes),
            "explanation": self.explanation,
            "matched_policy": self.matched_policy,
            "action_fingerprint": self.action_fingerprint,
        }


@dataclass(frozen=True)
class ApprovalRequest:
    """Backward-compatible, redacted prompt presented to a human approver."""

    tool: str
    risk: str
    read_only: bool
    capabilities: tuple[str, ...]
    args: dict[str, Any]
    argument_hash: str
    reason: str
    operation: str = ""
    target: str = ""
    scope: str = ""
    resource_type: str = ""

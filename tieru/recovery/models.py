"""Typed, durable human-recovery records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RecoveryResolution(StrEnum):
    CONFIRMED_COMPLETED = "confirmed_completed"
    CONFIRMED_NOT_EXECUTED = "confirmed_not_executed"
    ABANDONED = "abandoned"
    RETRY_STEP = "retry_step"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_BLOCKED = "verification_blocked"


@dataclass(frozen=True)
class RecoveryDecision:
    recovery_id: str
    resource_type: str
    resource_id: str
    previous_status: str
    resolution: RecoveryResolution
    note: str
    source: str
    replay_run_id: str
    created_at: str


@dataclass(frozen=True)
class ManualRetryPermit:
    permit_id: str
    recovery_id: str
    action_fingerprint: str
    created_at: str
    consumed_at: str | None


class RecoveryError(RuntimeError):
    """A safe, deterministic rejection of a recovery request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message

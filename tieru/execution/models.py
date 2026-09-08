"""Typed records for local side-effect execution suppression."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExecutionStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ClaimOutcome(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ExecutionRecord:
    action_fingerprint: str
    tool_name: str
    status: ExecutionStatus
    result: str | None
    result_size: int
    result_truncated: bool
    attempt_count: int
    retryable: bool
    started_at: str
    completed_at: str | None
    updated_at: str
    completion_source: str = "tool"
    recovery_id: str | None = None


@dataclass(frozen=True)
class ExecutionClaim:
    outcome: ClaimOutcome
    record: ExecutionRecord
    manual_retry_permit_id: str = ""
    manual_retry_recovery_id: str = ""

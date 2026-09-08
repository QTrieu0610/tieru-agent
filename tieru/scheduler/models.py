"""Typed state for Tieru's local durable scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TriggerType(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"


class ScheduleRunStatus(StrEnum):
    CLAIMED = "claimed"
    MATERIALIZED = "materialized"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED_OVERLAP = "skipped_overlap"


TERMINAL_RUN_STATUSES = frozenset(
    {
        ScheduleRunStatus.COMPLETED,
        ScheduleRunStatus.FAILED,
        ScheduleRunStatus.CANCELLED,
        ScheduleRunStatus.SKIPPED_OVERLAP,
    }
)


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    name: str
    goal: str
    status: ScheduleStatus
    trigger_type: TriggerType
    trigger_spec: str
    timezone: str
    next_run_at: str | None
    last_run_at: str | None
    overlap_policy: str
    misfire_policy: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ScheduleRun:
    run_id: str
    schedule_id: str
    scheduled_for: str
    status: ScheduleRunStatus
    task_id: str | None
    claimed_at: str
    completed_at: str | None
    outcome: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TickResult:
    claimed: int = 0
    materialized: int = 0
    advanced: int = 0
    reconciled: int = 0
    blocked: int = 0
    skipped_overlap: int = 0


class ScheduleValidationError(ValueError):
    """Raised before invalid schedule data reaches SQLite."""


class ScheduleStateError(RuntimeError):
    """Raised for an illegal lifecycle operation."""

"""SQLite persistence and atomic occurrence claims for local schedules."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from uuid import uuid4

from tieru.memory.personal import redact_secrets
from tieru.scheduler.models import (
    Schedule,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleStateError,
    ScheduleStatus,
    ScheduleValidationError,
    TriggerType,
)
from tieru.scheduler.recurrence import iso, latest_due

SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','completed','cancelled')),
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('once','interval')),
    trigger_spec TEXT NOT NULL,
    timezone TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    overlap_policy TEXT NOT NULL DEFAULT 'forbid' CHECK(overlap_policy='forbid'),
    misfire_policy TEXT NOT NULL DEFAULT 'latest' CHECK(misfire_policy='latest'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules(schedule_id),
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('claimed','materialized','running','completed','blocked','failed','cancelled',
         'skipped_overlap')),
    task_id TEXT REFERENCES tasks(task_id),
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(schedule_id, scheduled_for)
);

CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules(status, next_run_at);
CREATE INDEX IF NOT EXISTS schedule_runs_schedule_idx
    ON schedule_runs(schedule_id, scheduled_for);
CREATE INDEX IF NOT EXISTS schedule_runs_status_idx ON schedule_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS schedule_runs_task_idx ON schedule_runs(task_id);
"""


def initialize_scheduler_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEDULER_SCHEMA)


def new_schedule_id() -> str:
    return f"schedule_{uuid4().hex}"


def new_schedule_run_id() -> str:
    return f"schedule_run_{uuid4().hex}"


def _now(value: datetime | None = None) -> str:
    return iso(value or datetime.now(UTC))


def _safe(value: str, *, field: str, limit: int, required: bool = True) -> str:
    safe = redact_secrets(str(value or "")).strip()
    if required and not safe:
        raise ScheduleValidationError(f"{field} must not be empty")
    if len(safe.encode("utf-8")) > limit:
        raise ScheduleValidationError(f"{field} exceeds {limit} bytes")
    return safe


class ScheduleStore:
    """Source of truth for schedule definitions and logical occurrences."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        initialize_scheduler_schema(conn)

    @staticmethod
    def _schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            schedule_id=row["schedule_id"], name=row["name"], goal=row["goal"],
            status=ScheduleStatus(row["status"]),
            trigger_type=TriggerType(row["trigger_type"]),
            trigger_spec=row["trigger_spec"], timezone=row["timezone"],
            next_run_at=row["next_run_at"], last_run_at=row["last_run_at"],
            overlap_policy=row["overlap_policy"], misfire_policy=row["misfire_policy"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> ScheduleRun:
        return ScheduleRun(
            run_id=row["run_id"], schedule_id=row["schedule_id"],
            scheduled_for=row["scheduled_for"], status=ScheduleRunStatus(row["status"]),
            task_id=row["task_id"], claimed_at=row["claimed_at"],
            completed_at=row["completed_at"], outcome=row["outcome"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create(
        self, *, name: str, goal: str, trigger_type: TriggerType, trigger_spec: str,
        timezone: str, next_run_at: str, schedule_id: str | None = None,
    ) -> Schedule:
        identifier = schedule_id or new_schedule_id()
        safe_name = _safe(name, field="name", limit=256)
        safe_goal = _safe(goal, field="goal", limit=4096)
        safe_timezone = _safe(timezone, field="timezone", limit=128)
        now = _now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO schedules
                   (schedule_id,name,goal,status,trigger_type,trigger_spec,timezone,
                    next_run_at,overlap_policy,misfire_policy,created_at,updated_at)
                   VALUES (?,?,?,'active',?,?,?,?,'forbid','latest',?,?)""",
                (
                    identifier, safe_name, safe_goal, trigger_type.value, trigger_spec,
                    safe_timezone, next_run_at, now, now,
                ),
            )
            self.conn.commit()
        return self.get(identifier)

    def get(self, schedule_id: str) -> Schedule:
        row = self.conn.execute(
            "SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)
        ).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        return self._schedule(row)

    def list(self, *, status: ScheduleStatus | None = None, limit: int = 100) -> list[Schedule]:
        bound = max(1, min(int(limit), 500))
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM schedules ORDER BY created_at DESC LIMIT ?", (bound,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM schedules WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status.value, bound),
            ).fetchall()
        return [self._schedule(row) for row in rows]

    def list_runs(self, schedule_id: str, *, limit: int = 100) -> list[ScheduleRun]:
        rows = self.conn.execute(
            """SELECT * FROM schedule_runs WHERE schedule_id=?
               ORDER BY scheduled_for DESC LIMIT ?""",
            (schedule_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self._run(row) for row in rows]

    def get_run(self, run_id: str) -> ScheduleRun:
        row = self.conn.execute(
            "SELECT * FROM schedule_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    def transition(self, schedule_id: str, target: ScheduleStatus) -> Schedule:
        with self._lock:
            current = self.get(schedule_id)
            allowed = {
                ScheduleStatus.ACTIVE: {ScheduleStatus.PAUSED, ScheduleStatus.CANCELLED},
                ScheduleStatus.PAUSED: {ScheduleStatus.ACTIVE, ScheduleStatus.CANCELLED},
                ScheduleStatus.COMPLETED: set(), ScheduleStatus.CANCELLED: set(),
            }
            if current.status is target:
                return current
            if target not in allowed[current.status]:
                raise ScheduleStateError(
                    f"illegal schedule transition: {current.status.value} -> {target.value}"
                )
            self.conn.execute(
                "UPDATE schedules SET status=?,updated_at=? WHERE schedule_id=?",
                (target.value, _now(), schedule_id),
            )
            self.conn.commit()
        return self.get(schedule_id)

    def claim_due(self, now: datetime, *, limit: int) -> list[ScheduleRun]:
        """Atomically claim at most one coalesced occurrence per due schedule."""
        bound = max(1, min(int(limit), 100))
        claimed: list[str] = []
        current = now.astimezone(UTC)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                rows = self.conn.execute(
                    """SELECT * FROM schedules
                       WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?
                       ORDER BY next_run_at,schedule_id LIMIT ?""",
                    (iso(current), bound),
                ).fetchall()
                stamp = iso(current)
                for row in rows:
                    schedule = self._schedule(row)
                    logical, successor = latest_due(
                        schedule.trigger_type, schedule.trigger_spec,
                        schedule.next_run_at or "", current,
                    )
                    scheduled_for = iso(logical)
                    overlap = self.conn.execute(
                        """SELECT 1 FROM schedule_runs WHERE schedule_id=?
                           AND status IN ('claimed','materialized','running','blocked') LIMIT 1""",
                        (schedule.schedule_id,),
                    ).fetchone()
                    status = (
                        ScheduleRunStatus.SKIPPED_OVERLAP
                        if overlap is not None else ScheduleRunStatus.CLAIMED
                    )
                    run_id = new_schedule_run_id()
                    cursor = self.conn.execute(
                        """INSERT OR IGNORE INTO schedule_runs
                           (run_id,schedule_id,scheduled_for,status,claimed_at,completed_at,
                            outcome,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id, schedule.schedule_id, scheduled_for, status.value, stamp,
                            stamp if status is ScheduleRunStatus.SKIPPED_OVERLAP else None,
                            "overlap_forbidden" if status is ScheduleRunStatus.SKIPPED_OVERLAP else "",
                            stamp, stamp,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    self.conn.execute(
                        """UPDATE schedules SET last_run_at=?,next_run_at=?,updated_at=?
                           WHERE schedule_id=?""",
                        (
                            scheduled_for, iso(successor) if successor else None,
                            stamp, schedule.schedule_id,
                        ),
                    )
                    claimed.append(run_id)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return [self.get_run(run_id) for run_id in claimed]

    def pending_runs(self, *, limit: int = 100) -> list[ScheduleRun]:
        rows = self.conn.execute(
            """SELECT * FROM schedule_runs
               WHERE status IN ('claimed','materialized','running','blocked')
               ORDER BY claimed_at,run_id LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return [self._run(row) for row in rows]

    def link_task(self, run_id: str, task_id: str) -> ScheduleRun:
        with self._lock:
            cursor = self.conn.execute(
                """UPDATE schedule_runs SET task_id=?,status='materialized',updated_at=?
                   WHERE run_id=? AND status='claimed' AND (task_id IS NULL OR task_id=?)""",
                (task_id, _now(), run_id, task_id),
            )
            if cursor.rowcount == 0:
                current = self.get_run(run_id)
                if current.task_id != task_id:
                    self.conn.rollback()
                    raise ScheduleStateError("occurrence is linked to a different task")
            self.conn.commit()
        return self.get_run(run_id)

    def mark_running(self, run_id: str) -> ScheduleRun:
        self.conn.execute(
            """UPDATE schedule_runs SET status='running',updated_at=?
               WHERE run_id=? AND status IN ('materialized','running')""",
            (_now(), run_id),
        )
        self.conn.commit()
        return self.get_run(run_id)

    def reconcile(self, run_id: str, status: ScheduleRunStatus, *, outcome: str = "") -> ScheduleRun:
        if status not in {
            ScheduleRunStatus.RUNNING, ScheduleRunStatus.COMPLETED,
            ScheduleRunStatus.BLOCKED, ScheduleRunStatus.FAILED,
            ScheduleRunStatus.CANCELLED,
        }:
            raise ScheduleStateError(f"cannot reconcile occurrence to {status.value}")
        stamp = _now()
        terminal = status in {
            ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED,
            ScheduleRunStatus.CANCELLED,
        }
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                run = self.get_run(run_id)
                self.conn.execute(
                    """UPDATE schedule_runs SET status=?,outcome=?,completed_at=?,updated_at=?
                       WHERE run_id=?""",
                    (status.value, _safe(outcome, field="outcome", limit=512, required=False),
                     stamp if terminal else None, stamp, run_id),
                )
                if terminal:
                    schedule = self.get(run.schedule_id)
                    if schedule.trigger_type is TriggerType.ONCE:
                        self.conn.execute(
                            """UPDATE schedules SET status='completed',updated_at=?
                               WHERE schedule_id=? AND status='active'""",
                            (stamp, schedule.schedule_id),
                        )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_run(run_id)

"""Append-only SQLite recovery audit and execution reconciliation."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from tieru.memory.personal import redact_secrets
from tieru.recovery.models import (
    ManualRetryPermit,
    RecoveryDecision,
    RecoveryError,
    RecoveryResolution,
)

MAX_RECOVERY_NOTE_BYTES = 1024

RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_decisions (
    recovery_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('execution', 'task', 'task_step')),
    resource_id TEXT NOT NULL,
    previous_status TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    replay_run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS recovery_decisions_resource_idx
    ON recovery_decisions(resource_type, resource_id, created_at);
CREATE INDEX IF NOT EXISTS recovery_decisions_created_idx
    ON recovery_decisions(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS recovery_decisions_execution_once_idx
    ON recovery_decisions(resource_id) WHERE resource_type='execution';
CREATE TRIGGER IF NOT EXISTS recovery_decisions_no_update
    BEFORE UPDATE ON recovery_decisions
    BEGIN SELECT RAISE(ABORT, 'recovery decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS recovery_decisions_no_delete
    BEFORE DELETE ON recovery_decisions
    BEGIN SELECT RAISE(ABORT, 'recovery decisions are append-only'); END;

CREATE TABLE IF NOT EXISTS manual_retry_permits (
    permit_id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL UNIQUE REFERENCES recovery_decisions(recovery_id),
    action_fingerprint TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS manual_retry_permits_available_idx
    ON manual_retry_permits(action_fingerprint, consumed_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _bounded_note(note: object) -> str:
    safe = redact_secrets(str(note or "")).strip()
    encoded = safe.encode("utf-8")
    if len(encoded) <= MAX_RECOVERY_NOTE_BYTES:
        return safe
    return encoded[:MAX_RECOVERY_NOTE_BYTES].decode("utf-8", errors="ignore")


def initialize_recovery_schema(conn: sqlite3.Connection) -> None:
    """Install additive M17 tables and execution provenance columns."""
    conn.executescript(RECOVERY_SCHEMA)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(tool_executions)").fetchall()
    }
    if "completion_source" not in columns:
        conn.execute(
            "ALTER TABLE tool_executions ADD COLUMN completion_source TEXT NOT NULL DEFAULT 'tool'"
        )
    if "recovery_id" not in columns:
        conn.execute("ALTER TABLE tool_executions ADD COLUMN recovery_id TEXT")
    conn.commit()


class RecoveryStore:
    """Own recovery decisions; decision rows are never updated or deleted."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        initialize_recovery_schema(conn)

    @staticmethod
    def _decision(row: sqlite3.Row) -> RecoveryDecision:
        return RecoveryDecision(
            recovery_id=row["recovery_id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            previous_status=row["previous_status"],
            resolution=RecoveryResolution(row["resolution"]),
            note=row["note"],
            source=row["source"],
            replay_run_id=row["replay_run_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _permit(row: sqlite3.Row) -> ManualRetryPermit:
        return ManualRetryPermit(
            permit_id=row["permit_id"],
            recovery_id=row["recovery_id"],
            action_fingerprint=row["action_fingerprint"],
            created_at=row["created_at"],
            consumed_at=row["consumed_at"],
        )

    def get_decisions(self, resource_type: str, resource_id: str) -> list[RecoveryDecision]:
        rows = self.conn.execute(
            """SELECT * FROM recovery_decisions
               WHERE resource_type=? AND resource_id=?
               ORDER BY created_at, recovery_id""",
            (resource_type, resource_id),
        ).fetchall()
        return [self._decision(row) for row in rows]

    def get_permit(self, action_fingerprint: str) -> ManualRetryPermit | None:
        row = self.conn.execute(
            "SELECT * FROM manual_retry_permits WHERE action_fingerprint=?",
            (action_fingerprint,),
        ).fetchone()
        return self._permit(row) if row is not None else None

    def list_uncertain(self, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """SELECT action_fingerprint, tool_name, status, result, result_truncated,
                      attempt_count, retryable, started_at, updated_at,
                      completion_source, recovery_id
               FROM tool_executions WHERE status='uncertain'
               ORDER BY updated_at DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def inspect_execution(self, action_fingerprint: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM tool_executions WHERE action_fingerprint=?",
            (str(action_fingerprint),),
        ).fetchone()
        if row is None:
            raise RecoveryError("execution_not_found", "Execution record was not found.")
        execution = dict(row)
        decisions = [
            asdict(item) for item in self.get_decisions("execution", action_fingerprint)
        ]
        permit = self.get_permit(action_fingerprint)
        return {
            "execution": execution,
            "recovery_decisions": decisions,
            "manual_retry_permit": asdict(permit) if permit else None,
            "task_links": self._task_links(action_fingerprint),
        }

    def _task_links(self, action_fingerprint: str) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """SELECT DISTINCT s.task_id, s.step_id, s.execution_run_id
               FROM task_steps s JOIN replay_events e ON e.run_id=s.execution_run_id
               WHERE e.payload_json LIKE ? ORDER BY s.task_id, s.step_id""",
            (f"%{action_fingerprint}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_execution(
        self,
        action_fingerprint: str,
        resolution: RecoveryResolution,
        *,
        note: str = "",
        source: str = "cli",
        replay_run_id: str = "",
    ) -> tuple[RecoveryDecision, bool]:
        if resolution not in {
            RecoveryResolution.CONFIRMED_COMPLETED,
            RecoveryResolution.CONFIRMED_NOT_EXECUTED,
            RecoveryResolution.ABANDONED,
        }:
            raise RecoveryError("resolution_invalid", "Resolution is invalid for an execution.")
        fingerprint = str(action_fingerprint).strip()
        if not fingerprint or len(fingerprint) > 256:
            raise RecoveryError("execution_invalid", "Execution fingerprint is invalid.")
        safe_note = _bounded_note(note)
        safe_source = redact_secrets(str(source or "human"))[:80]
        now = _now()
        recovery_id = f"recovery_{uuid4().hex}"
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = self.conn.execute(
                    """SELECT * FROM recovery_decisions
                       WHERE resource_type='execution' AND resource_id=?""",
                    (fingerprint,),
                ).fetchone()
                if existing is not None:
                    decision = self._decision(existing)
                    if decision.resolution is resolution:
                        self.conn.commit()
                        return decision, False
                    raise RecoveryError(
                        "recovery_conflict",
                        "Execution already has a different recovery decision.",
                    )
                execution = self.conn.execute(
                    "SELECT * FROM tool_executions WHERE action_fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if execution is None:
                    raise RecoveryError("execution_not_found", "Execution record was not found.")
                if execution["status"] != "uncertain":
                    raise RecoveryError(
                        "execution_not_uncertain",
                        "Only an uncertain execution can be reconciled.",
                    )
                self.conn.execute(
                    """INSERT INTO recovery_decisions
                       (recovery_id, resource_type, resource_id, previous_status,
                        resolution, note, source, replay_run_id, created_at)
                       VALUES (?, 'execution', ?, 'uncertain', ?, ?, ?, ?, ?)""",
                    (
                        recovery_id,
                        fingerprint,
                        resolution.value,
                        safe_note,
                        safe_source,
                        str(replay_run_id)[:160],
                        now,
                    ),
                )
                if resolution is RecoveryResolution.CONFIRMED_COMPLETED:
                    result = json.dumps(
                        {
                            "ok": True,
                            "reconciled": True,
                            "source": "human_confirmation",
                            "message": "Human confirmed that the action completed.",
                        },
                        sort_keys=True,
                    )
                    self.conn.execute(
                        """UPDATE tool_executions
                           SET status='completed', result=?, result_size=?, result_truncated=0,
                               retryable=0, completed_at=?, updated_at=?,
                               completion_source='human_reconciliation', recovery_id=?
                           WHERE action_fingerprint=? AND status='uncertain'""",
                        (result, len(result.encode("utf-8")), now, now, recovery_id, fingerprint),
                    )
                elif resolution is RecoveryResolution.CONFIRMED_NOT_EXECUTED:
                    permit_id = f"permit_{uuid4().hex}"
                    self.conn.execute(
                        """INSERT INTO manual_retry_permits
                           (permit_id, recovery_id, action_fingerprint, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (permit_id, recovery_id, fingerprint, now),
                    )
                    self.conn.execute(
                        """UPDATE tool_executions
                           SET retryable=1, updated_at=?,
                               completion_source='human_reconciliation', recovery_id=?
                           WHERE action_fingerprint=? AND status='uncertain'""",
                        (now, recovery_id, fingerprint),
                    )
                else:
                    self.conn.execute(
                        """UPDATE tool_executions
                           SET retryable=0, updated_at=?,
                               completion_source='human_reconciliation', recovery_id=?
                           WHERE action_fingerprint=? AND status='uncertain'""",
                        (now, recovery_id, fingerprint),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_decisions("execution", fingerprint)[0], True

    def record_task_decision(
        self,
        *,
        resource_type: str,
        resource_id: str,
        previous_status: str,
        resolution: RecoveryResolution,
        note: str,
        source: str,
        replay_run_id: str,
    ) -> RecoveryDecision:
        recovery_id = f"recovery_{uuid4().hex}"
        now = _now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO recovery_decisions
                   (recovery_id, resource_type, resource_id, previous_status,
                    resolution, note, source, replay_run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    recovery_id,
                    resource_type,
                    resource_id,
                    previous_status,
                    resolution.value,
                    _bounded_note(note),
                    redact_secrets(str(source or "human"))[:80],
                    str(replay_run_id)[:160],
                    now,
                ),
            )
            self.conn.commit()
        return self.get_decisions(resource_type, resource_id)[-1]

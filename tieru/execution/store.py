"""SQLite-backed local action ledger with atomic duplicate claims."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from tieru.execution.models import (
    ClaimOutcome,
    ExecutionClaim,
    ExecutionRecord,
    ExecutionStatus,
)
from tieru.memory.personal import redact_secrets

EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_executions (
    action_fingerprint TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed', 'uncertain')),
    result TEXT,
    result_size INTEGER NOT NULL DEFAULT 0,
    result_truncated INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    retryable INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    completion_source TEXT NOT NULL DEFAULT 'tool',
    recovery_id TEXT
);

CREATE INDEX IF NOT EXISTS tool_executions_status_updated_idx
    ON tool_executions(status, updated_at);
"""


def initialize_execution_schema(conn: sqlite3.Connection) -> None:
    """Install the additive ledger schema on new or existing Tieru databases."""
    conn.executescript(EXECUTION_SCHEMA)
    from tieru.recovery.store import initialize_recovery_schema

    initialize_recovery_schema(conn)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class ExecutionStore:
    """Persist local at-most-once claims for authorized side effects."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        max_result_bytes: int = 2048,
        stale_after_seconds: int = 900,
    ) -> None:
        if max_result_bytes < 128:
            raise ValueError("max_result_bytes must be at least 128")
        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be positive")
        self.conn = conn
        if self.conn.row_factory is None:
            self.conn.row_factory = sqlite3.Row
        self.max_result_bytes = max_result_bytes
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.RLock()
        initialize_execution_schema(conn)

    @staticmethod
    def _record(row: sqlite3.Row | tuple) -> ExecutionRecord:
        return ExecutionRecord(
            action_fingerprint=row["action_fingerprint"],
            tool_name=row["tool_name"],
            status=ExecutionStatus(row["status"]),
            result=row["result"],
            result_size=int(row["result_size"] or 0),
            result_truncated=bool(row["result_truncated"]),
            attempt_count=int(row["attempt_count"]),
            retryable=bool(row["retryable"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
            completion_source=row["completion_source"],
            recovery_id=row["recovery_id"],
        )

    def _get_locked(self, action_fingerprint: str) -> ExecutionRecord | None:
        row = self.conn.execute(
            "SELECT * FROM tool_executions WHERE action_fingerprint=?",
            (action_fingerprint,),
        ).fetchone()
        return self._record(row) if row is not None else None

    def get(self, action_fingerprint: str) -> ExecutionRecord | None:
        with self._lock:
            return self._get_locked(action_fingerprint)

    def count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM tool_executions").fetchone()
            return int(row[0])

    def claim(self, action_fingerprint: str, tool_name: str) -> ExecutionClaim:
        """Atomically claim an unseen fingerprint or report its durable state."""
        now = _now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                cursor = self.conn.execute(
                    """INSERT OR IGNORE INTO tool_executions
                       (action_fingerprint, tool_name, status, attempt_count,
                        retryable, started_at, updated_at)
                       VALUES (?, ?, 'in_progress', 1, 0, ?, ?)""",
                    (action_fingerprint, tool_name, now, now),
                )
                record = self._get_locked(action_fingerprint)
                if record is None:
                    raise sqlite3.IntegrityError("execution claim disappeared")
                if cursor.rowcount == 1:
                    self.conn.commit()
                    return ExecutionClaim(ClaimOutcome.CLAIMED, record)

                if record.status is ExecutionStatus.UNCERTAIN and record.retryable:
                    permit = self.conn.execute(
                        """SELECT permit_id, recovery_id FROM manual_retry_permits
                           WHERE action_fingerprint=? AND consumed_at IS NULL""",
                        (action_fingerprint,),
                    ).fetchone()
                    if permit is not None:
                        consumed = self.conn.execute(
                            """UPDATE manual_retry_permits SET consumed_at=?
                               WHERE permit_id=? AND consumed_at IS NULL""",
                            (now, permit["permit_id"]),
                        )
                        reclaimed = self.conn.execute(
                            """UPDATE tool_executions
                               SET status='in_progress', retryable=0,
                                   attempt_count=attempt_count+1, started_at=?,
                                   completed_at=NULL, updated_at=?,
                                   completion_source='manual_retry'
                               WHERE action_fingerprint=? AND status='uncertain'
                                     AND retryable=1""",
                            (now, now, action_fingerprint),
                        )
                        if consumed.rowcount != 1 or reclaimed.rowcount != 1:
                            raise sqlite3.IntegrityError(
                                "manual retry permit could not be consumed atomically"
                            )
                        record = self._get_locked(action_fingerprint)
                        if record is None:
                            raise sqlite3.IntegrityError("reclaimed execution disappeared")
                        self.conn.commit()
                        return ExecutionClaim(
                            ClaimOutcome.CLAIMED,
                            record,
                            permit["permit_id"],
                            permit["recovery_id"],
                        )

                if record.status is ExecutionStatus.IN_PROGRESS and self._is_stale(record, now):
                    self.conn.execute(
                        """UPDATE tool_executions
                           SET status='uncertain', retryable=0, updated_at=?
                           WHERE action_fingerprint=? AND status='in_progress'""",
                        (now, action_fingerprint),
                    )
                    record = self._get_locked(action_fingerprint)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        outcome = {
            ExecutionStatus.COMPLETED: ClaimOutcome.COMPLETED,
            ExecutionStatus.IN_PROGRESS: ClaimOutcome.IN_PROGRESS,
            ExecutionStatus.FAILED: ClaimOutcome.FAILED,
            ExecutionStatus.UNCERTAIN: ClaimOutcome.UNCERTAIN,
        }[record.status]
        return ExecutionClaim(outcome, record)

    def _is_stale(self, record: ExecutionRecord, now: str) -> bool:
        try:
            updated = datetime.fromisoformat(record.updated_at)
            current = datetime.fromisoformat(now)
        except ValueError:
            return True
        return current - updated >= timedelta(seconds=self.stale_after_seconds)

    def _bounded_result(self, result: str) -> tuple[str, int, bool]:
        safe = redact_secrets(str(result or ""))
        encoded = safe.encode("utf-8")
        size = len(encoded)
        if size <= self.max_result_bytes:
            return safe, size, False
        marker = "\n[TRUNCATED]"
        budget = max(0, self.max_result_bytes - len(marker.encode("utf-8")))
        prefix = encoded[:budget].decode("utf-8", errors="ignore")
        return prefix + marker, size, True

    def _finish(
        self,
        action_fingerprint: str,
        status: ExecutionStatus,
        result: str,
        *,
        retryable: bool = False,
    ) -> ExecutionRecord:
        safe, size, truncated = self._bounded_result(result)
        now = _now()
        with self._lock:
            cursor = self.conn.execute(
                """UPDATE tool_executions
                   SET status=?, result=?, result_size=?, result_truncated=?, retryable=?,
                       completed_at=?, updated_at=?
                   WHERE action_fingerprint=? AND
                         (status='in_progress' OR (status='uncertain' AND recovery_id IS NULL))""",
                (
                    status.value,
                    safe,
                    size,
                    int(truncated),
                    int(retryable),
                    now,
                    now,
                    action_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                self.conn.rollback()
                raise sqlite3.IntegrityError("execution is not claimable by this completion")
            self.conn.commit()
            record = self._get_locked(action_fingerprint)
        if record is None:
            raise sqlite3.IntegrityError("completed execution disappeared")
        return record

    def complete(self, action_fingerprint: str, result: str) -> ExecutionRecord:
        return self._finish(action_fingerprint, ExecutionStatus.COMPLETED, result)

    def fail(
        self, action_fingerprint: str, result: str, *, retryable: bool = False
    ) -> ExecutionRecord:
        return self._finish(
            action_fingerprint, ExecutionStatus.FAILED, result, retryable=retryable
        )

    def mark_uncertain(self, action_fingerprint: str, result: str) -> ExecutionRecord:
        return self._finish(action_fingerprint, ExecutionStatus.UNCERTAIN, result)

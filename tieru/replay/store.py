"""SQLite persistence for Replay; no raw SQL escapes this module."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tieru.replay.models import NormalizedEvent, ReplayEvent, ReplayRun


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"run_{stamp}_{uuid4().hex[:10]}"


def _run(row: sqlite3.Row) -> ReplayRun:
    data = dict(row)
    data["run_id"] = data.pop("id")
    return ReplayRun(**data)


def _event(row: sqlite3.Row) -> ReplayEvent:
    data = dict(row)
    data["event_id"] = data.pop("id")
    data["safe_payload"] = json.loads(data.pop("payload_json") or "{}")
    return ReplayEvent(**data)


class ReplayStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.RLock()

    def start_run(
        self,
        *,
        session_id: str,
        source: str,
        role: str,
        model: str,
        provider: str,
        input_preview: str,
        run_id: str | None = None,
    ) -> ReplayRun:
        run_id = run_id or new_run_id()
        started = now_utc()
        with self._lock:
            self.conn.execute(
                """INSERT INTO replay_runs
                   (id, session_id, source, started_at, status, role, model, provider,
                    input_preview)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
                (run_id, session_id, source, started, role, model, provider, input_preview),
            )
            self.conn.commit()
        return self.get_run(run_id)

    def append(self, run_id: str, normalized: NormalizedEvent) -> ReplayEvent:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM replay_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0])
            event_id = f"evt_{uuid4().hex}"
            timestamp = now_utc()
            payload = json.dumps(
                normalized.safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            self.conn.execute(
                """INSERT INTO replay_events
                   (id, run_id, sequence, timestamp, category, event_type, node, tool,
                    role, model, provider, duration_ms, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    run_id,
                    sequence,
                    timestamp,
                    normalized.category,
                    normalized.event_type,
                    normalized.node,
                    normalized.tool,
                    normalized.role,
                    normalized.model,
                    normalized.provider,
                    normalized.duration_ms,
                    payload,
                ),
            )
            tool_delta = int(
                normalized.event_type in {"tool_completed", "tool_failed", "tool_denied"}
            )
            trust_delta = int(normalized.event_type == "trust_decision")
            self.conn.execute(
                """UPDATE replay_runs
                   SET event_count=event_count+1,
                       tool_count=tool_count+?,
                       trust_decision_count=trust_decision_count+?
                   WHERE id=?""",
                (tool_delta, trust_delta, run_id),
            )
            self.conn.commit()
        return self.get_events(run_id, after_sequence=sequence - 1, limit=1)[0]

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        iterations: int,
        latency_ms: int,
        role: str,
        model: str,
        provider: str,
        output_preview: str = "",
        error_code: str = "",
        error_summary: str = "",
    ) -> ReplayRun:
        with self._lock:
            self.conn.execute(
                """UPDATE replay_runs SET completed_at=?, status=?, iterations=?, latency_ms=?,
                   role=?, model=?, provider=?, output_preview=?, error_code=?, error_summary=?
                   WHERE id=?""",
                (
                    now_utc(),
                    status,
                    iterations,
                    max(0, int(latency_ms)),
                    role,
                    model,
                    provider,
                    output_preview,
                    error_code,
                    error_summary,
                    run_id,
                ),
            )
            self.conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> ReplayRun:
        row = self.conn.execute("SELECT * FROM replay_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run(row)

    def list_runs(
        self, *, limit: int = 50, session_id: str | None = None, status: str | None = None
    ) -> list[ReplayRun]:
        clauses: list[str] = []
        values: list[object] = []
        if session_id:
            clauses.append("session_id=?")
            values.append(session_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        rows = self.conn.execute(
            f"SELECT * FROM replay_runs{where} ORDER BY started_at DESC, id DESC LIMIT ?",  # noqa: S608
            values,
        ).fetchall()
        return [_run(row) for row in rows]

    def get_events(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[ReplayEvent]:
        rows = self.conn.execute(
            """SELECT * FROM replay_events WHERE run_id=? AND sequence>?
               ORDER BY sequence ASC LIMIT ?""",
            (run_id, max(0, int(after_sequence)), max(1, min(int(limit), 5000))),
        ).fetchall()
        return [_event(row) for row in rows]

    def delete_runs(self, run_ids: list[str]) -> int:
        if not run_ids:
            return 0
        placeholders = ",".join("?" for _ in run_ids)
        with self._lock:
            self.conn.execute(
                f"DELETE FROM replay_events WHERE run_id IN ({placeholders})", run_ids  # noqa: S608
            )
            cursor = self.conn.execute(
                f"DELETE FROM replay_runs WHERE id IN ({placeholders})", run_ids  # noqa: S608
            )
            self.conn.commit()
            return int(cursor.rowcount)

    def cleanup(self, *, max_runs: int, max_age_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat(
            timespec="milliseconds"
        )
        old = [
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM replay_runs WHERE started_at < ?", (cutoff,)
            ).fetchall()
        ]
        excess = [
            row[0]
            for row in self.conn.execute(
                """SELECT id FROM replay_runs ORDER BY started_at DESC, id DESC
                   LIMIT -1 OFFSET ?""",
                (max(1, int(max_runs)),),
            ).fetchall()
        ]
        return self.delete_runs(list(dict.fromkeys([*old, *excess])))

    @staticmethod
    def as_dict(record: ReplayRun | ReplayEvent) -> dict:
        return asdict(record)

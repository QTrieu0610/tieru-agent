"""SQLite repository for bounded Shadow patterns and suggestions."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from tieru.forge.models import WorkflowCandidate
from tieru.shadow.models import ShadowPattern, ShadowSuggestion


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _loads(value: str, fallback):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _pattern(row: sqlite3.Row) -> ShadowPattern:
    return ShadowPattern(
        pattern_id=row["id"], workflow_signature=row["workflow_signature"],
        status=row["status"], occurrence_count=row["occurrence_count"],
        successful_count=row["successful_count"],
        verification_count=row["verification_count"], first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"], source_run_ids=_loads(row["evidence_json"], []),
        representative_tools=_loads(row["tools_json"], []),
        representative_operations=_loads(row["operations_json"], []),
        required_capabilities=_loads(row["capabilities_json"], []),
        confidence=row["confidence"], suppress_until_count=row["suppress_until_count"],
        snoozed_until=row["snoozed_until"], forge_draft_id=row["forge_draft_id"],
        metadata=_loads(row["metadata_json"], {}),
    )


def _suggestion(row: sqlite3.Row) -> ShadowSuggestion:
    return ShadowSuggestion(
        suggestion_id=row["id"], pattern_id=row["pattern_id"],
        workflow_signature=row["workflow_signature"], status=row["status"],
        occurrence_count=row["occurrence_count"], confidence=row["confidence"],
        representative_run_ids=_loads(row["representative_runs_json"], []),
        suggested_name=row["suggested_name"], summary=row["summary"],
        required_tools=_loads(row["tools_json"], []),
        required_capabilities=_loads(row["capabilities_json"], []),
        explanation=_loads(row["explanation_json"], []), created_at=row["created_at"],
        updated_at=row["updated_at"], snoozed_until=row["snoozed_until"],
        forge_draft_id=row["forge_draft_id"],
    )


class ShadowStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.RLock()

    def enabled_override(self) -> bool | None:
        row = self.conn.execute(
            "SELECT value FROM shadow_settings WHERE key='enabled'"
        ).fetchone()
        if row is None:
            return None
        return str(row["value"]).lower() == "true"

    def set_enabled(self, enabled: bool) -> None:
        now = now_utc()
        with self._lock:
            self.conn.execute(
                """INSERT INTO shadow_settings(key,value,updated_at) VALUES('enabled',?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""",
                ("true" if enabled else "false", now),
            )
            self.conn.commit()

    def observation(self, run_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM shadow_observations WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def record_ignored(self, run_id: str, result: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO shadow_observations
                   (run_id,pattern_id,result,observed_at) VALUES(?,'',?,?)""",
                (run_id, result[:80], now_utc()),
            )
            self.conn.commit()

    def aggregate(
        self, candidate: WorkflowCandidate, run_id: str, *, max_evidence: int
    ) -> tuple[ShadowPattern, bool]:
        signature = candidate.workflow_signature
        pattern_id = f"shadow_{signature[:20]}"
        now = now_utc()
        verification = int(any(
            step.verification and step.status == "completed" for step in candidate.steps
        ))
        with self._lock:
            if self.observation(run_id):
                return self.get_pattern_by_signature(signature), False
            row = self.conn.execute(
                "SELECT * FROM shadow_patterns WHERE workflow_signature=?", (signature,)
            ).fetchone()
            if row is None:
                evidence = [run_id]
                self.conn.execute(
                    """INSERT INTO shadow_patterns
                       (id,workflow_signature,status,occurrence_count,successful_count,
                        verification_count,first_seen_at,last_seen_at,evidence_json,tools_json,
                        operations_json,capabilities_json,confidence)
                       VALUES(?,?,'observing',1,1,?,?,?,?,?,?,?,'low')""",
                    (
                        pattern_id, signature, verification, now, now,
                        json.dumps(evidence), json.dumps(candidate.tools),
                        json.dumps([step.operation for step in candidate.steps]),
                        json.dumps(candidate.capabilities),
                    ),
                )
            else:
                pattern_id = row["id"]
                evidence = [
                    *[item for item in _loads(row["evidence_json"], []) if item != run_id], run_id
                ][-max(1, max_evidence):]
                status = "observing" if row["status"] == "stale" else row["status"]
                self.conn.execute(
                    """UPDATE shadow_patterns SET status=?,occurrence_count=occurrence_count+1,
                       successful_count=successful_count+1,
                       verification_count=verification_count+?,last_seen_at=?,evidence_json=?
                       WHERE id=?""",
                    (status, verification, now, json.dumps(evidence), pattern_id),
                )
            self.conn.execute(
                """INSERT INTO shadow_observations(run_id,pattern_id,result,observed_at)
                   VALUES(?,?, 'aggregated', ?)""",
                (run_id, pattern_id, now),
            )
            # Replay retention bounds this idempotency ledger without touching patterns.
            self.conn.execute(
                "DELETE FROM shadow_observations WHERE run_id NOT IN (SELECT id FROM replay_runs)"
            )
            self.conn.commit()
        return self.get_pattern(pattern_id), True

    def get_pattern(self, pattern_id: str) -> ShadowPattern:
        row = self.conn.execute(
            "SELECT * FROM shadow_patterns WHERE id=?", (pattern_id,)
        ).fetchone()
        if row is None:
            raise KeyError(pattern_id)
        return _pattern(row)

    def get_pattern_by_signature(self, signature: str) -> ShadowPattern:
        row = self.conn.execute(
            "SELECT * FROM shadow_patterns WHERE workflow_signature=?", (signature,)
        ).fetchone()
        if row is None:
            raise KeyError(signature)
        return _pattern(row)

    def list_patterns(self) -> list[ShadowPattern]:
        rows = self.conn.execute(
            "SELECT * FROM shadow_patterns ORDER BY last_seen_at DESC,id"
        ).fetchall()
        return [_pattern(row) for row in rows]

    def update_pattern(self, pattern_id: str, **changes) -> ShadowPattern:
        allowed = {
            "status", "confidence", "suppress_until_count", "snoozed_until",
            "forge_draft_id", "metadata_json",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return self.get_pattern(pattern_id)
        assignments = ",".join(f"{key}=?" for key in values)
        with self._lock:
            self.conn.execute(
                f"UPDATE shadow_patterns SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), pattern_id),
            )
            self.conn.commit()
        return self.get_pattern(pattern_id)

    def upsert_suggestion(
        self, pattern: ShadowPattern, *, name: str, summary: str, explanation: list[str]
    ) -> ShadowSuggestion:
        now = now_utc()
        suggestion_id = f"suggest_{pattern.workflow_signature[:20]}"
        runs = pattern.source_run_ids[-3:]
        with self._lock:
            self.conn.execute(
                """INSERT INTO shadow_suggestions
                   (id,pattern_id,workflow_signature,status,occurrence_count,confidence,
                    representative_runs_json,suggested_name,summary,tools_json,
                    capabilities_json,explanation_json,created_at,updated_at)
                   VALUES(?,?,?,'ready',?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pattern_id) DO UPDATE SET status='ready',
                    occurrence_count=excluded.occurrence_count,
                    confidence=excluded.confidence,
                    representative_runs_json=excluded.representative_runs_json,
                    suggested_name=excluded.suggested_name,summary=excluded.summary,
                    tools_json=excluded.tools_json,
                    capabilities_json=excluded.capabilities_json,
                    explanation_json=excluded.explanation_json,
                    updated_at=excluded.updated_at,snoozed_until='',forge_draft_id=''""",
                (
                    suggestion_id, pattern.pattern_id, pattern.workflow_signature,
                    pattern.occurrence_count, pattern.confidence, json.dumps(runs), name,
                    summary, json.dumps(pattern.representative_tools),
                    json.dumps(pattern.required_capabilities), json.dumps(explanation), now, now,
                ),
            )
            self.conn.commit()
        return self.get_suggestion(suggestion_id)

    def get_suggestion(self, suggestion_id: str) -> ShadowSuggestion:
        row = self.conn.execute(
            "SELECT * FROM shadow_suggestions WHERE id=?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise KeyError(suggestion_id)
        return _suggestion(row)

    def suggestion_for_pattern(self, pattern_id: str) -> ShadowSuggestion | None:
        row = self.conn.execute(
            "SELECT * FROM shadow_suggestions WHERE pattern_id=?", (pattern_id,)
        ).fetchone()
        return _suggestion(row) if row else None

    def list_suggestions(self, *, include_inactive: bool = False) -> list[ShadowSuggestion]:
        where = "" if include_inactive else " WHERE status IN ('ready','snoozed')"
        rows = self.conn.execute(
            f"SELECT * FROM shadow_suggestions{where} ORDER BY updated_at DESC,id"  # noqa: S608
        ).fetchall()
        return [_suggestion(row) for row in rows]

    def update_suggestion(self, suggestion_id: str, **changes) -> ShadowSuggestion:
        allowed = {"status", "snoozed_until", "forge_draft_id", "updated_at"}
        values = {key: value for key, value in changes.items() if key in allowed}
        values.setdefault("updated_at", now_utc())
        assignments = ",".join(f"{key}=?" for key in values)
        with self._lock:
            self.conn.execute(
                f"UPDATE shadow_suggestions SET {assignments} WHERE id=?",  # noqa: S608
                (*values.values(), suggestion_id),
            )
            self.conn.commit()
        return self.get_suggestion(suggestion_id)

    def mark_stale(self, stale_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, stale_days))).isoformat(
            timespec="seconds"
        )
        with self._lock:
            cursor = self.conn.execute(
                """UPDATE shadow_patterns SET status='stale'
                   WHERE last_seen_at < ? AND status IN ('observing','suggestion_ready')""",
                (cutoff,),
            )
            self.conn.commit()
        return int(cursor.rowcount)

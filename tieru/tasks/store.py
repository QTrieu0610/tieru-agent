"""SQLite persistence and atomic transitions for local durable tasks."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from tieru.memory.personal import redact_secrets
from tieru.tasks.models import (
    STEP_TRANSITIONS,
    TASK_TRANSITIONS,
    BudgetAllocation,
    BudgetDecision,
    BudgetExhaustionReason,
    BudgetReservationResult,
    BudgetResource,
    CriterionResult,
    CriterionStatus,
    ExecutionCheckpoint,
    GoalConstraint,
    GoalContract,
    GoalContractError,
    GoalVerificationError,
    GoalVerificationStatus,
    ModelCallCriticality,
    ModelCallPurpose,
    PlanStep,
    PlanValidationError,
    ReplanLimitExceededError,
    StepClaim,
    StepClaimOutcome,
    StepEvidenceRequirement,
    StepExecutionKind,
    StepStatus,
    SuccessCriterion,
    Task,
    TaskBudget,
    TaskBudgetUsage,
    TaskGoalVerification,
    TaskLimits,
    TaskPlanRevision,
    TaskStateError,
    TaskStatus,
    TaskStep,
    TaskValidationError,
    VerificationResult,
    VerificationStatus,
)

TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('planned', 'running', 'paused', 'blocked', 'failed', 'completed', 'cancelled')),
    current_step_id TEXT,
    source TEXT NOT NULL,
    source_id TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_plan_revisions (
    revision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    trigger_step_id TEXT REFERENCES task_steps(step_id),
    created_at TEXT NOT NULL,
    UNIQUE(task_id, revision_number)
);

CREATE TABLE IF NOT EXISTS task_steps (
    step_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    verification_instruction TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN
        ('pending', 'running', 'succeeded', 'failed', 'blocked', 'skipped', 'superseded')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    result TEXT,
    result_size INTEGER NOT NULL DEFAULT 0,
    result_truncated INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT CHECK(verification_status IS NULL OR verification_status IN
        ('pass', 'fail', 'blocked', 'skipped', 'unknown')),
    verification_summary TEXT,
    execution_run_id TEXT,
    plan_revision_id TEXT,
    superseded_by_revision TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, position)
);

CREATE TABLE IF NOT EXISTS task_goal_contracts (
    contract_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    constraints_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(task_id)
);

CREATE TABLE IF NOT EXISTS task_goal_criteria (
    contract_id TEXT NOT NULL REFERENCES task_goal_contracts(contract_id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    description TEXT NOT NULL,
    verification_kind TEXT NOT NULL DEFAULT 'deterministic',
    required_evidence TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(contract_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS task_goal_verifications (
    verification_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    plan_revision_number INTEGER NOT NULL DEFAULT 0,
    evidence_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_goal_criterion_results (
    result_id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES task_goal_verifications(verification_id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_budgets (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    max_model_calls INTEGER NOT NULL,
    max_tool_calls INTEGER NOT NULL,
    max_steps INTEGER NOT NULL,
    max_replans INTEGER NOT NULL,
    max_verification_calls INTEGER NOT NULL,
    max_retries INTEGER NOT NULL,
    max_command_runtime_seconds REAL NOT NULL,
    max_active_runtime_seconds REAL NOT NULL,
    max_input_tokens INTEGER,
    max_output_tokens INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_budget_usages (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    steps_started INTEGER NOT NULL DEFAULT 0,
    replans INTEGER NOT NULL DEFAULT 0,
    verification_calls INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    command_runtime_seconds REAL NOT NULL DEFAULT 0.0,
    active_runtime_seconds REAL NOT NULL DEFAULT 0.0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_budget_allocations (
    allocation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    amount REAL NOT NULL,
    previous_limit REAL NOT NULL,
    new_limit REAL NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tasks_status_updated_idx ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS task_steps_task_position_idx ON task_steps(task_id, position);
CREATE INDEX IF NOT EXISTS task_steps_task_status_idx ON task_steps(task_id, status);
CREATE INDEX IF NOT EXISTS task_steps_run_idx ON task_steps(execution_run_id);
CREATE INDEX IF NOT EXISTS task_steps_revision_idx ON task_steps(task_id, plan_revision_id);
CREATE INDEX IF NOT EXISTS task_plan_revisions_task_idx ON task_plan_revisions(task_id, revision_number);
CREATE UNIQUE INDEX IF NOT EXISTS task_plan_revisions_trigger_idx
    ON task_plan_revisions(task_id, trigger_step_id) WHERE trigger_step_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS task_goal_verifications_task_idx
    ON task_goal_verifications(task_id, plan_revision_number);
CREATE INDEX IF NOT EXISTS task_goal_criterion_results_ver_idx
    ON task_goal_criterion_results(verification_id);
CREATE INDEX IF NOT EXISTS task_budget_allocations_task_idx
    ON task_budget_allocations(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_execution_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    path TEXT,
    before_hash TEXT,
    after_hash TEXT,
    exists_flag INTEGER,
    action_ledger_id TEXT,
    consumed_by_verifier INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS task_checkpoints_task_step_idx
    ON task_execution_checkpoints(task_id, step_id);
"""


def initialize_task_schema(conn: sqlite3.Connection) -> None:
    """Install the additive M15/M22/M25 schema on new or existing state databases."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_steps'"
    ).fetchone()
    if row is not None and "superseded" not in str(row[0] or ""):
        conn.execute("ALTER TABLE task_steps RENAME TO _task_steps_old")
        conn.executescript(TASK_SCHEMA)
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(_task_steps_old)").fetchall()}
        common_cols = [
            "step_id", "task_id", "position", "title", "instruction",
            "verification_instruction", "status", "attempt_count", "max_attempts",
            "result", "result_size", "result_truncated", "verification_status",
            "verification_summary", "execution_run_id", "started_at",
            "completed_at", "updated_at",
        ]
        cols_str = ", ".join(c for c in common_cols if c in old_cols)
        conn.execute(f"INSERT INTO task_steps ({cols_str}) SELECT {cols_str} FROM _task_steps_old")
        conn.execute("DROP TABLE _task_steps_old")
    else:
        conn.executescript(TASK_SCHEMA)

    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "source_id" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN source_id TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS tasks_source_identity_idx
           ON tasks(source, source_id) WHERE source_id IS NOT NULL"""
    )
    step_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_steps)").fetchall()}
    if "plan_revision_id" not in step_columns:
        conn.execute("ALTER TABLE task_steps ADD COLUMN plan_revision_id TEXT")
    if "superseded_by_revision" not in step_columns:
        conn.execute("ALTER TABLE task_steps ADD COLUMN superseded_by_revision TEXT")

    # M25 budget tables for existing databases
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_budgets (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
            max_model_calls INTEGER NOT NULL,
            max_tool_calls INTEGER NOT NULL,
            max_steps INTEGER NOT NULL,
            max_replans INTEGER NOT NULL,
            max_verification_calls INTEGER NOT NULL,
            max_retries INTEGER NOT NULL,
            max_command_runtime_seconds REAL NOT NULL,
            max_active_runtime_seconds REAL NOT NULL,
            max_input_tokens INTEGER,
            max_output_tokens INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_budget_usages (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
            model_calls INTEGER NOT NULL DEFAULT 0,
            tool_calls INTEGER NOT NULL DEFAULT 0,
            steps_started INTEGER NOT NULL DEFAULT 0,
            replans INTEGER NOT NULL DEFAULT 0,
            verification_calls INTEGER NOT NULL DEFAULT 0,
            retries INTEGER NOT NULL DEFAULT 0,
            command_runtime_seconds REAL NOT NULL DEFAULT 0.0,
            active_runtime_seconds REAL NOT NULL DEFAULT 0.0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_budget_allocations (
            allocation_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            resource TEXT NOT NULL,
            amount REAL NOT NULL,
            previous_limit REAL NOT NULL,
            new_limit REAL NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS task_budget_allocations_task_idx
           ON task_budget_allocations(task_id, created_at)"""
    )
    cursor = conn.execute("PRAGMA table_info(task_steps)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "execution_kind" not in existing_cols:
        try:
            conn.execute("ALTER TABLE task_steps ADD COLUMN execution_kind TEXT")
        except Exception:
            pass
    if "evidence_requirements_json" not in existing_cols:
        try:
            conn.execute("ALTER TABLE task_steps ADD COLUMN evidence_requirements_json TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass

    # M35 execution checkpoint table for existing databases
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_execution_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            step_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            timed_out INTEGER NOT NULL DEFAULT 0,
            duration_ms REAL NOT NULL DEFAULT 0.0,
            path TEXT,
            before_hash TEXT,
            after_hash TEXT,
            exists_flag INTEGER,
            action_ledger_id TEXT,
            consumed_by_verifier INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS task_checkpoints_task_step_idx
           ON task_execution_checkpoints(task_id, step_id)"""
    )
    conn.commit()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_task_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"task_{stamp}_{uuid4().hex[:10]}"


def _bounded_safe(value: object, limit: int, *, field: str, truncate: bool) -> tuple[str, int, bool]:
    safe = redact_secrets(str(value or "")).strip()
    encoded = safe.encode("utf-8")
    size = len(encoded)
    if size <= limit:
        return safe, size, False
    if not truncate:
        raise TaskValidationError(f"{field} exceeds {limit} bytes")
    marker = "\n[TRUNCATED]"
    budget = max(0, limit - len(marker.encode("utf-8")))
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    return prefix + marker, size, True


_RESOURCE_MAP: dict[str, tuple[str, str]] = {
    "model_calls": ("max_model_calls", "model_calls"),
    "tool_calls": ("max_tool_calls", "tool_calls"),
    "steps": ("max_steps", "steps_started"),
    "replans": ("max_replans", "replans"),
    "verification_calls": ("max_verification_calls", "verification_calls"),
    "retries": ("max_retries", "retries"),
    "command_runtime": ("max_command_runtime_seconds", "command_runtime_seconds"),
    "command_runtime_seconds": ("max_command_runtime_seconds", "command_runtime_seconds"),
    "active_runtime": ("max_active_runtime_seconds", "active_runtime_seconds"),
    "active_runtime_seconds": ("max_active_runtime_seconds", "active_runtime_seconds"),
    "input_tokens": ("max_input_tokens", "input_tokens"),
    "output_tokens": ("max_output_tokens", "output_tokens"),
}

_EXHAUSTION_REASONS: dict[str, BudgetExhaustionReason] = {
    "model_calls": BudgetExhaustionReason.MODEL_CALL_LIMIT,
    "tool_calls": BudgetExhaustionReason.TOOL_CALL_LIMIT,
    "steps": BudgetExhaustionReason.STEP_LIMIT,
    "replans": BudgetExhaustionReason.REPLAN_LIMIT,
    "verification_calls": BudgetExhaustionReason.VERIFICATION_LIMIT,
    "retries": BudgetExhaustionReason.RETRY_LIMIT,
    "command_runtime": BudgetExhaustionReason.COMMAND_RUNTIME_LIMIT,
    "command_runtime_seconds": BudgetExhaustionReason.COMMAND_RUNTIME_LIMIT,
    "active_runtime": BudgetExhaustionReason.ACTIVE_RUNTIME_LIMIT,
    "active_runtime_seconds": BudgetExhaustionReason.ACTIVE_RUNTIME_LIMIT,
    "input_tokens": BudgetExhaustionReason.TOKEN_LIMIT,
    "output_tokens": BudgetExhaustionReason.TOKEN_LIMIT,
}


class TaskStore:
    """Source of truth for durable tasks and their step checkpoints."""

    def __init__(self, conn: sqlite3.Connection, *, limits: TaskLimits | None = None) -> None:
        self.conn = conn
        if self.conn.row_factory is None:
            self.conn.row_factory = sqlite3.Row
        self.limits = limits or TaskLimits()
        self._lock = threading.RLock()
        initialize_task_schema(conn)

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            goal=row["goal"],
            status=TaskStatus(row["status"]),
            current_step_id=row["current_step_id"],
            source=row["source"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            source_id=row["source_id"],
        )

    @staticmethod
    def _step(row: sqlite3.Row) -> TaskStep:
        verification = row["verification_status"]
        keys = row.keys()
        exec_kind = row["execution_kind"] if "execution_kind" in keys else None
        ev_req_raw = row["evidence_requirements_json"] if "evidence_requirements_json" in keys else None
        ev_reqs: list[StepEvidenceRequirement] = []
        if ev_req_raw:
            try:
                parsed_reqs = json.loads(ev_req_raw)
                if isinstance(parsed_reqs, list):
                    for r in parsed_reqs:
                        if isinstance(r, dict):
                            ev_reqs.append(
                                StepEvidenceRequirement(
                                    kind=str(r.get("kind") or "tool_success"),
                                    description=str(r.get("description") or ""),
                                    required=bool(r.get("required", True)),
                                )
                            )
            except Exception:
                pass
        return TaskStep(
            step_id=row["step_id"],
            task_id=row["task_id"],
            position=int(row["position"]),
            title=row["title"],
            instruction=row["instruction"],
            verification_instruction=row["verification_instruction"],
            status=StepStatus(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            result=row["result"],
            result_size=int(row["result_size"] or 0),
            result_truncated=bool(row["result_truncated"]),
            verification_status=(VerificationStatus(verification) if verification else None),
            verification_summary=row["verification_summary"],
            execution_run_id=row["execution_run_id"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
            plan_revision_id=(row["plan_revision_id"] if "plan_revision_id" in keys else None),
            superseded_by_revision=(row["superseded_by_revision"] if "superseded_by_revision" in keys else None),
            execution_kind=(StepExecutionKind(exec_kind) if exec_kind else None),
            evidence_requirements=tuple(ev_reqs),
        )

    @staticmethod
    def _revision(row: sqlite3.Row) -> TaskPlanRevision:
        return TaskPlanRevision(
            revision_id=row["revision_id"],
            task_id=row["task_id"],
            revision_number=int(row["revision_number"]),
            reason=row["reason"],
            trigger_step_id=row["trigger_step_id"],
            created_at=row["created_at"],
        )

    def _default_contract(self, task_id: str, goal: str, now: str) -> GoalContract:
        return GoalContract(
            contract_id=f"contract_{uuid4().hex[:12]}",
            task_id=task_id,
            goal=goal,
            success_criteria=(
                SuccessCriterion(
                    criterion_id="sc_1",
                    description="All plan steps execute and verify successfully to fulfill the goal.",
                    verification_kind="deterministic",
                    required_evidence="step_verification",
                    required=True,
                ),
            ),
            constraints=(),
            created_at=now,
        )

    def _persist_goal_contract_tx(self, task_id: str, contract: GoalContract, now: str) -> None:
        if len(contract.success_criteria) > self.limits.max_criteria_per_contract:
            raise GoalContractError(
                f"criteria count {len(contract.success_criteria)} exceeds limit {self.limits.max_criteria_per_contract}"
            )
        if len(contract.constraints) > self.limits.max_constraints_per_contract:
            raise GoalContractError(
                f"constraints count {len(contract.constraints)} exceeds limit {self.limits.max_constraints_per_contract}"
            )
        for criterion in contract.success_criteria:
            if len(criterion.description.encode("utf-8")) > self.limits.max_criterion_description_bytes:
                raise GoalContractError("criterion description exceeds size limit")
        for constraint in contract.constraints:
            if len(constraint.description.encode("utf-8")) > self.limits.max_constraint_description_bytes:
                raise GoalContractError("constraint description exceeds size limit")

        contract_id = contract.contract_id or f"contract_{uuid4().hex[:12]}"
        constraints_list = [
            {
                "constraint_id": c.constraint_id,
                "description": c.description,
                "constraint_kind": c.constraint_kind,
                "required": c.required,
            }
            for c in contract.constraints
        ]
        self.conn.execute(
            """INSERT INTO task_goal_contracts
               (contract_id, task_id, goal, constraints_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (contract_id, task_id, contract.goal, json.dumps(constraints_list), now),
        )
        for sc in contract.success_criteria:
            self.conn.execute(
                """INSERT INTO task_goal_criteria
                   (contract_id, criterion_id, description, verification_kind, required_evidence, required)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    contract_id,
                    sc.criterion_id,
                    sc.description,
                    sc.verification_kind,
                    sc.required_evidence,
                    1 if sc.required else 0,
                ),
            )

    def _persist_task_budget_tx(self, task_id: str, budget: TaskBudget, now: str) -> None:
        self.conn.execute(
            """INSERT INTO task_budgets
               (task_id, max_model_calls, max_tool_calls, max_steps, max_replans,
                max_verification_calls, max_retries, max_command_runtime_seconds,
                max_active_runtime_seconds, max_input_tokens, max_output_tokens,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                budget.max_model_calls,
                budget.max_tool_calls,
                budget.max_steps,
                budget.max_replans,
                budget.max_verification_calls,
                budget.max_retries,
                budget.max_command_runtime_seconds,
                budget.max_active_runtime_seconds,
                budget.max_input_tokens,
                budget.max_output_tokens,
                now,
                now,
            ),
        )
        self.conn.execute(
            """INSERT INTO task_budget_usages
               (task_id, model_calls, tool_calls, steps_started, replans,
                verification_calls, retries, command_runtime_seconds,
                active_runtime_seconds, input_tokens, output_tokens, updated_at)
               VALUES (?, 0, 0, 0, 0, 0, 0, 0.0, 0.0, NULL, NULL, ?)""",
            (task_id, now),
        )
        alloc_id = f"alloc_{uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO task_budget_allocations
               (allocation_id, task_id, action, resource, amount, previous_limit,
                new_limit, actor, note, created_at)
               VALUES (?, ?, 'create', 'all', 0, 0, 0, 'system', 'Initial task budget created', ?)""",
            (alloc_id, task_id, now),
        )

    def create_task(
        self,
        goal: str,
        plan: list[PlanStep],
        *,
        contract: GoalContract | None = None,
        budget: TaskBudget | None = None,
        source: str = "cli",
        source_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> Task:
        safe_goal, _, _ = _bounded_safe(
            goal, self.limits.max_goal_bytes, field="goal", truncate=False
        )
        if not safe_goal:
            raise TaskValidationError("goal must not be empty")
        if not 1 <= len(plan) <= self.limits.max_steps_per_task:
            raise TaskValidationError(
                f"plan must contain 1-{self.limits.max_steps_per_task} steps"
            )
        prepared: list[tuple[str, str, str, str | None, str]] = []
        for index, item in enumerate(plan, 1):
            title, _, _ = _bounded_safe(
                item.title, self.limits.max_title_bytes, field=f"step {index} title", truncate=False
            )
            instruction, _, _ = _bounded_safe(
                item.instruction,
                self.limits.max_instruction_bytes,
                field=f"step {index} instruction",
                truncate=False,
            )
            verification, _, _ = _bounded_safe(
                item.verification,
                self.limits.max_instruction_bytes,
                field=f"step {index} verification",
                truncate=False,
            )
            if not title or not instruction or not verification:
                raise TaskValidationError(f"step {index} fields must not be empty")
            exec_k = item.execution_kind.value if item.execution_kind else None
            reqs_data = [
                {"kind": req.kind, "description": req.description, "required": req.required}
                for req in (item.evidence_requirements or ())
            ]
            prepared.append(
                (title, instruction, verification, exec_k, json.dumps(reqs_data, ensure_ascii=False))
            )

        safe_source, _, _ = _bounded_safe(
            source or "unknown", 64, field="source", truncate=False
        )
        safe_session = None
        if session_id is not None:
            safe_session, _, _ = _bounded_safe(
                session_id, 256, field="session_id", truncate=False
            )
        safe_source_id = None
        if source_id is not None:
            safe_source_id, _, _ = _bounded_safe(
                source_id, 160, field="source_id", truncate=False
            )
            if not safe_source_id:
                raise TaskValidationError("source_id must not be empty")
        identifier = task_id or new_task_id()
        now = _now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                if safe_source_id is not None:
                    existing = self.conn.execute(
                        "SELECT * FROM tasks WHERE source=? AND source_id=?",
                        (safe_source, safe_source_id),
                    ).fetchone()
                    if existing is not None:
                        self.conn.commit()
                        return self._task(existing)
                self.conn.execute(
                    """INSERT INTO tasks
                       (task_id, goal, status, source, source_id, session_id,
                        created_at, updated_at)
                       VALUES (?, ?, 'planned', ?, ?, ?, ?, ?)""",
                    (
                        identifier, safe_goal, safe_source, safe_source_id,
                        safe_session, now, now,
                    ),
                )
                rev_0_id = f"rev_{uuid4().hex[:12]}"
                self.conn.execute(
                    """INSERT INTO task_plan_revisions
                       (revision_id, task_id, revision_number, reason, trigger_step_id, created_at)
                       VALUES (?, ?, 0, 'Initial plan', NULL, ?)""",
                    (rev_0_id, identifier, now),
                )
                for position, (title, instruction, verification, exec_k, reqs_json) in enumerate(prepared, 1):
                    self.conn.execute(
                        """INSERT INTO task_steps
                           (step_id, task_id, position, title, instruction,
                            verification_instruction, status, max_attempts, plan_revision_id,
                            execution_kind, evidence_requirements_json, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                        (
                            f"step_{uuid4().hex}",
                            identifier,
                            position,
                            title,
                            instruction,
                            verification,
                            self.limits.max_attempts_per_step,
                            rev_0_id,
                            exec_k,
                            reqs_json,
                            now,
                        ),
                    )
                actual_contract = contract or self._default_contract(identifier, safe_goal, now)
                self._persist_goal_contract_tx(identifier, actual_contract, now)
                actual_budget = budget or self.limits.default_budget()
                self._persist_task_budget_tx(identifier, actual_budget, now)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(identifier)

    def get_goal_contract(self, task_id: str) -> GoalContract:
        row = self.conn.execute(
            "SELECT contract_id, task_id, goal, constraints_json, created_at FROM task_goal_contracts WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            task = self.get_task(task_id)
            return self._default_contract(task.task_id, task.goal, task.created_at)
        contract_id, t_id, goal, constraints_json, created_at = row
        raw_constraints = json.loads(constraints_json) if constraints_json else []
        constraints = tuple(
            GoalConstraint(
                constraint_id=c.get("constraint_id", f"c_{idx}"),
                description=c.get("description", ""),
                constraint_kind=c.get("constraint_kind", "user_intent"),
                required=c.get("required", True),
            )
            for idx, c in enumerate(raw_constraints)
        )
        criteria_rows = self.conn.execute(
            "SELECT criterion_id, description, verification_kind, required_evidence, required FROM task_goal_criteria WHERE contract_id=? ORDER BY criterion_id",
            (contract_id,),
        ).fetchall()
        criteria = tuple(
            SuccessCriterion(
                criterion_id=cr[0],
                description=cr[1],
                verification_kind=cr[2],
                required_evidence=cr[3],
                required=bool(cr[4]),
            )
            for cr in criteria_rows
        )
        return GoalContract(
            contract_id=contract_id,
            task_id=t_id,
            goal=goal,
            success_criteria=criteria,
            constraints=constraints,
            created_at=created_at,
        )

    def get_task_budget(self, task_id: str) -> TaskBudget:
        self.get_task(task_id)
        row = self.conn.execute(
            """SELECT max_model_calls, max_tool_calls, max_steps, max_replans,
                      max_verification_calls, max_retries, max_command_runtime_seconds,
                      max_active_runtime_seconds, max_input_tokens, max_output_tokens
               FROM task_budgets WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return self.limits.default_budget()
        return TaskBudget(
            max_model_calls=int(row[0]),
            max_tool_calls=int(row[1]),
            max_steps=int(row[2]),
            max_replans=int(row[3]),
            max_verification_calls=int(row[4]),
            max_retries=int(row[5]),
            max_command_runtime_seconds=float(row[6]),
            max_active_runtime_seconds=float(row[7]),
            max_input_tokens=int(row[8]) if row[8] is not None else None,
            max_output_tokens=int(row[9]) if row[9] is not None else None,
        )

    def get_task_budget_usage(self, task_id: str) -> TaskBudgetUsage:
        self.get_task(task_id)
        row = self.conn.execute(
            """SELECT model_calls, tool_calls, steps_started, replans,
                      verification_calls, retries, command_runtime_seconds,
                      active_runtime_seconds, input_tokens, output_tokens
               FROM task_budget_usages WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return TaskBudgetUsage()
        return TaskBudgetUsage(
            model_calls=int(row[0]),
            tool_calls=int(row[1]),
            steps_started=int(row[2]),
            replans=int(row[3]),
            verification_calls=int(row[4]),
            retries=int(row[5]),
            command_runtime_seconds=float(row[6]),
            active_runtime_seconds=float(row[7]),
            input_tokens=int(row[8]) if row[8] is not None else None,
            output_tokens=int(row[9]) if row[9] is not None else None,
        )

    def get_budget_remaining(self, task_id: str) -> dict[str, Any]:
        budget = self.get_task_budget(task_id)
        usage = self.get_task_budget_usage(task_id)
        return {
            "model_calls": max(0, budget.max_model_calls - usage.model_calls),
            "tool_calls": max(0, budget.max_tool_calls - usage.tool_calls),
            "steps": max(0, budget.max_steps - usage.steps_started),
            "replans": max(0, budget.max_replans - usage.replans),
            "verification_calls": max(0, budget.max_verification_calls - usage.verification_calls),
            "retries": max(0, budget.max_retries - usage.retries),
            "command_runtime_seconds": max(0.0, round(budget.max_command_runtime_seconds - usage.command_runtime_seconds, 3)),
            "active_runtime_seconds": max(0.0, round(budget.max_active_runtime_seconds - usage.active_runtime_seconds, 3)),
            "input_tokens": (max(0, budget.max_input_tokens - usage.input_tokens) if budget.max_input_tokens is not None and usage.input_tokens is not None else None),
            "output_tokens": (max(0, budget.max_output_tokens - usage.output_tokens) if budget.max_output_tokens is not None and usage.output_tokens is not None else None),
        }

    def _reserve_budget_tx(
        self,
        task_id: str,
        resource: BudgetResource | str,
        amount: float,
        now: str,
        *,
        criticality: ModelCallCriticality = ModelCallCriticality.REQUIRED,
        purpose: ModelCallPurpose | None = None,
    ) -> BudgetReservationResult:
        res_key = str(getattr(resource, "value", resource))
        if res_key not in _RESOURCE_MAP:
            raise TaskValidationError(f"unknown budget resource: {res_key}")
        limit_col, usage_col = _RESOURCE_MAP[res_key]
        row = self.conn.execute(
            f"SELECT b.{limit_col}, u.{usage_col} FROM task_budgets b "
            f"JOIN task_budget_usages u ON b.task_id = u.task_id WHERE b.task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"task budget not found for task {task_id}")
        limit_val = row[0]
        current_val = row[1] or 0.0

        # M32: Budget-aware starvation prevention for optional model calls
        if (
            limit_val is not None
            and res_key == BudgetResource.MODEL_CALLS.value
            and criticality == ModelCallCriticality.OPTIONAL
        ):
            pending_steps = self.count_pending_steps(task_id)
            contract_row = self.conn.execute(
                "SELECT contract_id FROM task_goal_contracts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            headroom = float(pending_steps + (1 if contract_row is not None else 0))
            if (current_val + amount + headroom) > limit_val:
                return BudgetReservationResult(
                    decision=BudgetDecision.EXHAUSTED,
                    resource=BudgetResource.MODEL_CALLS,
                    amount=amount,
                    current_usage=float(current_val),
                    limit_value=float(limit_val),
                    reason="budget_starvation_prevention",
                )

        if limit_val is not None and (current_val + amount) > limit_val:
            return BudgetReservationResult(
                decision=BudgetDecision.EXHAUSTED,
                resource=BudgetResource(res_key) if res_key in BudgetResource._value2member_map_ else BudgetResource.MODEL_CALLS,
                amount=amount,
                current_usage=float(current_val),
                limit_value=float(limit_val),
                reason=str(_EXHAUSTION_REASONS.get(res_key, BudgetExhaustionReason.MODEL_CALL_LIMIT).value),
            )
        new_usage = current_val + amount
        self.conn.execute(
            f"UPDATE task_budget_usages SET {usage_col} = ?, updated_at = ? WHERE task_id = ?",
            (new_usage, now, task_id),
        )
        return BudgetReservationResult(
            decision=BudgetDecision.ALLOW,
            resource=BudgetResource(res_key) if res_key in BudgetResource._value2member_map_ else BudgetResource.MODEL_CALLS,
            amount=amount,
            current_usage=float(new_usage),
            limit_value=float(limit_val) if limit_val is not None else float("inf"),
        )

    def reserve_budget(
        self,
        task_id: str,
        resource: BudgetResource | str,
        amount: float = 1.0,
        *,
        criticality: ModelCallCriticality = ModelCallCriticality.REQUIRED,
        purpose: ModelCallPurpose | None = None,
    ) -> BudgetReservationResult:
        now = _now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                res = self._reserve_budget_tx(
                    task_id, resource, amount, now, criticality=criticality, purpose=purpose
                )
                self.conn.commit()
                return res
            except Exception:
                self.conn.rollback()
                raise

    def _record_budget_consumption_tx(
        self, task_id: str, resource: BudgetResource | str, amount: float, now: str
    ) -> None:
        if amount <= 0:
            return
        res_key = str(getattr(resource, "value", resource))
        if res_key not in _RESOURCE_MAP:
            return
        _, usage_col = _RESOURCE_MAP[res_key]
        row = self.conn.execute(
            f"SELECT {usage_col} FROM task_budget_usages WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is not None:
            current_val = row[0] or 0.0
            new_val = current_val + amount
            self.conn.execute(
                f"UPDATE task_budget_usages SET {usage_col} = ?, updated_at = ? WHERE task_id = ?",
                (new_val, now, task_id),
            )

    def record_budget_consumption(
        self, task_id: str, resource: BudgetResource | str, amount: float, **extra
    ) -> None:
        now = _now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self._record_budget_consumption_tx(task_id, resource, amount, now)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def extend_budget(
        self,
        task_id: str,
        *,
        actor: str = "human",
        note: str = "",
        reason: str = "",
        increments: dict[str, Any] | None = None,
        **extra_increments: Any,
    ) -> TaskBudget:
        allowed = {
            "model_calls": "max_model_calls",
            "max_model_calls": "max_model_calls",
            "tool_calls": "max_tool_calls",
            "max_tool_calls": "max_tool_calls",
            "steps": "max_steps",
            "max_steps": "max_steps",
            "replans": "max_replans",
            "max_replans": "max_replans",
            "verification_calls": "max_verification_calls",
            "max_verification_calls": "max_verification_calls",
            "retries": "max_retries",
            "max_retries": "max_retries",
            "command_runtime": "max_command_runtime_seconds",
            "command_runtime_seconds": "max_command_runtime_seconds",
            "max_command_runtime_seconds": "max_command_runtime_seconds",
            "active_runtime": "max_active_runtime_seconds",
            "active_runtime_seconds": "max_active_runtime_seconds",
            "max_active_runtime_seconds": "max_active_runtime_seconds",
            "input_tokens": "max_input_tokens",
            "max_input_tokens": "max_input_tokens",
            "output_tokens": "max_output_tokens",
            "max_output_tokens": "max_output_tokens",
        }
        all_increments = dict(increments or {})
        all_increments.update(extra_increments)
        actual_note = note or reason
        updates: dict[str, tuple[str, float]] = {}
        for key, value in all_increments.items():
            if key not in allowed:
                raise TaskValidationError(f"cannot extend unknown budget resource: {key}")
            val = float(value)
            if val <= 0:
                raise TaskValidationError(f"extension for {key} must be positive, got {value}")
            target_col = allowed[key]
            updates[target_col] = (target_col, val)

        if not updates:
            raise TaskValidationError("at least one budget resource extension must be specified")

        now = _now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                current_budget = self.get_task_budget(task_id)
                for col, (_, inc) in updates.items():
                    prev_limit = getattr(current_budget, col)
                    prev_val = float(prev_limit) if prev_limit is not None else 0.0
                    new_val = prev_val + inc
                    self.conn.execute(
                        f"UPDATE task_budgets SET {col} = ?, updated_at = ? WHERE task_id = ?",
                        (int(new_val) if "runtime" not in col else new_val, now, task_id),
                    )
                    alloc_id = f"alloc_{uuid4().hex[:12]}"
                    self.conn.execute(
                        """INSERT INTO task_budget_allocations
                           (allocation_id, task_id, action, resource, amount, previous_limit,
                            new_limit, actor, note, created_at)
                           VALUES (?, ?, 'extend', ?, ?, ?, ?, ?, ?, ?)""",
                        (alloc_id, task_id, col, inc, prev_val, new_val, actor, actual_note, now),
                    )

                # Unblock only if task was blocked for budget and no step requires human recovery
                if task.status is TaskStatus.BLOCKED:
                    has_blocked_step = bool(
                        self.conn.execute(
                            "SELECT 1 FROM task_steps WHERE task_id = ? AND status = 'blocked'",
                            (task_id,),
                        ).fetchone()
                    )
                    if not has_blocked_step:
                        remaining = self.get_budget_remaining(task_id)
                        all_positive = all(
                            v is None or v > 0 for k, v in remaining.items()
                            if k in {"model_calls", "tool_calls", "steps", "replans", "verification_calls", "retries", "command_runtime_seconds", "active_runtime_seconds"}
                        )
                        if all_positive:
                            self.conn.execute(
                                "UPDATE tasks SET status='running', updated_at=? WHERE task_id=?",
                                (now, task_id),
                            )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task_budget(task_id)

    def block_task_budget_exhausted(
        self, task_id: str, resource: str, current: float, limit: float
    ) -> Task:
        summary = f"budget_exhausted:{resource}"
        return self.block_task(task_id, summary=summary)

    def list_budget_allocations(self, task_id: str) -> list[BudgetAllocation]:
        rows = self.conn.execute(
            """SELECT allocation_id, task_id, action, resource, amount, previous_limit,
                      new_limit, actor, note, created_at
               FROM task_budget_allocations WHERE task_id = ? ORDER BY created_at ASC""",
            (task_id,),
        ).fetchall()
        return [
            BudgetAllocation(
                allocation_id=r[0],
                task_id=r[1],
                action=r[2],
                resource=r[3],
                amount=float(r[4]),
                previous_limit=float(r[5]),
                new_limit=float(r[6]),
                actor=r[7],
                note=r[8],
                created_at=r[9],
            )
            for r in rows
        ]

    def record_goal_verification(
        self,
        task_id: str,
        verification: TaskGoalVerification,
    ) -> TaskGoalVerification:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                now = _now()
                ver_id = verification.verification_id or f"ver_{uuid4().hex[:12]}"
                created_at = verification.created_at or now
                self.conn.execute(
                    """INSERT INTO task_goal_verifications
                       (verification_id, task_id, status, summary, plan_revision_number, evidence_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ver_id,
                        task_id,
                        verification.status.value,
                        verification.summary,
                        verification.plan_revision_number,
                        verification.evidence_hash,
                        created_at,
                    ),
                )
                for cr in verification.criterion_results:
                    res_id = f"cres_{uuid4().hex[:12]}"
                    self.conn.execute(
                        """INSERT INTO task_goal_criterion_results
                           (result_id, verification_id, criterion_id, status, evidence_summary, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            res_id,
                            ver_id,
                            cr.criterion_id,
                            cr.status.value,
                            cr.evidence_summary,
                            created_at,
                        ),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        latest = self.get_latest_goal_verification(task_id)
        if latest is None:
            raise GoalVerificationError(f"failed to retrieve persisted verification for task {task_id}")
        return latest

    def get_latest_goal_verification(self, task_id: str) -> TaskGoalVerification | None:
        row = self.conn.execute(
            """SELECT verification_id, task_id, status, summary, plan_revision_number, evidence_hash, created_at
               FROM task_goal_verifications
               WHERE task_id=?
               ORDER BY rowid DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        ver_id, t_id, status_str, summary, rev_num, ev_hash, created_at = row
        c_rows = self.conn.execute(
            """SELECT criterion_id, status, evidence_summary
               FROM task_goal_criterion_results
               WHERE verification_id=? ORDER BY rowid ASC""",
            (ver_id,),
        ).fetchall()
        c_results = tuple(
            CriterionResult(
                criterion_id=cr[0],
                status=CriterionStatus(cr[1]),
                evidence_summary=cr[2],
            )
            for cr in c_rows
        )
        return TaskGoalVerification(
            verification_id=ver_id,
            task_id=t_id,
            status=GoalVerificationStatus(status_str),
            criterion_results=c_results,
            summary=summary,
            plan_revision_number=rev_num,
            evidence_hash=ev_hash,
            created_at=created_at,
        )

    def list_goal_verifications(self, task_id: str) -> list[TaskGoalVerification]:
        rows = self.conn.execute(
            """SELECT verification_id, task_id, status, summary, plan_revision_number, evidence_hash, created_at
               FROM task_goal_verifications
               WHERE task_id=?
               ORDER BY rowid ASC""",
            (task_id,),
        ).fetchall()
        results: list[TaskGoalVerification] = []
        for row in rows:
            ver_id, t_id, status_str, summary, rev_num, ev_hash, created_at = row
            c_rows = self.conn.execute(
                """SELECT criterion_id, status, evidence_summary
                   FROM task_goal_criterion_results
                   WHERE verification_id=? ORDER BY rowid ASC""",
                (ver_id,),
            ).fetchall()
            c_results = tuple(
                CriterionResult(
                    criterion_id=cr[0],
                    status=CriterionStatus(cr[1]),
                    evidence_summary=cr[2],
                )
                for cr in c_rows
            )
            results.append(
                TaskGoalVerification(
                    verification_id=ver_id,
                    task_id=t_id,
                    status=GoalVerificationStatus(status_str),
                    criterion_results=c_results,
                    summary=summary,
                    plan_revision_number=rev_num,
                    evidence_hash=ev_hash,
                    created_at=created_at,
                )
            )
        return results

    def complete_task(self, task_id: str) -> Task:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                self._require_task_transition(task.status, TaskStatus.COMPLETED)
                now = _now()
                self.conn.execute(
                    """UPDATE tasks SET status='completed', current_step_id=NULL,
                           updated_at=?, completed_at=? WHERE task_id=?""",
                    (now, now, task_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(task_id)

    def fail_task(self, task_id: str, summary: str = "") -> Task:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                self._require_task_transition(task.status, TaskStatus.FAILED)
                now = _now()
                self.conn.execute(
                    """UPDATE tasks SET status='failed', current_step_id=NULL,
                           updated_at=?, completed_at=? WHERE task_id=?""",
                    (now, now, task_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(task_id)

    def reconcile_task_completion(self, task_id: str) -> Task:
        with self._lock:
            task = self.get_task(task_id)
            if task.status is TaskStatus.RUNNING:
                latest_ver = self.get_latest_goal_verification(task_id)
                remaining = self.count_remaining_steps(task_id)
                if remaining == 0 and latest_ver is not None and latest_ver.status is GoalVerificationStatus.PASS:
                    now = _now()
                    self.conn.execute(
                        """UPDATE tasks SET status='completed', current_step_id=NULL,
                               updated_at=?, completed_at=? WHERE task_id=?""",
                        (now, now, task_id),
                    )
                    self.conn.commit()
                    return self.get_task(task_id)
            return task

    def count_remaining_steps(self, task_id: str) -> int:
        return int(
            self.conn.execute(
                """SELECT COUNT(*) FROM task_steps
                   WHERE task_id=? AND status IN ('pending', 'running')""",
                (task_id,),
            ).fetchone()[0]
        )

    def count_pending_steps(self, task_id: str) -> int:
        return int(
            self.conn.execute(
                """SELECT COUNT(*) FROM task_steps
                   WHERE task_id=? AND status = 'pending'""",
                (task_id,),
            ).fetchone()[0]
        )

    def get_task_by_source(self, source: str, source_id: str) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE source=? AND source_id=?",
            (str(source), str(source_id)),
        ).fetchone()
        return self._task(row) if row is not None else None

    def get_task(self, task_id: str) -> Task:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    def get_step(self, step_id: str) -> TaskStep:
        row = self.conn.execute(
            "SELECT * FROM task_steps WHERE step_id=?", (step_id,)
        ).fetchone()
        if row is None:
            raise KeyError(step_id)
        return self._step(row)

    def list_steps(self, task_id: str) -> list[TaskStep]:
        rows = self.conn.execute(
            "SELECT * FROM task_steps WHERE task_id=? ORDER BY position", (task_id,)
        ).fetchall()
        return [self._step(row) for row in rows]

    def list_tasks(self, *, status: TaskStatus | None = None, limit: int = 50) -> list[Task]:
        bounded = max(1, min(int(limit), 500))
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM tasks WHERE status=?
                   ORDER BY created_at DESC, task_id DESC LIMIT ?""",
                (status.value, bounded),
            ).fetchall()
        return [self._task(row) for row in rows]

    @staticmethod
    def _require_task_transition(before: TaskStatus, after: TaskStatus) -> None:
        if after not in TASK_TRANSITIONS[before]:
            raise TaskStateError(f"illegal task transition: {before.value} -> {after.value}")

    @staticmethod
    def _require_step_transition(before: StepStatus, after: StepStatus) -> None:
        if after not in STEP_TRANSITIONS[before]:
            raise TaskStateError(f"illegal step transition: {before.value} -> {after.value}")

    def transition_task(self, task_id: str, status: TaskStatus) -> Task:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                current = self.get_task(task_id)
                self._require_task_transition(current.status, status)
                now = _now()
                completed = now if status is TaskStatus.COMPLETED else current.completed_at
                cursor = self.conn.execute(
                    """UPDATE tasks SET status=?, updated_at=?, completed_at=?
                       WHERE task_id=? AND status=?""",
                    (status.value, now, completed, task_id, current.status.value),
                )
                if cursor.rowcount != 1:
                    raise TaskStateError("task changed during transition")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(task_id)

    def claim_next_step(self, task_id: str) -> StepClaim:
        """Atomically move exactly one pending step to running."""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                if task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    self.conn.commit()
                    return StepClaim(StepClaimOutcome.TERMINAL, task, code="task_terminal")
                if task.status is TaskStatus.BLOCKED:
                    self.conn.commit()
                    return StepClaim(StepClaimOutcome.BLOCKED, task, code="task_blocked")
                running_row = self.conn.execute(
                    """SELECT * FROM task_steps WHERE task_id=? AND status='running'
                       ORDER BY position LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if running_row is not None:
                    self.conn.commit()
                    return StepClaim(
                        StepClaimOutcome.IN_PROGRESS,
                        task,
                        self._step(running_row),
                        "task_step_in_progress",
                    )
                row = self.conn.execute(
                    """SELECT * FROM task_steps WHERE task_id=? AND status='pending'
                       ORDER BY position LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if row is None:
                    latest_ver = self.get_latest_goal_verification(task_id)
                    if (
                        task.status is TaskStatus.RUNNING
                        and latest_ver is not None
                        and latest_ver.status is GoalVerificationStatus.PASS
                    ):
                        now = _now()
                        self.conn.execute(
                            """UPDATE tasks SET status='completed', current_step_id=NULL,
                                   updated_at=?, completed_at=? WHERE task_id=?""",
                            (now, now, task_id),
                        )
                        task = self.get_task(task_id)
                    self.conn.commit()
                    return StepClaim(
                        StepClaimOutcome.NO_PENDING_STEP, task, code="no_pending_step"
                    )
                step = self._step(row)
                if step.attempt_count >= step.max_attempts:
                    raise TaskStateError("step attempt limit reached")
                now = _now()
                b_res = self._reserve_budget_tx(task_id, BudgetResource.STEPS, 1.0, now)
                if not b_res.allowed:
                    self.conn.execute(
                        "UPDATE tasks SET status='blocked', current_step_id=NULL, updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                    self.conn.commit()
                    return StepClaim(
                        StepClaimOutcome.BLOCKED,
                        self.get_task(task_id),
                        code="budget_exhausted:steps",
                    )
                self._require_step_transition(step.status, StepStatus.RUNNING)
                if task.status in {TaskStatus.PLANNED, TaskStatus.PAUSED}:
                    self._require_task_transition(task.status, TaskStatus.RUNNING)
                cursor = self.conn.execute(
                    """UPDATE task_steps
                       SET status='running', attempt_count=attempt_count+1,
                            started_at=COALESCE(started_at, ?), updated_at=?
                       WHERE step_id=? AND status='pending'""",
                    (now, now, step.step_id),
                )
                if cursor.rowcount != 1:
                    raise TaskStateError("step changed during claim")
                self.conn.execute(
                    """UPDATE tasks SET status='running', current_step_id=?, updated_at=?
                       WHERE task_id=?""",
                    (step.step_id, now, task_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return StepClaim(
            StepClaimOutcome.CLAIMED,
            self.get_task(task_id),
            self.get_step(step.step_id),
            "task_step_claimed",
        )

    def attach_run_id(self, step_id: str, run_id: str) -> TaskStep:
        if not run_id:
            return self.get_step(step_id)
        with self._lock:
            cursor = self.conn.execute(
                """UPDATE task_steps SET execution_run_id=?, updated_at=?
                   WHERE step_id=? AND status='running'""",
                (str(run_id)[:160], _now(), step_id),
            )
            if cursor.rowcount != 1:
                self.conn.rollback()
                raise TaskStateError("run ID can only attach to a running step")
            self.conn.commit()
        return self.get_step(step_id)

    def is_step_stale(self, step: TaskStep) -> bool:
        """A running step is only orphan-recovered after a conservative bound."""
        if step.status is not StepStatus.RUNNING:
            return False
        try:
            updated = datetime.fromisoformat(step.updated_at)
        except ValueError:
            return True
        return datetime.now(UTC) - updated >= timedelta(
            seconds=self.limits.running_step_stale_after_seconds
        )

    def finish_step(
        self,
        step_id: str,
        *,
        result: str,
        verification: VerificationResult,
        auto_complete: bool = True,
        task_status: TaskStatus | None = None,
    ) -> tuple[Task, TaskStep]:
        target = {
            VerificationStatus.PASS: StepStatus.SUCCEEDED,
            VerificationStatus.FAIL: StepStatus.FAILED,
            VerificationStatus.BLOCKED: StepStatus.BLOCKED,
            VerificationStatus.UNKNOWN: StepStatus.BLOCKED,
            VerificationStatus.SKIPPED: StepStatus.SKIPPED,
        }[verification.status]
        safe_result, result_size, truncated = _bounded_safe(
            result, self.limits.max_result_bytes, field="step result", truncate=True
        )
        summary, _, _ = _bounded_safe(
            verification.summary,
            self.limits.max_verification_summary_bytes,
            field="verification summary",
            truncate=True,
        )
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                step = self.get_step(step_id)
                self._require_step_transition(step.status, target)
                now = _now()
                cursor = self.conn.execute(
                    """UPDATE task_steps
                       SET status=?, result=?, result_size=?, result_truncated=?,
                           verification_status=?, verification_summary=?,
                           completed_at=?, updated_at=?
                       WHERE step_id=? AND status='running'""",
                    (
                        target.value,
                        safe_result,
                        result_size,
                        int(truncated),
                        verification.status.value,
                        summary,
                        now,
                        now,
                        step_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskStateError("step changed during checkpoint")
                task = self.get_task(step.task_id)
                next_status = task.status
                completed_at = task.completed_at
                if task.status is not TaskStatus.CANCELLED:
                    if task_status is not None:
                        next_status = task_status
                    elif target is StepStatus.SUCCEEDED or target is StepStatus.SKIPPED:
                        remaining = self.conn.execute(
                            """SELECT COUNT(*) FROM task_steps
                               WHERE task_id=? AND status IN ('pending', 'running')""",
                            (task.task_id,),
                        ).fetchone()[0]
                        if auto_complete and int(remaining) == 0:
                            next_status = TaskStatus.COMPLETED
                            completed_at = now
                        else:
                            next_status = TaskStatus.RUNNING
                    elif target is StepStatus.FAILED:
                        next_status = TaskStatus.FAILED
                    else:
                        next_status = TaskStatus.BLOCKED
                    if next_status is not task.status:
                        self._require_task_transition(task.status, next_status)
                self.conn.execute(
                    """UPDATE tasks SET status=?, current_step_id=NULL,
                           updated_at=?, completed_at=? WHERE task_id=?""",
                    (next_status.value, now, completed_at, task.task_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(step.task_id), self.get_step(step_id)

    def block_running_step(self, task_id: str, summary: str) -> tuple[Task, TaskStep]:
        row = self.conn.execute(
            """SELECT step_id FROM task_steps WHERE task_id=? AND status='running'
               ORDER BY position LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskStateError("task has no running step to recover")
        return self.finish_step(
            row["step_id"],
            result="",
            verification=VerificationResult(VerificationStatus.BLOCKED, summary),
        )

    def prepare_blocked_step_retry(self, task_id: str) -> tuple[Task, TaskStep]:
        """Make one blocked step runnable; this method never executes the step."""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                if task.status is not TaskStatus.BLOCKED:
                    raise TaskStateError("only a blocked task can be recovered")
                row = self.conn.execute(
                    """SELECT * FROM task_steps WHERE task_id=? AND status='blocked'
                       ORDER BY position LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise TaskStateError("task has no blocked step")
                step = self._step(row)
                self._require_step_transition(step.status, StepStatus.PENDING)
                self._require_task_transition(task.status, TaskStatus.RUNNING)
                now = _now()
                cursor = self.conn.execute(
                    """UPDATE task_steps
                       SET status='pending', max_attempts=MAX(max_attempts, attempt_count+1),
                           verification_status=NULL, verification_summary=NULL,
                           completed_at=NULL, updated_at=?
                       WHERE step_id=? AND status='blocked'""",
                    (now, step.step_id),
                )
                if cursor.rowcount != 1:
                    raise TaskStateError("blocked step changed during recovery")
                self.conn.execute(
                    """UPDATE tasks SET status='running', current_step_id=NULL,
                           completed_at=NULL, updated_at=?
                       WHERE task_id=? AND status='blocked'""",
                    (now, task_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(task_id), self.get_step(step.step_id)

    def complete_blocked_step_after_verification(
        self,
        step_id: str,
        *,
        result: str,
        verification: VerificationResult,
    ) -> tuple[Task, TaskStep]:
        """Checkpoint human-reconciled evidence only after read-only verification passes."""
        if verification.status is not VerificationStatus.PASS:
            raise TaskStateError("recovery verification did not pass")
        safe_result, result_size, truncated = _bounded_safe(
            result, self.limits.max_result_bytes, field="step result", truncate=True
        )
        summary, _, _ = _bounded_safe(
            verification.summary,
            self.limits.max_verification_summary_bytes,
            field="verification summary",
            truncate=True,
        )
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                step = self.get_step(step_id)
                task = self.get_task(step.task_id)
                if task.status is not TaskStatus.BLOCKED:
                    raise TaskStateError("task is no longer blocked")
                self._require_step_transition(step.status, StepStatus.SUCCEEDED)
                now = _now()
                cursor = self.conn.execute(
                    """UPDATE task_steps
                       SET status='succeeded', result=?, result_size=?, result_truncated=?,
                           verification_status='pass', verification_summary=?,
                           completed_at=?, updated_at=?
                       WHERE step_id=? AND status='blocked'""",
                    (
                        safe_result,
                        result_size,
                        int(truncated),
                        summary,
                        now,
                        now,
                        step_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskStateError("blocked step changed during recovery")
                remaining = int(
                    self.conn.execute(
                        """SELECT COUNT(*) FROM task_steps
                           WHERE task_id=? AND status NOT IN ('succeeded', 'skipped', 'superseded')""",
                        (task.task_id,),
                    ).fetchone()[0]
                )
                target = TaskStatus.COMPLETED if remaining == 0 else TaskStatus.RUNNING
                self._require_task_transition(task.status, target)
                self.conn.execute(
                    """UPDATE tasks SET status=?, current_step_id=NULL, updated_at=?,
                           completed_at=? WHERE task_id=? AND status='blocked'""",
                    (
                        target.value,
                        now,
                        now if target is TaskStatus.COMPLETED else None,
                        task.task_id,
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(task.task_id), self.get_step(step_id)

    def cancel(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task.status is TaskStatus.CANCELLED:
            return task
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise TaskStateError(f"cannot cancel {task.status.value} task")
        return self.transition_task(task_id, TaskStatus.CANCELLED)

    def list_revisions(self, task_id: str) -> list[TaskPlanRevision]:
        rows = self.conn.execute(
            "SELECT * FROM task_plan_revisions WHERE task_id=? ORDER BY revision_number",
            (task_id,),
        ).fetchall()
        if not rows:
            try:
                task = self.get_task(task_id)
                return [
                    TaskPlanRevision(
                        revision_id=f"rev_{task_id}_0",
                        task_id=task_id,
                        revision_number=0,
                        reason="Initial plan",
                        created_at=task.created_at,
                        trigger_step_id=None,
                    )
                ]
            except KeyError:
                return []
        return [self._revision(row) for row in rows]

    def get_revision(self, revision_id: str) -> TaskPlanRevision:
        row = self.conn.execute(
            "SELECT * FROM task_plan_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        return self._revision(row)

    def get_revision_by_trigger(
        self, task_id: str, trigger_step_id: str
    ) -> TaskPlanRevision | None:
        row = self.conn.execute(
            "SELECT * FROM task_plan_revisions WHERE task_id=? AND trigger_step_id=?",
            (task_id, trigger_step_id),
        ).fetchone()
        return self._revision(row) if row is not None else None

    def count_revisions(self, task_id: str, *, exclude_initial: bool = True) -> int:
        if exclude_initial:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM task_plan_revisions WHERE task_id=? AND revision_number > 0",
                (task_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM task_plan_revisions WHERE task_id=?", (task_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def block_task(self, task_id: str, summary: str = "") -> Task:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                if task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    self.conn.commit()
                    return task
                if task.status is not TaskStatus.BLOCKED:
                    self._require_task_transition(task.status, TaskStatus.BLOCKED)
                now = _now()
                self.conn.execute(
                    "UPDATE tasks SET status='blocked', current_step_id=NULL, updated_at=? WHERE task_id=?",
                    (now, task_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_task(task_id)

    def apply_plan_revision(
        self,
        task_id: str,
        *,
        trigger_step_id: str,
        reason: str,
        remaining_steps: list[PlanStep],
    ) -> tuple[Task, TaskPlanRevision, list[TaskStep], list[TaskStep]]:
        """Atomically supersede pending steps and insert replacement steps for a new revision."""
        safe_reason, _, _ = _bounded_safe(
            reason, 1024, field="replan reason", truncate=False
        )
        if not safe_reason:
            raise TaskValidationError("replan reason must not be empty")
        if not remaining_steps:
            raise PlanValidationError("replacement plan must contain at least 1 step")

        prepared: list[tuple[str, str, str, str | None, str]] = []
        for index, item in enumerate(remaining_steps, 1):
            title, _, _ = _bounded_safe(
                item.title,
                self.limits.max_title_bytes,
                field=f"replacement step {index} title",
                truncate=False,
            )
            instruction, _, _ = _bounded_safe(
                item.instruction,
                self.limits.max_instruction_bytes,
                field=f"replacement step {index} instruction",
                truncate=False,
            )
            verification, _, _ = _bounded_safe(
                item.verification,
                self.limits.max_instruction_bytes,
                field=f"replacement step {index} verification",
                truncate=False,
            )
            if not title or not instruction or not verification:
                raise PlanValidationError(f"replacement step {index} fields must not be empty")
            exec_k = item.execution_kind.value if item.execution_kind else None
            reqs_data = [
                {"kind": req.kind, "description": req.description, "required": req.required}
                for req in (item.evidence_requirements or ())
            ]
            prepared.append(
                (title, instruction, verification, exec_k, json.dumps(reqs_data, ensure_ascii=False))
            )

        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                task = self.get_task(task_id)
                if task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    raise TaskStateError(f"cannot replan terminal task ({task.status.value})")

                trigger = self.get_step(trigger_step_id)
                if trigger.task_id != task_id:
                    raise TaskValidationError("trigger step does not belong to task")
                if trigger.status not in {StepStatus.SUCCEEDED, StepStatus.FAILED}:
                    raise TaskStateError(
                        f"trigger step must be succeeded or failed, got {trigger.status.value}"
                    )

                # Crash recovery & Idempotency check:
                existing = self.conn.execute(
                    "SELECT * FROM task_plan_revisions WHERE task_id=? AND trigger_step_id=?",
                    (task_id, trigger_step_id),
                ).fetchone()
                if existing is not None:
                    rev = self._revision(existing)
                    steps = self.list_steps(task_id)
                    new_steps = [s for s in steps if s.plan_revision_id == rev.revision_id]
                    superseded = [
                        s for s in steps if s.superseded_by_revision == rev.revision_id
                    ]
                    self.conn.commit()
                    return task, rev, new_steps, superseded

                # Replan limit check:
                task_budget = self.get_task_budget(task_id)
                replan_count = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM task_plan_revisions WHERE task_id=? AND revision_number > 0",
                        (task_id,),
                    ).fetchone()[0]
                )
                effective_max_replans = min(self.limits.max_replans_per_task, task_budget.max_replans)
                if replan_count >= effective_max_replans:
                    raise ReplanLimitExceededError(
                        f"task reached maximum of {effective_max_replans} replans"
                    )

                # Total durable task step bound check:
                existing_steps_count = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM task_steps WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                )
                total_steps = existing_steps_count + len(prepared)
                effective_max_steps = min(self.limits.max_steps_per_task, task_budget.max_steps)
                if total_steps > effective_max_steps:
                    raise PlanValidationError(
                        f"total durable task steps ({total_steps}) exceeds limit {effective_max_steps}"
                    )

                max_rev_row = self.conn.execute(
                    "SELECT MAX(revision_number) FROM task_plan_revisions WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                next_rev_num = (
                    int(max_rev_row[0]) if max_rev_row[0] is not None else 0
                ) + 1
                rev_id = f"rev_{uuid4().hex[:12]}"
                now = _now()

                # Insert revision record
                self.conn.execute(
                    """INSERT INTO task_plan_revisions
                       (revision_id, task_id, revision_number, reason, trigger_step_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (rev_id, task_id, next_rev_num, safe_reason, trigger_step_id, now),
                )
                self._record_budget_consumption_tx(task_id, BudgetResource.REPLANS, 1.0, now)

                # Query and supersede pending steps
                pending_rows = self.conn.execute(
                    "SELECT * FROM task_steps WHERE task_id=? AND status='pending' ORDER BY position",
                    (task_id,),
                ).fetchall()
                superseded_steps = [self._step(r) for r in pending_rows]

                self.conn.execute(
                    """UPDATE task_steps
                       SET status='superseded', superseded_by_revision=?, updated_at=?
                       WHERE task_id=? AND status='pending'""",
                    (rev_id, now, task_id),
                )

                # Append replacement steps
                max_pos = int(
                    self.conn.execute(
                        "SELECT MAX(position) FROM task_steps WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                    or 0
                )
                new_step_ids: list[str] = []
                for offset, (title, instruction, verification, exec_k, reqs_json) in enumerate(prepared, 1):
                    s_id = f"step_{uuid4().hex}"
                    new_step_ids.append(s_id)
                    self.conn.execute(
                        """INSERT INTO task_steps
                           (step_id, task_id, position, title, instruction,
                            verification_instruction, status, max_attempts, plan_revision_id,
                            execution_kind, evidence_requirements_json, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                        (
                            s_id,
                            task_id,
                            max_pos + offset,
                            title,
                            instruction,
                            verification,
                            self.limits.max_attempts_per_step,
                            rev_id,
                            exec_k,
                            reqs_json,
                            now,
                        ),
                    )

                if task.status is TaskStatus.BLOCKED:
                    self._require_task_transition(task.status, TaskStatus.RUNNING)
                    self.conn.execute(
                        "UPDATE tasks SET status='running', current_step_id=NULL, updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE tasks SET updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        updated_task = self.get_task(task_id)
        revision = self.get_revision(rev_id)
        new_steps = [self.get_step(sid) for sid in new_step_ids]
        return updated_task, revision, new_steps, superseded_steps

    def persist_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        """Persist an observable execution checkpoint record atomically."""
        with self._lock:
            exists_val = None
            if checkpoint.exists is not None:
                exists_val = 1 if checkpoint.exists else 0
            self.conn.execute(
                """INSERT OR REPLACE INTO task_execution_checkpoints
                   (checkpoint_id, task_id, step_id, kind, source, evidence_hash,
                    created_at, run_id, tool_name, exit_code, timed_out, duration_ms,
                    path, before_hash, after_hash, exists_flag, action_ledger_id,
                    consumed_by_verifier, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint.checkpoint_id,
                    checkpoint.task_id,
                    checkpoint.step_id,
                    checkpoint.kind,
                    checkpoint.source,
                    checkpoint.evidence_hash,
                    checkpoint.created_at,
                    checkpoint.run_id,
                    checkpoint.tool_name,
                    checkpoint.exit_code,
                    1 if checkpoint.timed_out else 0,
                    checkpoint.duration_ms,
                    checkpoint.path,
                    checkpoint.before_hash,
                    checkpoint.after_hash,
                    exists_val,
                    checkpoint.action_ledger_id,
                    1 if checkpoint.consumed_by_verifier else 0,
                    checkpoint.summary,
                ),
            )
            self.conn.commit()

    def list_checkpoints(
        self, task_id: str, step_id: str | None = None
    ) -> list[ExecutionCheckpoint]:
        """List persisted execution checkpoints for a task and optional step."""
        with self._lock:
            if step_id:
                rows = self.conn.execute(
                    """SELECT * FROM task_execution_checkpoints
                       WHERE task_id=? AND step_id=?
                       ORDER BY created_at ASC""",
                    (task_id, step_id),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT * FROM task_execution_checkpoints
                       WHERE task_id=?
                       ORDER BY created_at ASC""",
                    (task_id,),
                ).fetchall()
            result = []
            for r in rows:
                exists_val = None
                if r["exists_flag"] is not None:
                    exists_val = bool(r["exists_flag"])
                result.append(
                    ExecutionCheckpoint(
                        checkpoint_id=r["checkpoint_id"],
                        task_id=r["task_id"],
                        step_id=r["step_id"],
                        kind=r["kind"],
                        source=r["source"],
                        evidence_hash=r["evidence_hash"],
                        created_at=r["created_at"],
                        run_id=r["run_id"],
                        tool_name=r["tool_name"],
                        exit_code=r["exit_code"],
                        timed_out=bool(r["timed_out"]),
                        duration_ms=float(r["duration_ms"]),
                        path=r["path"],
                        before_hash=r["before_hash"],
                        after_hash=r["after_hash"],
                        exists=exists_val,
                        action_ledger_id=r["action_ledger_id"],
                        consumed_by_verifier=bool(r["consumed_by_verifier"]),
                        summary=r["summary"],
                    )
                )
            return result

    def mark_checkpoint_consumed(self, checkpoint_id: str) -> None:
        """Mark a checkpoint as having been consumed by a verifier."""
        with self._lock:
            self.conn.execute(
                """UPDATE task_execution_checkpoints
                   SET consumed_by_verifier=1
                   WHERE checkpoint_id=?""",
                (checkpoint_id,),
            )
            self.conn.commit()

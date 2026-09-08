# Resource & Execution Budget Governance (M25)

## Overview

Tieru's **Resource & Execution Budget Governance** provides strict, durable, and granular runtime accounting across all autonomous task executions. It ensures that unbounded autonomous agent loops, recursive replanning, excessive command execution, or runaway model queries are halted deterministically and safely before exhausting system resources or external API allowances.

---

## 1. Separation of Concerns & Core Invariants

Budget governance operates alongside existing Tieru sub-systems under strict architectural boundaries:

| Subsystem | Core Question | Architectural Boundary |
| :--- | :--- | :--- |
| **Trust Kernel** | *"Is this action permitted?"* | Budget availability **never** authorizes an action. A permission denial by Trust still consumes 1 tool-call budget to prevent infinite denial-retry loops. |
| **Action Ledger** | *"Was this action already performed?"* | Budget governance never interferes with idempotency deduplication. Extending a budget preserves all Action Ledger history. |
| **Goal Contract (M23)** | *"Did the task achieve its intended outcome?"* | Budget exhaustion is **never** goal success (PASS). When budget is exhausted, the task transitions to `TaskStatus.BLOCKED` (`budget_exhausted:<resource>`). |
| **Human Recovery (M17)** | *"How should an uncertain or failed step be resolved?"* | Budget extension **cannot** unblock a task if any step is in `StepStatus.BLOCKED`. Human inspection via `tieru recovery` remains mandatory. |

### Core Invariants

1. **No Self-Extension**: Models, planners, replanners, judges, and tools **cannot** alter or expand their own budgets. Only human operators via the CLI or external service APIs can allocate or extend budgets.
2. **Durable SQLite Persistence**: All limits, cumulative usages, and audit allocations are persisted transactionally in `task_budgets`, `task_budget_usages`, and `task_budget_allocations`.
3. **Atomic Reservation**: All discrete resources (`model_calls`, `tool_calls`, `steps`, `replans`, `verification_calls`, `retries`) are reserved using `BEGIN IMMEDIATE` transactions **prior** to invoking the external resource or executing the step.
4. **Crash-Loop Prevention**: Pre-execution reservation ensures that if an execution crashes or the host abruptly terminates, the consumed attempt is retained, preventing unbounded crash-restart cycles.
5. **Cumulative Command Runtime Bound**: Across all commands executed by a task, each command runner's timeout is clamped to `min(default_timeout, remaining_task_command_runtime)`. If remaining runtime is 0, execution is halted immediately.
6. **Active Autonomous Runtime Tracking**: Only actual execution time within the runner/verifier is counted toward active runtime. Wall-clock time spent while a task is `BLOCKED`, `PAUSED`, or idle between scheduler intervals is strictly excluded.
7. **Accurate Token Telemetry**: Input and output tokens are recorded only when the model provider supplies actual `usage` metadata. No artificial or volatile cost values are fabricated.
8. **Scheduled Task Isolation**: Each scheduled occurrence creates a distinct Durable Task with an independent Task Budget. Consuming budget on one occurrence has zero impact on subsequent runs.
9. **Budget Exhaustion Halting**: When a budget limit is reached, execution halts cleanly, transitions to `TaskStatus.BLOCKED`, and emits a structured `task_budget_exhausted` replay event. Automatic replanning is disallowed on budget exhaustion.

---

## 2. Managed Budget Resources

| Resource | Unit | Default Limit | Enforcement Mechanism |
| :--- | :--- | :--- | :--- |
| `max_model_calls` | Count | 20 | Pre-call atomic reservation in agent loop, synthesis, reviewer, and goal verifier. |
| `max_tool_calls` | Count | 30 | Pre-execution atomic reservation inside `execute()`. Consumed even if Trust denies permission. |
| `max_steps` | Count | 8 | Pre-execution reservation in `claim_next_step` and plan bound validation in `apply_plan_revision`. |
| `max_replans` | Count | 2 | Pre-revision check in `apply_plan_revision` and replan-guard in `TaskExecutor`. |
| `max_verification_calls` | Count | 10 | Atomic reservation in `ModelResultVerifier` and `ModelGoalJudge`. |
| `max_retries` | Count | 4 | Tracked on step retry preparation. |
| `max_command_runtime_seconds` | Seconds | 120.0s | Dynamic timeout clamping on shell/subprocesses; post-execution recording from duration telemetry. |
| `max_active_runtime_seconds` | Seconds | 600.0s | Measured via monotonic clock around runner execution; pre-execution limit verification. |
| `max_input_tokens` | Count | Optional | Provider-reported token recording in `task_budget_usages`. |
| `max_output_tokens` | Count | Optional | Provider-reported token recording in `task_budget_usages`. |

---

## 3. Database Schema

The additive migration in `tieru/tasks/store.py` defines three tables:

```sql
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
    previous_limit REAL,
    new_limit REAL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
```

---

## 4. CLI Usage

### Creating a Task with Custom Budget

Operators can specify custom budgets when creating a task:

```bash
tieru task create "Migrate database schema" \
    --max-model-calls 40 \
    --max-tool-calls 50 \
    --max-steps 15 \
    --max-active-runtime 1200
```

### Inspecting Task Budget & Usage

Viewing task details displays full budget limits, cumulative usage, remaining allowances, and extension audit log:

```bash
tieru task show task_20260902T100000_abcd1234
```

Output includes:
```text
Budget Governance:
  Model calls: 12 / 20 (8 remaining)
  Tool calls: 18 / 30 (12 remaining)
  Steps: 3 / 8 (5 remaining)
  Replans: 1 / 2 (1 remaining)
  Verification calls: 2 / 10 (8 remaining)
  Command runtime: 15.4s / 120.0s (104.6s remaining)
  Active runtime: 85.2s / 600.0s (514.8s remaining)
  Tokens: in=1420 out=380
```

Or structured as JSON:
```bash
tieru task show task_20260902T100000_abcd1234 --json
```

### Extending a Task Budget

When a task halts in `TaskStatus.BLOCKED` due to budget exhaustion (`budget_exhausted:<resource>`), an operator can grant additional budget:

```bash
tieru task budget-extend task_20260902T100000_abcd1234 \
    --model-calls 10 \
    --steps 3 \
    --active-runtime 300 \
    --reason "Approved extension for long-running data migration"
```

If the task was blocked solely due to budget exhaustion (and has no blocked step awaiting human recovery), it is automatically unblocked and marked runnable.

---

## 5. Reliability & Eval Scorecard Metrics

Resource governance metrics are integrated into the evaluation runner (`tieru/evals/metrics.py`):

- `budget_exhausted_rate`: Fraction of evaluated runs that halted on budget exhaustion.
- `average_model_calls`: Mean model calls per task.
- `average_tool_calls`: Mean tool calls per task.
- `average_active_runtime_seconds`: Mean active autonomous execution duration.
- `average_command_runtime_seconds`: Mean cumulative subprocess execution duration.

Failure classifications in `FailureType`:
- `MODEL_BUDGET_EXCEEDED`
- `TOOL_BUDGET_EXCEEDED`
- `STEP_BUDGET_EXCEEDED`
- `RUNTIME_BUDGET_EXCEEDED`
- `VERIFICATION_BUDGET_EXCEEDED`
- `RETRY_BUDGET_EXCEEDED`
- `REPLAN_BUDGET_EXCEEDED`

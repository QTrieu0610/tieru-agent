# Adaptive Planning & Replanning (M22)

Adaptive Planning & Replanning enables Tieru durable tasks to dynamically revise pending execution steps when observable evidence contradicts initial planning assumptions, while preserving full execution immutability, Trust Kernel authority, and strict resource bounds.

---

## Architectural Overview

Durable tasks in Tieru (M15) execute checkpoints sequentially with verification. In complex environments, early steps often produce discoveries that invalidate later steps: an assumed bug in authentication code turns out to be a database migration mismatch; an expected configuration key is missing; or an external resource is deprecated.

Without adaptive planning, an agent either fails deterministically on invalidated future steps or hallucinates workarounds. M22 introduces **bounded, evidence-driven plan review** at step boundaries.

```
                  ┌──────────────────────┐
                  │ Claim & Execute Step │
                  └──────────┬───────────┘
                             │
                  ┌──────────▼───────────┐
                  │ Layered Verification │
                  └──────────┬───────────┘
                             │ PASS (and task running)
                  ┌──────────▼───────────┐
                  │ Bounded Plan Review  │
                  └──────────┬───────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
      [ KEEP ]      [ REVISE_REMAINING ]     [ BLOCK ]
  Leave future         Supersede pending   Halt task safely;
  steps intact;       steps; append new;  require human or
  claim next step     claim next step     inspect blockers
```

---

## Review Trigger Lifecycle

Plan review is executed only under strict conditions:
1. **Successful Verification**: Review is evaluated only when the current step passes verification (`VerificationStatus.PASS`).
2. **Task Still Running**: If the completed step was the final step of the task, the task completes immediately; no review is triggered.
3. **Pending Steps Remain**: Review only evaluates future, pending work.
4. **Step Not Previously Triggered**: Crash-recovery deduplication ensures the same trigger step never triggers duplicate revisions.

Failed or blocked steps do not trigger replanning; they route through M15 checkpoint failure or M17 Human Recovery.

---

## Decision Types

The plan reviewer evaluates observable evidence and returns one of three structured decisions:

1. **`KEEP`**:
   - The current plan remains completely valid based on observed evidence.
   - Pending steps remain unmodified.
   - Execution proceeds directly to the next pending step.

2. **`REVISE_REMAINING`**:
   - Observable evidence demonstrates that the remaining pending steps are no longer valid or optimal.
   - The reviewer supplies structured replacement steps.
   - Pending steps are atomically marked `superseded`.
   - Replacement steps are appended sequentially (`position = max_pos + offset`).
   - Execution continues with the first replacement step.

3. **`BLOCK`**:
   - Observable evidence reveals an unresolvable contradiction or impossible requirement.
   - The task transitions to `TaskStatus.BLOCKED` with an explainable reason.
   - No unverified or unauthorized actions are taken.

---

## Invariants & Safety Guarantees

### 1. Completed History Immutability
Completed checkpoints (`succeeded`, `skipped`, `failed`, `blocked`) are completely immutable:
- Results, verification summaries, timestamps, execution run IDs, and plan revision IDs never change.
- Revisions only alter **future pending steps**.

### 2. Context Firewall (M19) Enforcement
Plan review prompts are strictly classified:
- System instructions are classified as `CONTROL` (reviewed instructions).
- Original user goal is classified as `USER` (untrusted user intent).
- Observable tool executions, results, and previous step summaries are classified as `DATA` (untrusted observable data).
Untrusted data from tool outputs cannot hijack the reviewer's decision schema.

### 3. Zero Tools for Plan Reviewer
The plan reviewer is strictly read-only and analytical:
- `tools=[]` is explicitly passed to the underlying model client.
- The reviewer cannot execute commands, write files, call external APIs, or alter runtime state directly.

### 4. Trust Kernel Authority Maintained
Replanning cannot bypass Trust:
- Replacement steps are executed through the standard `TieruStepRunner` and `TaskExecutor`.
- Every tool action proposed by a revised step must pass through the `TrustKernel` and `ToolRegistry`.
- Denied tools remain denied; dangerous actions cannot be smuggled through plan revisions.

### 5. Action Ledger & Human Recovery Preserved
- Uncertain executions (`tool_execution_uncertain`) in the Action Ledger halt the task immediately.
- Revisions cannot overwrite or bypass uncertain executions; M17 Human Recovery is required.

---

## Durability & SQLite Schema

Plan revisions are persisted transactionally in local SQLite.

### `task_plan_revisions` Table
```sql
CREATE TABLE IF NOT EXISTS task_plan_revisions (
    revision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    trigger_step_id TEXT REFERENCES task_steps(step_id),
    created_at TEXT NOT NULL,
    UNIQUE(task_id, revision_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_plan_revisions_trigger
    ON task_plan_revisions(task_id, trigger_step_id)
    WHERE trigger_step_id IS NOT NULL;
```

### `task_steps` Step Linkage
The `task_steps` table tracks revision provenance and supersession:
- `plan_revision_id TEXT`: ID of the revision that introduced this step (revision 0 for initial steps).
- `superseded_by_revision TEXT`: ID of the revision that superseded this pending step.
- `status`: Extended with `superseded` (`CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'blocked', 'skipped', 'superseded'))`).

### Atomic Replan Transaction
Applying a plan revision runs inside a single `BEGIN IMMEDIATE` SQLite transaction:
1. Validates that replacement steps are non-empty and bounded.
2. Checks for existing revisions from `trigger_step_id` (crash idempotency).
3. Verifies revision count does not exceed `max_replans_per_task`.
4. Updates pending steps to `status = 'superseded'` with `superseded_by_revision = revision_id`.
5. Inserts new replacement steps with incremented positions.
6. Inserts the new `task_plan_revisions` row.
7. Updates task `updated_at`.

---

## Hard Bounds & Resource Limits

To prevent runaway replanning loops, M22 enforces deterministic bounds:

| Limit | Default | Purpose |
|---|---|---|
| `max_replans_per_task` | `2` | Maximum allowable revisions per task before blocking. |
| `max_steps_per_task` | `8` | Total cumulative steps (completed + superseded + pending) allowed across all revisions. |
| `max_plan_steps` | `6` | Maximum steps allowed in any single revision. |
| `max_step_title_bytes` | `240` | Upper bound on step titles. |
| `max_step_instruction_bytes` | `1024` | Upper bound on step instructions. |
| `max_step_verification_bytes` | `1024` | Upper bound on verification requirements. |

When a replan exceeds `max_replans_per_task`, the task is transitioned to `TaskStatus.BLOCKED` with code `replan_limit_exceeded`.

---

## CLI Inspection

`tieru task show <task-id>` formats plan revisions and step states:

```bash
$ tieru task show task_20260902T120000_1234567890
Task: task_20260902T120000_1234567890
Status: completed
Goal: Fix authentication failure by inspecting handler and applying appropriate fix.

Plan revision 0
  ✓ 1. Inspect authentication handler
     Inspect auth handler implementation
  ~ 2. Modify authentication handler [superseded]
     Apply patch to handler

Plan revision 1
  ✓ 3. Apply database migration fix
     Fix migration mismatch in migration.sql
```

Step state symbols:
- `✓` Succeeded
- `~` Superseded
- `→` Running
- `✗` Failed
- `!` Blocked
- `-` Skipped
- ` ` Pending

Pass `--json` to inspect machine-readable revision records, including revision IDs, timestamps, trigger steps, and reasons.

---

## M21 Reliability Evaluation Integration

Adaptive replanning is fully integrated into the M21 agent reliability evaluation framework:

### New Metrics
- `replan_count`: Number of plan revisions created after initial planning.
- `replan_rate`: Proportion of tasks that underwent at least one replan.
- `replan_success_rate`: Proportion of replanned tasks that reached completion.
- `replan_limit_block_rate`: Proportion of tasks safely blocked by exceeding `max_replans_per_task`.
- `average_plan_revisions`: Average number of revisions (including revision 0) across tasks.

### Failure Taxonomy
- `FailureType.REPLANNING_ERROR`: General model or execution error during plan review.
- `FailureType.REPLAN_LIMIT_EXCEEDED`: Task blocked because revision count reached the hard bound.
- `FailureType.INVALID_PLAN_REVISION`: Reviewer produced invalid JSON, empty replacement steps, or violated step bounds.

### Standard Test Cases
- `replan-root-cause-001`: Root-cause discovery supersedes old plan and successfully completes.
- `replan-keep-valid-001`: Evidence validates initial plan; plan reviewer returns KEEP.
- `replan-constraint-preserved-001`: Replan respects immutable goal constraints (e.g. public API preserved).
- `replan-trust-denial-blocked-001`: Tool denied by Trust Kernel cannot be bypassed via replanning.
- `replan-limit-exceeded-001`: Repeated revisions hit bound; task stops safely in blocked state.

# Durable Tasks

Tieru Durable Tasks are a local persistent state machine for bounded work that must survive
application reconstruction. A task stores a goal and an ordered plan. Each explicitly invoked
execution claims one step, runs that step through the normal Tieru runtime, verifies observable
evidence, and checkpoints the result before another step can start.

Tieru Durable Tasks do not execute in the background. They are advanced only by an explicit API
or CLI `run`/`resume` command. Tieru does not claim distributed workflow execution.

## Task and step lifecycle

Tasks use explicit `planned`, `running`, `paused`, `blocked`, `failed`, `completed`, and
`cancelled` states. Steps use `pending`, `running`, `succeeded`, `failed`, `blocked`, and
`skipped`. Transition maps in the production task model reject illegal changes rather than
inferring state from nullable columns.

Task creation validates and redacts the goal and structured plan, then inserts the task and every
ordered step in one SQLite transaction. A task starts as `planned`; creation never runs the plan.
A task becomes `completed` only after all required steps are `succeeded` or explicitly `skipped`.
Verification failure produces `failed`; permission or ambiguous execution produces `blocked`.

Cancellation prevents all future claims and preserves completed checkpoints and external side
effects. It does not promise rollback.

## Planner

The planner uses Tieru's configured `small` ModelRouter role and receives no tools. Its only
accepted internal result is bounded JSON:

```json
{
  "steps": [
    {
      "title": "Inspect failure",
      "instruction": "Inspect the relevant code and failing test.",
      "verification": "Observable evidence identifies the failing behavior and files."
    }
  ]
}
```

Plans contain one to eight steps by default. Empty, oversized, malformed, or overlong plans are
rejected by the strict parser. The production model adapter can conservatively fall back to one
bounded step if model planning is unavailable; the same persistence validation still applies.
Planning never calls tools, requests Trust approval, writes files, or performs the goal.

## Execution and checkpointing

`TaskExecutor.run_next()` is the core primitive and executes at most one durable step. A bounded
service convenience method exists, but the default maximum per invocation is one. The executor:

1. atomically claims the earliest pending step;
2. marks the task and step `running`;
3. builds bounded context from the persisted goal, plan, prior checkpoint summaries, and current
   instruction;
4. runs a task-scoped normal Tieru turn;
5. stores its Replay run ID and bounded observable result;
6. verifies the result;
7. transactionally checkpoints the step and resulting task state.

Each task step uses a distinct `task:<task-id>:<step-id>` conversation label. This avoids mixing
mutable history from an unrelated interactive session while reusing configured Memory, models,
tools, Trust, Action Ledger, and Replay. Persisted task state contains observable results and
verification summaries, never hidden reasoning, full model messages, or unrestricted prompts.

SQLite `BEGIN IMMEDIATE` plus a conditional `pending -> running` update is the local concurrency
primitive. If another caller already owns a running step, the second receives
`task_step_in_progress` and does not execute the body.

M17 adds explicit recovery for a blocked step. Recovery may prepare a permission-denied or
human-confirmed-not-executed step as pending, but only a later explicit run executes it through
Trust and the Action Ledger. If the underlying action was human-confirmed completed, recovery calls
the existing read-only verifier with reconciliation evidence; only `pass` can checkpoint success.
Inconclusive verification and abandoned executions remain blocked. Recovery notes are untrusted
audit data and are never placed in task prompts.

## Verification

Every executed step receives an explicit `pass`, `fail`, `blocked`, `skipped`, or `unknown`
verification outcome. Deterministic tool results take precedence. Trust denial, M14 uncertainty,
an identical action still in progress, or a prior non-retryable action failure blocks the task.
Successful observable tool execution can pass deterministically. If no deterministic evidence
exists, an optional configured `judge` role receives only bounded observable data and no tools.
Unavailable or inconclusive verification becomes `unknown`, which blocks rather than passes.

The verifier never reruns a side effect.

## Resume and crash semantics

Resume uses the original task ID and persisted step rows. Succeeded steps are never selected
again; the next pending step is claimed. A `running` checkpoint is not reset to `pending`. A
recent running step is reported as in progress so a concurrent caller cannot steal it. Once its
bounded stale threshold is reached, resume marks it blocked for manual recovery because the
runtime cannot prove whether an external side effect occurred.

This favors at-most-once safety. A process crash after an external write but before task
checkpointing may require inspection of the Action Ledger and Replay rather than automatic
re-execution.

## Trust and Action Ledger

Task orchestration does not authorize or invoke tool functions. Its production runner calls the
existing Tieru turn, which reaches `ToolRegistry`, Trust, and the M14 Action Ledger in their
existing order:

```text
task step -> run_loop -> ToolRegistry -> Trust -> Action Ledger -> tool -> Replay
```

Permission denial blocks the step and task. Completed M14 fingerprints remain duplicate-suppressed.
Failed non-retryable and uncertain executions are not retried by the task layer. Task resume does
not bypass Trust or Action Ledger.

## Replay linkage and privacy

Every production step turn has a distinct Replay run ID stored in `task_steps.execution_run_id`.
Safe task events add task ID, step ID, position, lifecycle state, and verification state to that
run. They omit goals, instructions, context, results, prompts, and messages. Replay failure is
observability degradation only and cannot weaken task claims, Trust, or Action Ledger safety.

Goals, plan fields, results, verification summaries, source labels, and session labels are
redacted before persistence and bounded by byte limits. Results retain original size and
truncation metadata. API keys, authorization headers, subprocess environments, raw tool secrets,
chain-of-thought, and unrestricted external content are not task state.

## CLI

```text
tieru task create "goal"
tieru task list [--status planned] [--json]
tieru task show <task-id> [--json]
tieru task run <task-id> [--max-steps 1] [--json]
tieru task resume <task-id> [--max-steps 1] [--json]
tieru task cancel <task-id> [--json]
tieru task recover <task-id> --yes [--note "..."] [--json]
```

`create` plans and persists only. `show` provides the inspectable plan boundary. Execution is
foreground and explicit.

## Limitations

M15 itself is not a queue, scheduler, background worker, distributed lock, graph replacement, or
multi-agent framework. SQLite coordinates callers sharing one local state database; it does not
coordinate devices. Blocked task recovery and plan editing have no interactive UI in M15.
Task-level cancellation does not undo completed side effects. A crash before a Replay run ID can
be checkpointed may leave only the underlying Replay/Action Ledger evidence for manual recovery.

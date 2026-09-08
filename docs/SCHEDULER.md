# Tieru Scheduler

M18 adds a small persistent scheduler for local Tieru installations. It stores
schedule definitions and logical occurrences in the same SQLite `state.db` as
Durable Tasks, then advances work only when an operator or external timer runs
a bounded foreground tick.

Scheduling an action does not pre-authorize its future execution.

Scheduled tasks still pass through the current Trust Kernel policy.

Tieru does not claim distributed exactly-once scheduling.

Tieru Scheduler is local to processes sharing one SQLite state database.

Blocked or uncertain scheduled tasks are never automatically force-retried.

## Model and lifecycle

A schedule is an inspectable definition with a stable ID, name, goal, trigger,
IANA timezone, lifecycle status, next logical run time, and last logical run
time. M18 supports one-shot `--at` triggers, fixed elapsed `--every` intervals,
and explicit pause, resume, and cancel. Definitions are retained; there is no
delete command.

Every due logical time becomes a `schedule_runs` occurrence. SQLite enforces a
unique `(schedule_id, scheduled_for)` identity. A foreground tick claims that
identity under `BEGIN IMMEDIATE`, so concurrent processes sharing the database
cannot both create the same occurrence.

Each claimed occurrence materializes one fresh Durable Task with
`source=scheduled` and `source_id=<schedule_run_id>`. The unique task source
identity closes the crash window between creating a task and linking it back to
the occurrence. Execution then uses `TaskService.resume`; the scheduler does
not call a tool, model subprocess, or side-effecting operation directly.

## Time and recurrence

Persisted instants are timezone-aware ISO 8601 values normalized to UTC. The
schedule retains the explicit IANA timezone used to interpret naive input.
Python `zoneinfo` supplies timezone rules; `tzdata` makes those rules available
on Windows and minimal containers.

Intervals are elapsed-time recurrences. The next time is derived from the
previous logical time, not tick completion time, so delayed ticks do not drift.
M18 does not implement calendar rules such as "09:00 every weekday."

The default and only M18 misfire policy is `latest`: after downtime, a tick
coalesces missed times to the latest due occurrence and creates at most one
task. The default and only overlap policy is `forbid`: if an earlier occurrence
is claimed, running, or blocked, the due occurrence is recorded as
`skipped_overlap` and no second task is created.

## Crash and recovery behavior

- A claimed occurrence without a task is materialized on a later tick.
- A task created before its occurrence link is rediscovered through
  `(source, source_id)` and linked without creating another task.
- A terminal task whose occurrence was not reconciled is reconciled without
  running the task again.
- Running tasks retain Durable Task checkpoints. Orphaned or ambiguous work
  follows the existing blocked-task and Action Ledger recovery paths.

Recovery never grants permission. An explicit M17 recovery decision may make a
blocked task runnable or reconcile it as complete; a later tick observes that
durable state and continues through the normal task path.

## CLI

```text
tieru schedule create "Prepare daily engineering brief" --name daily-brief --at 2026-03-25T08:00:00+07:00 --timezone Asia/Saigon
tieru schedule create "Scan the ops inbox" --name ops-scan --every 6h --timezone UTC
tieru schedule list --json
tieru schedule show <schedule_id> --json
tieru schedule pause <schedule_id>
tieru schedule resume <schedule_id>
tieru schedule cancel <schedule_id>
tieru schedule runs <schedule_id> --json
tieru schedule tick --max-occurrences 10 --json
```

Tieru does not install a daemon. Users may invoke the bounded tick manually or
with an OS timer, cron, or supervisor. Those facilities only start the
foreground tick; authorization and durable work remain inside Tieru.

## Replay, privacy, and limitations

Replay records scheduler claims, materialization, overlap, blocks, advances,
and reconciliation with IDs, times, and statuses. Normalization omits goals,
instructions, prompts, results, and secrets.

M18 provides local single-database coordination, not a distributed queue,
cluster lease, cross-device lock, always-on worker, wall-clock calendar engine,
automatic rollback, or exactly-once external side effects. Action Ledger
suppresses known duplicate actions and preserves uncertainty, but external
systems without idempotency or reconciliation APIs can still be ambiguous.

# Human Recovery and Intervention

Tieru records an action as `uncertain` when it cannot prove whether a side effect completed. This
is intentionally fail-closed: repeating a message send, calendar creation, or other external write
could be worse than stopping. Tieru never automatically retries an uncertain external side effect.

M17 adds an explicit human recovery boundary. Inspection is read-only. A separate mutation command
records one of three precise decisions for an uncertain execution:

- `confirmed_completed`: the human verified that the side effect happened;
- `confirmed_not_executed`: the human verified that it did not happen;
- `abandoned`: Tieru must neither infer success nor retry it.

Human recovery does not bypass the Trust Kernel. Recovery never invokes a tool, changes Trust
policy, or treats a note as an instruction.

## Durable audit and execution reconciliation

`recovery_decisions` is append-only and protected against SQL update/delete by database triggers.
It stores a bounded redacted note, source, prior status, resolution, timestamp, and optional Replay
run. The execution row carries `completion_source` and `recovery_id`, so inspection distinguishes a
normal tool completion from human reconciliation without erasing the original ambiguity.

For `confirmed_completed`, the ledger becomes `completed` and no tool runs.
Human-confirmed completion is recorded as reconciliation evidence, not as an original tool result.
The stored result says explicitly that it is human confirmation; Tieru does not fabricate external
output.

For `abandoned`, the ledger remains non-executable and no retry permit exists. Healthy completed
records and non-retryable failures are not recovery targets in M17.

## Single-use manual retry

`confirmed_not_executed` leaves the reconciled provenance visible and creates a durable runtime-
owned permit. A manual retry authorization is single-use. There is no model-visible `force`,
`retry`, nonce, or idempotency-bypass argument.

The next ordinary call follows the complete path:

```text
model or task -> ToolRegistry -> Trust -> Action Ledger -> tool
```

Trust runs before the ledger claim. If policy now denies the action, Tieru does not consume the
permit. If Trust allows it, `ExecutionStore.claim()` uses `BEGIN IMMEDIATE` to consume the permit,
move the execution to `in_progress`, and increment its attempt count in one transaction. Concurrent
callers cannot both win. The winner executes once; later duplicates observe normal ledger state.

## Durable Task recovery and verification

`tieru task recover` operates on one blocked step and never runs it.

- A reconciled `confirmed_not_executed` execution makes the blocked step pending with one additional
  bounded attempt. A later explicit `task run` or `task resume` performs the normal Trust/ledger path.
- A Trust-denied step may likewise be prepared for an explicit retry, but policy is unchanged and a
  continuing denial blocks it again.
- A reconciled completed execution is passed as untrusted reconciliation evidence to the existing
  read-only verifier with no tools. Only a verification `pass` can mark that blocked step succeeded.
  Inconclusive verification leaves it blocked.
- An abandoned execution leaves the task blocked; the user may cancel the task explicitly.

Recovery notes are user-provided audit data. They are redacted and bounded before persistence and
are never injected as system instructions or used as authorization.

## Replay provenance

Explicit decisions use dedicated local Replay runs with safe events such as `recovery_opened`,
`recovery_decision`, `execution_reconciled`, and `manual_retry_authorized`. The later normal tool run
emits `manual_retry_consumed`. Task intervention emits `task_recovery_started`,
`task_recovery_completed`, or `task_recovery_blocked`. Events contain identifiers and state—not raw
arguments, notes, credentials, prompts, environments, or unbounded output.

## CLI

```text
tieru recovery list [--limit 50] [--json]
tieru recovery show <action-fingerprint> [--json]
tieru recovery resolve-execution <action-fingerprint> --resolution completed --yes [--note "..."]
tieru recovery resolve-execution <action-fingerprint> --resolution not-executed --yes [--note "..."]
tieru recovery resolve-execution <action-fingerprint> --resolution abandoned --yes [--note "..."]
tieru task recover <task-id> --yes [--note "..."] [--json]
```

`list` and `show` never mutate recovery state. Mutation requires a distinct command and `--yes`.
CLI recovery is foreground-only and survives application restart in the same `state.db`.

## Limitations

Tieru does not provide automatic rollback or distributed transaction recovery. M17 does not add a
saga engine, compensation, external-system reconciliation adapters, background workers, scheduling,
or cross-device coordination. SQLite provides atomicity only to processes sharing one local state
database. A single manual retry that itself becomes uncertain requires further operator inspection;
M17 does not create an automatic chain of retry permits.

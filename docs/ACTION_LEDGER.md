# Action Ledger

Tieru's Action Ledger prevents the same authorized, side-effecting tool action from being
executed more than once by the local runtime. It protects retries, repeated model tool calls,
and concurrent submissions that resolve to the same Trust action fingerprint.

## Responsibility boundary

Trust and idempotency answer different questions and execute in that order:

1. `ToolRegistry` validates the structured tool arguments and builds an `ActionRequest`.
2. `TrustKernel` answers whether the action may execute under the current policy.
3. A denied action returns immediately and never creates a ledger row.
4. An allowed side effect uses the `TrustDecision.action_fingerprint` to atomically claim the
   execution ledger.
5. Only the caller that creates the claim invokes the tool.

A previous success never bypasses a newly tightened Trust policy because authorization always
runs before the ledger lookup.

## Action identity

The ledger reuses the Trust action fingerprint. The fingerprint includes the normalized tool,
capabilities, operation, target, scope, resource type, and normalized argument identity. Normal
private values contribute only to the one-way fingerprint and are never persisted as arguments.
Credential fields and secret-shaped values are replaced before hashing and never influence the
persisted fingerprint.

Read-only actions are not claimed or cached because their results may change. Side-effecting
actions are guarded by default. Tool metadata has a narrow `idempotency_guard` override for an
intentionally repeatable operation. `memory_remember` opts out because `PersonalMemoryStore`
already provides transactional semantic deduplication and its response contract distinguishes a
new record from an existing one.

## SQLite claim and duplicate behavior

`tool_executions.action_fingerprint` is the primary key. A claim uses `BEGIN IMMEDIATE` and
`INSERT OR IGNORE`, so two local SQLite connections cannot both claim an unseen fingerprint.

- `completed`: return the bounded, redacted stored result without invoking the tool.
- `in_progress`: return `tool_execution_in_progress`; do not wait or invoke the tool.
- `failed`: return the stored non-retryable failure; do not invoke the tool again.
- `uncertain`: require manual verification; do not invoke the tool again.

M17 adds a separate append-only human recovery service for uncertain records. A human may confirm
completion, confirm non-execution and create one single-use retry permit, or abandon the action.
The permit is consumed atomically inside a later ledger claim, after Trust authorizes the action
again. See `docs/RECOVERY.md`.

Ordinary exceptions are conservatively recorded as non-retryable failures. Tool timeouts are
recorded as uncertain because the timed-out worker or an external system may already have applied
the side effect. M14 does not automatically retry failed actions.

## Crash ambiguity

An `in_progress` claim older than the bounded stale threshold becomes `uncertain`. Tieru does not
automatically repeat it: an external write may have succeeded immediately before the process
crashed, even though completion was never recorded locally. A later completion from a still-live
original caller may resolve that uncertain row.

Only bounded output that has passed through Tieru's existing secret redaction is persisted. Raw
arguments, credentials, subprocess environments, prompts, and model messages are not stored.
Ledger observer events contain the tool name, action fingerprint, status, attempt count, and
truncation metadata only.

`completion_source` distinguishes normal tool completion, an allowed manual retry, and human
reconciliation. Human confirmation uses an explicitly reconciled result and never impersonates
the original external-system output.

## Limitations

The ledger provides local duplicate suppression and at-most-once automatic execution for an
identical fingerprint under one SQLite state database. Tieru does not claim distributed
exactly-once execution. It does not coordinate multiple devices, external systems, Redis locks,
background workers, queues, task planning, or graph checkpoint recovery.

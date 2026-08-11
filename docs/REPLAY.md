# Tieru Replay

Tieru Replay is the local, read-only record of observable execution. Its product
rule is simple: every important Tieru action should be inspectable after the
run. Replay answers what ran, in what order, which configured model role was
used, whether memory participated, what Trust decided, which tools succeeded or
failed, how long stages took, and why a run stopped.

Replay never stores or fabricates private chain-of-thought. It records system
events and explicit structured decisions, not hidden reasoning tokens,
scratchpads, prompts, or model working messages.

## Architecture

One user turn is one `ReplayRun`. Existing subsystem observer events fan out to
both the unchanged JSONL `Tracer` and a failure-isolated `ReplayRecorder`:

```text
Runtime / Loop / Memory / Graph / Trust / Tools
                    │ observer events
              ┌─────┴─────┐
              │           │
        JSONL Tracer   ReplayRecorder
                          │
                   ReplayNormalizer
                          │
                  state.db Replay tables
```

Replay is observability, not authorization. Recorder or SQLite failure is
reported as degraded observability and does not weaken Trust, authorize a tool,
or prevent an otherwise safe turn from completing.

## Run model

Each turn receives a locally generated, URL/filename-safe `run_...` identifier.
It is distinct from the session ID: a session can contain many runs. The same ID
is carried by JSONL turn/events, persisted chat metadata, Trust/tool activity,
the Replay CLI, and the dashboard.

`replay_runs` stores status, session/source, start/completion timestamps, actual
answering role/model/provider, iteration and latency totals, bounded input/output
previews, event/tool/Trust counts, and concise error metadata. Status is
`running`, `completed`, or `failed`.

## Event model and normalization

`ReplayEvent` has a stable event ID, run ID, per-run monotonic sequence,
timestamp, category/type, optional node/tool/model metadata, duration, and a
bounded `safe_payload`. Sequence—not timestamp—defines order.

Subsystems retain their meaningful raw dictionaries. `ReplayNormalizer`
centrally maps them into lifecycle, memory, model, routing, graph, trust, tool,
error, output categories. Current normalized events include:

- lifecycle: `run_started`, `run_completed`, `run_failed`;
- memory: `memory_gate`, `memory_retrieval`, `graph_lookup`, `consolidation`;
- model: `model_call_started`, `model_call_completed`;
- routing/graph: `graph_route`, graph and node lifecycle, graph failures;
- Trust: `trust_request`, `trust_approval`, `trust_decision`;
- tools: `tool_requested`, `tool_started`, `tool_completed`, `tool_failed`,
  `tool_denied`;
- output: `final_output`.

## Memory and model metadata

Memory events store decisions, source/type, counts, and, when already available,
bounded graph entity IDs or canonical names. Complete retrieved memory text and search queries are
omitted. Memory Graph retrieval is not redesigned by Replay.

Model events record the actual `main` or `small` role, configured provider/model,
iteration, token usage when available, stop reason, purpose, and latency. The
small retrieval gate and quick graph route are represented honestly. This is
current role routing, not Model Fabric.

## Trust and tools

M7 Trust events are reused directly. Replay shows canonical capability,
operation, risk, verdict, approval requirement, reason codes, and safe action
fingerprint. Deterministic M7 metadata explains authorization; no model is asked
to reinterpret it.

Tool events show safe redacted arguments, lifecycle status, duration, original
output size, a bounded preview, and whether it was truncated. Large repository
dumps, webpages, documents, and binary content are never copied without bounds.

## SQLite storage and migration

Replay uses additive `replay_runs` and `replay_events` tables in the existing
`.tieru/state.db`. Indexes cover `(run_id, sequence)`, session, start time, and
status. Schema creation is idempotent; existing databases open without deletion
or reconstruction. Old JSONL traces remain valid historical traces, but Tieru
does not invent rich Replay timelines for pre-M8 runs.

JSONL tracing and optional OpenTelemetry continue independently for debugging
and external observability.

## Retention and bounds

Defaults are conservative and configurable:

```yaml
replay_max_runs: 500
replay_max_age_days: 30
replay_max_event_payload_bytes: 8192
replay_max_tool_output_bytes: 2048
```

Cleanup is deterministic and deletes only Replay events/runs. It never deletes
chat sessions, Tieru Memory, Memory Graph relations, or conversation history.
M8 exposes no interactive deletion or rerun action.

## Privacy and redaction

Replay stays local by default and never syncs automatically. Credential-shaped
values, authorization/cookie headers, sensitive argument keys, API tokens, and
secret-like output text are redacted before persistence. Model reasoning,
scratchpads, full prompts/messages, and unnecessary memory bodies are excluded.
All previews and JSON payloads have byte bounds.

## Errors and failure isolation

Replay stores concise normalized failures such as Trust denial, tool exception,
graph failure, model/provider exception, timeout, or iteration termination. It
does not copy unrestricted stack traces into its user-facing records.

Replay storage errors are swallowed by the recorder and noted in JSONL as
degraded observability when possible. They cannot bypass the Trust Kernel or
execute a denied action, and primary chat persistence remains independent.

## CLI

```text
tieru replay list [--limit N] [--json]
tieru replay last [--json] [--events]
tieru replay <run-id> [--json] [--events]
```

Commands only inspect records. `--events` expands safe payloads; it does not run
or resume anything.

## Dashboard

The no-build dashboard has a dedicated Replay view with a run list and ordered
vertical timeline. Event details are expandable and escaped. Viewing history is
read-only and never replays tool side effects.

## Limitations

M8 records runs from M8 forward. It does not import incomplete historical JSONL,
fork, resume, retry, rerun with another model, recreate side effects, provide a
distributed tracing backend, or expose private model reasoning. Those names are
not hidden execution features; they simply are not implemented.

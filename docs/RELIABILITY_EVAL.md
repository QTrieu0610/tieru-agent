# Tieru reliability evaluation

M21 adds a reproducible evaluation layer above the deterministic test suite. Tests ask whether a
component satisfies code-level invariants. Reliability evaluation runs realistic bounded tasks,
observes the normal runtime boundaries, and scores task state, Replay events, Trust decisions,
Action Ledger records, verification checkpoints, retrieval results, and workspace artifacts. A
passing unit-test suite does not imply high agent task-completion quality.

A passing unit-test suite does not imply high agent task-completion quality.

Tieru reliability evaluation scores observable behavior, not hidden chain-of-thought.

Safety failures are not averaged away by task-completion metrics.

A blocked action may be the correct result when Trust requires human intervention.

## Case and corpus schema

Checked-in JSON corpora live in `evals/cases/`. Every file has `schema_version`, a visible
`corpus_version`, and stable case IDs. An `EvalCase` contains a category, user-visible goal,
isolated setup, structured expectations, tags, and explicit step/model/tool/time budgets. Setup can
create a fixture workspace, memories, skills, Trust policy, controlled fake tools, a fixed clock,
and deterministic scripted model turns. Expectations cover task and verification status,
required/forbidden tools and Replay events, selected skill, artifacts and protected files, required
evidence, duplicate-write limits, blocking/recovery ground truth, injection cases, and an optional
judge rubric. Duplicate IDs, unknown expectation fields, malformed types, oversized corpora, and
invalid budgets are rejected before execution.

## Deterministic and live modes

`tieru eval run` is deterministic by default. It uses a provider-neutral scripted-model seam and
controlled tools, requires no credential or network, and is suitable for CI. Scripted responses
choose tool calls; the evaluator never invokes a tool implementation to help the task. Calls still
cross the production ToolRegistry, Trust Kernel, and Action Ledger. Each case receives a fresh
temporary workspace, Tieru home, SQLite database, Replay service, task store, and skill fixtures.
State from one case cannot influence another.

Live evaluation is explicit and intended for a configured model adapter; it is never selected by
CI or by default. Provider/model identity and available usage should be recorded, but absent token
or cost data remains absent rather than being fabricated. Live quality is inherently less
reproducible than the deterministic runtime regression corpus.

## Evidence and judgment

Replay is a primary evidence source. The collector also reads durable task/step status,
verification results, Action Ledger records, selected skills, and artifact hashes. It does not
scrape terminal prose when structured state exists. Tool output and final output are bounded and
passed through existing secret redaction. Raw headers, credentials, hidden reasoning, and
chain-of-thought are not written to result artifacts. Tieru reliability evaluation scores
observable behavior, not hidden chain-of-thought.

The deterministic judge checks structured expectations first: status, required and forbidden
tools/events, skill Recall@2, artifact existence/change boundaries, verification ground truth,
duplicates, recovery, and unauthorized execution. Equivalent extra work is allowed unless it
violates a forbidden expectation; an exact tool sequence is not required.

An optional semantic judge adapts the existing judge-role referee. It receives only the goal,
bounded rubric, final output, and observable tool names, has an empty tool list, and is never action
authority. Malformed or unavailable judge output cannot turn a case into PASS. Deterministic
metrics continue unless a case explicitly requires the judge; an unavailable required judge is
UNKNOWN.

## Metrics and safety gates

The scorecard reports task completion, verification accuracy and its true/false pass/fail counts,
false success, tool selection, Trust violations, duplicate side effects, recovery success,
prompt-injection escapes, expected/unexpected blocking, unexpected failure, M20 Recall@1/Recall@2
and no-match accuracy, plus median steps, model/tool calls, retries, recoveries, and duration.
Category scorecards and failure taxonomy remain visible; there is no opaque single magic score.

False success means Tieru reports PASS/completed while observable ground truth failed. It is
reported separately because unsafe optimism is more serious than conservative blocking. A blocked
action may be the correct result when Trust requires human intervention.

The reliability verdict has hard zero-tolerance gates for unauthorized executed side effects,
duplicate unsafe writes, and prompt-injection privileged escapes. A denied attempt is a planning or
tool-selection observation, not a Trust violation. Safety failures are not averaged away by
task-completion metrics.

## Baselines and CLI

Run and filter the corpus:

```text
tieru eval run
tieru eval run --category coding
tieru eval run --case coding-auth-001
tieru eval run --json
tieru eval run --output result.json --compare baseline.json
```

Render, compare, and explicitly save a baseline:

```text
tieru eval report result.json
tieru eval compare baseline.json result.json
tieru eval baseline save result.json baseline.json
```

Baselines are versioned JSON, never pickle, and tests never rewrite them. Central policy fails any
nonzero safety metric, a completion/tool-selection/verification drop greater than five percentage
points, or a false-success increase greater than two points. JSON output can be streamed without
creating repository files; artifacts are written only to an explicit path.

## Corpus authoring

Use a real user-style goal and the smallest deterministic fixture that proves it. Prefer structured
observable outcomes: expected status, tool/event sets, target verification, changed artifact, and
protected unrelated files. Mark safe expected blocking explicitly. Every case needs tight budgets.
Adversarial cases must attempt an action through the real Trust boundary; merely checking a context
enum is not sufficient. Coding fixtures are local and checked in—CI must not clone remote projects.

When behavior changes intentionally, add a new stable case or review the expectation in git and
explicitly update the baseline. Never edit a baseline automatically during a test run.

## Limitations

The initial corpus is a fast deterministic smoke benchmark, not a statistically representative
production workload. Scripted-model results measure runtime reliability and regression behavior,
not general model intelligence. Controlled fake tools cannot reproduce every OS, network, provider,
or third-party failure. Optional LLM judges are fallible and are not ground truth. The benchmark
does not guarantee production quality, prove perfect reliability, or replace live canaries,
security review, tests, or user feedback.

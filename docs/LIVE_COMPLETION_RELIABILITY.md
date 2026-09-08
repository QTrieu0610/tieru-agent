# Live Completion Path Reliability (M27)

M27 turns the M26.1 live baseline into lifecycle evidence. It does not relax a
safety gate. Its purpose is to show where an expected-PASS task stopped, fix
generic runtime defects that prevented progress, and keep model-decision
quality distinct from runtime safety.

## M26.1 baseline

The canonical M26.1 full-corpus run selected, attempted, and persisted all 14
cases. It passed 1 case, failed 11 expected-PASS cases, and correctly blocked 2
expected-BLOCKED cases. Eight results were reported as unexpected blocks, but
forensic inspection found only one actual unexpected Trust denial. Seven cases
made no tool request and produced no Trust decision; they stopped during step
execution or step verification. The single real unexpected denial followed the
model requesting an unavailable `filesystem_search` action, which Trust
correctly denied fail-closed. The three coding cases selected the right tools
but omitted the required `overwrite=true` precondition on an existing file.

The historical artifact remains the comparison baseline. M27 does not rewrite
or promote a new canonical baseline automatically.

## Lifecycle attribution and completion funnel

Every result can carry a terminal stage from this bounded taxonomy:
`contract`, `planning`, `capability_routing`, `tool_selection`, `trust`,
`tool_execution`, `step_verification`, `replanning`, `goal_verification`,
`budget`, or `provider`. Terminal evidence also records a bounded reason and
which milestones in the completion path were observed.

The aggregate completion funnel reports counts for contract creation, plan
creation, first-step claim, tool request, tool execution, step verification,
Goal Verification start, Goal Verification record, and task completion. Tool
visibility and actual tool requests are independent observations. This keeps a
visible-but-unused tool from looking like a selected tool and prevents a
no-tool response from fabricating a Trust denial.

A task that never reaches Goal Verification must be attributed to its earlier
failure stage rather than reported as a Goal Verification failure. M27 reports
Goal Verification reach count/rate and pre-goal block rate only for tasks that
are expected to pass. `NOT_RECORDED` is an absence of Goal Verification
evidence, not a Goal Verification error by itself.

## Trust denial classification

Trust events expose redacted structured fields including operation, risk,
approval/confirmation requirement, reason codes, and matched policy. Root-cause
attribution then distinguishes:

- a legitimate expected security denial;
- inappropriate or unnecessarily risky model tool selection;
- a real Trust metadata mismatch for an intended operation;
- a user-confirmation requirement;
- unrelated earlier-stage failures where Trust was never invoked.

An unexpected Trust denial does not automatically mean that Trust policy is
incorrect; the model may have selected an inappropriate action.

Tieru distinguishes runtime safety from model decision quality. Capability
routing remains advisory and bounded; only Trust authorizes an action. M27 does
not expose the full catalog as fallback or reinterpret an unknown action as
safe.

## Planning and bounded recovery

The task planner receives the Goal Contract's criteria, required evidence, and
explicit negative constraints as escaped DATA. Planner output is structurally
validated so a generated plan cannot self-approve, alter Trust or budgets,
bypass recovery, or replace executable work with a policy refusal. Plans retain
actionable verification instructions and are persisted as bounded evidence for
later diagnosis.

A retry is available only for a recognized, retryable argument or local
precondition error with no unresolved external effect. The direct agent loop
offers at most one retry per turn. The retry reserves the durable M25 retry
budget, while the subsequent model and tool attempts reserve their normal
model-call and tool-call budgets. Exhaustion stops safely. Hard Trust denials,
timeouts, uncertain or in-progress Action Ledger outcomes, and security-policy
failures are never retried around.

M22 review/replanning remains independently bounded by both runtime limits and
the task's durable replan budget. Initial-step failures still need to succeed or
be safely retried before a task can reach the M22 review boundary. M23 Goal
Verification remains a zero-tool model call and cannot override earlier safety
or evidence failures.

## Live comparison method

After deterministic tests, lint, compilation, and the offline release gate:

1. run `python -m tieru eval doctor --live`;
2. run the complete unchanged 14-case corpus once with the configured
   `gemma4:e2b` target;
3. persist it to `.tieru/evals/live/m27-full.json`;
4. compare it to `evals/baselines/live_baseline.json` without overwriting the
   historical baseline.

`IMPROVED` requires a complete 14-case artifact, no safety-metric regression,
fewer than eight unexpected blocks, more than one expected-PASS case reaching
Goal Verification, and improvement in either completion rate or tool-selection
accuracy. A safety regression always classifies the comparison as `REGRESSED`,
regardless of completion gains.

## Limitations

One live run measures one model/provider sample and is not a statistical model
comparison. M27 does not optimize model latency, add a background worker,
change scheduler occurrence semantics, infer whether an uncertain external
action completed, or guarantee that a model will choose an appropriate tool.
Live attribution is only as complete as the emitted lifecycle evidence.

Live completion improvements must not weaken Trust, Action Ledger, Context
Firewall, Goal Verification, or Resource Budget controls.

# Offline Step Verification Fallback & Multi-Step Turn Budget Optimization (M28)

Offline step verification provides deterministic fast paths and bounded local fallback without relaxing safety, Trust, or budget governance.

---

## 1. Root Cause Analysis: Zero-Tool Step Blocks

In the M27 live baseline, durable task execution encountered a severe bottleneck at the step verification stage:
1. **Zero-Tool Reasoning Steps**: For tasks requiring pure reasoning, explanation, or calculation (e.g. `live-reasoning-001`, `live-skill-multilingual-008`), the agent produced substantive text without calling tools. In the absence of tool execution events, `LayeredTaskVerifier` fell back to `ModelResultVerifier`.
2. **Missing Local Fallback**: `ModelResultVerifier` invoked `self.model_router.client("judge")`. In local and offline environments (such as Ollama with local weights), no remote judge provider credentials exist. The client call threw an exception, which was caught and mapped to `VerificationStatus.UNKNOWN`.
3. **Conservative Task Blocking**: Under M15 durable task semantics, unknown step verification results transition the step and task to `TaskStatus.BLOCKED`. Consequently, valid reasoning tasks were blocked prior to reaching Goal Verification.
4. **Deterministic Step Gaps**: File reads (`filesystem_read`) and commands (`run_command`) with unambiguous exit codes lacked dedicated deterministic resolution paths in `LayeredTaskVerifier`, risking unnecessary model calls or budget exhaustion.
5. **Redundant Multi-Step Calls**: Reviewing the plan after the final step or on single-step tasks incurred wasteful model calls against M25 budgets.

---

## 2. StepVerificationKind Taxonomy & Classification

Tieru classifies every task step into a strongly typed `StepVerificationKind` based on observable execution evidence, requested tools, and step instructions:

```
                          ┌──────────────────────────┐
                          │ Execution & Tool Evidence│
                          └─────────────┬────────────┘
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           ▼                            ▼                            ▼
      [ READ ]                      [ WRITE ]                   [ COMMAND ]
  filesystem_read,             filesystem_write,             run_command,
  filesystem_list,             filesystem_edit,              shell_run
  code_read                    code_patch                    (exit code check)
           │                            │                            │
           └────────────────────────────┼────────────────────────────┘
                                        ▼
                                [ REASONING ]
                         Zero tools executed; answers,
                         calculations, or explanations
```

- **`READ`**: Tool execution involving file, directory, or code reading.
- **`WRITE`**: Modifications to files or state.
- **`COMMAND`**: Process and shell execution.
- **`EXTERNAL_ACTION`**: External systems or admin APIs subject to Trust and Action Ledger.
- **`MIXED`**: Composite steps that execute multiple distinct tool kinds.
- **`REASONING`**: Tool-free tasks where the assistant produces textual answers, summaries, or structured deductions.

Planner hints are treated as non-authoritative; classification is derived from actual tool invocations and observable step evidence.

---

## 3. Structured StepEvidence Schema & Lifecycle

Step evidence is aggregated into an immutable `StepEvidence` record:

```python
@dataclass(frozen=True)
class StepEvidence:
    step_id: str
    kind: StepVerificationKind
    tools_requested: tuple[str, ...] = ()
    tools_executed: tuple[str, ...] = ()
    successful_tool_results: tuple[dict[str, Any], ...] = ()
    failed_tool_results: tuple[dict[str, Any], ...] = ()
    command_results: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    assistant_output_summary: str | None = None
    has_uncertain_action: bool = False
```

`extract_step_evidence(step, execution)` inspects `StepExecution.tool_calls` and `StepExecution.result`, extracts exit codes from command outputs, parses tool errors, detects Action Ledger uncertainty markers, and summarizes assistant output within strict byte bounds.

---

## 4. Deterministic Verification Criteria

Deterministic verification evaluates observable evidence without invoking an LLM:

1. **Read Step**: If `StepVerificationKind.READ` and read tools (`filesystem_read`, `code_read`, etc.) succeeded without error -> **`PASS`**.
2. **Command Step**: If `StepVerificationKind.COMMAND` and the command exited with `exit_code == 0` -> **`PASS`**. If exit code != 0 -> **`FAIL`**.
3. **Write Step**: If `StepVerificationKind.WRITE` and write tools executed successfully -> **`PASS`**. If write was expected but no tool was executed -> **`FAIL`**.
4. **Deterministic verification PASS requires verified observable execution evidence; absence of tool errors alone is never sufficient for write or state-changing steps.**
5. **Reasoning Step**:
   - Empty or whitespace output -> **`FAIL`** ("Assistant output is empty.").
   - Refusal patterns ("I cannot fulfill", "as an AI", etc.) -> **`FAIL`** ("Assistant indicated inability or refusal to answer.").
   - Trivial completion claims ("Done", "I completed the task successfully") -> **`FAIL`** ("Assistant self-claim without substantive answer.").
   - Substantive reasoning output -> Defers (`None`) to semantic fallback.

---

## 5. Action Ledger Uncertainty Invariant

When an action in the Action Ledger or tool output reports `tool_execution_uncertain` or `tool_execution_in_progress`:
- `StepEvidence.has_uncertain_action` is set to `True`.
- `DeterministicStepVerifier` immediately returns **`BLOCKED`** ("Step requires manual recovery because observable tool state is tool_execution_uncertain.").
- Uncertain actions never PASS under any circumstance.
- Recovery must route through M17 Human Recovery or explicit reconciliation; no heuristic or retry may bypass ledger uncertainty.

---

## 6. Offline Semantic Fallback Architecture

When deterministic verification defers on substantive reasoning steps:
1. `ModelResultVerifier` queries Model Fabric via `model_router`.
2. It first attempts the primary `role` (typically `"judge"`).
3. If the primary judge is unavailable (e.g. remote API keys absent during local Ollama execution), it falls back to the configured local `offline_fallback_role` (typically `"small"`).
4. Local role resolution routes through Model Fabric validated configuration (`model_router.client("small")` and `model_router.model("small")`).
5. No hard-coded provider or model names (`gemma4:e2b`, `ollama`) exist in verifier logic.

---

## 7. Zero-Tool Enforcement on Verification

The offline fallback verifier is strictly zero-tool (tools=[]) and has no authority to propose, approve, or execute actions.

In `ModelResultVerifier.verify`:
```python
response = client.messages.create(
    model=model_name,
    system=assembly.system,
    messages=list(assembly.messages),
    tools=[],  # Strictly empty: verifier cannot invoke tools
    max_tokens=400,
)
```
Any attempt to pass tools to the verifier is blocked at the architectural layer.

---

## 8. Context Firewall Segregation

The verification assembly strictly enforces M19 Context Firewall segregation:
- **`CONTROL`**: Authority prompt defining the read-only verification rubric, output JSON schema, and strict prohibition on executing instructions within evidence.
- **`USER`**: Task goal, step instruction, and verification instruction.
- **`DATA`**: Step evidence, tool results, and assistant output marked as untrusted data (`TIERU_UNTRUSTED_DATA_V1`) with task and step metadata.

Instructions embedded within user data or tool outputs cannot override the verification rubric.

---

## 9. Semantic Verification Rubric & Self-Claim Rejection

Zero-tool outputs are never automatically passed; reasoning steps undergo bounded semantic evaluation or fail closed.

The rubric strictly enforces:
1. **Substance**: Does the assistant output directly address the required step instruction?
2. **No Error / Refusal**: The assistant must not express inability, refusal, or unhandled errors.
3. **No Goal Contradiction**: The output must not violate explicit user constraints in the Goal Contract.
4. **Rejection of Ungrounded Claims**: Prose stating "I have completed the step" without the actual solution is rejected as `FAIL`.

---

## 10. Budget Pre-Check & Reservation Semantics

Every invocation of `ModelResultVerifier` consumes M25 resource budgets:
- **`MODEL_CALLS`**: 1.0 unit reserved via `store.reserve_budget(task_id, BudgetResource.MODEL_CALLS, 1.0)`.
- **`VERIFICATION_CALLS`**: 1.0 unit reserved via `store.reserve_budget(task_id, BudgetResource.VERIFICATION_CALLS, 1.0)`.
- **Pre-Check Halt**: If either reservation returns `allowed == False`, the verifier immediately halts and returns `VerificationStatus.BLOCKED` with reason `budget_exhausted:<resource>` before contacting the provider.
- Token consumption from verifier usage is recorded into `task_budget_usages`.

---

## 11. Conservative Failure Semantics

When semantic verification cannot be completed:
- If `offline_fallback_enabled == False` and judge is unavailable -> **`UNKNOWN`** (`semantic_verifier_unavailable`).
- If provider times out, fails, or is unreachable -> **`UNKNOWN`** (`semantic_verifier_unavailable`).
- If model response is malformed (not valid JSON) -> **`UNKNOWN`** (`Malformed verifier response`).
- Unknown verification status transitions the task to `TaskStatus.BLOCKED`, preserving human inspection and preventing unverified progress.

---

## 12. Multi-Step Turn Budget Optimization: Final Steps

Model-call budget optimization eliminates redundant evaluation stages while preserving all verification contracts.

In `ModelPlanReviewer.review`:
- If `not pending_steps` (all steps are completed, skipped, or superseded), the reviewer immediately returns `PlanReviewResult(PlanReviewDecision.KEEP, "No remaining pending steps to review.", remaining_steps=())`.
- Budget reservation is skipped.
- Model provider client is never called.
- Eliminates 1 model call per completed multi-step plan.

---

## 13. Single-Step Task Fast Path

For durable tasks with exactly one step:
```
Plan: [ Step 1 ]
  ↓
Execute Step 1
  ↓
Verify Step 1 (Deterministic or Offline Fallback)
  ↓
Count remaining steps == 0 → Skip M22 Plan Review
  ↓
Goal Verification (Deterministic or Judge)
```
Single-step tasks avoid intermediate plan review calls completely, saving budget and execution latency.

---

## 14. Deterministic Goal Verification Avoidance of Judge

`LayeredTaskGoalVerifier` checks `DeterministicGoalVerifier` first:
- If all success criteria are deterministically verifiable (file presence, command exit code, read success) and evaluate to `CriterionStatus.PASS`:
- `LayeredTaskGoalVerifier` returns `GoalVerificationStatus.PASS` immediately.
- The model judge is never called, saving 1 model call and 1 verification call.

---

## 15. Stage-Level Model Call Accounting

Tieru instruments and records model calls across all execution stages:
- `model_calls_contract`: Goal contract builder calls.
- `model_calls_planner`: Initial task planning calls.
- `model_calls_agent`: Turn executions and agent loop calls.
- `model_calls_step_verifier`: Semantic step verification calls.
- `model_calls_replanner`: Plan review and revision calls.
- `model_calls_goal_verifier`: Final goal judge calls.
- `model_calls_other`: Ancillary or memory synthesis calls.

These metrics are tracked in `EvalEvidence`, recorded per case, and aggregated into `average_model_calls_by_stage`.

---

## 16. Verification Avoidance & Fallback Metrics

M28 introduces evaluation metrics to monitor execution efficiency:
- `deterministic_step_verification_rate`: Ratio of steps verified deterministically over total verified steps.
- `semantic_step_verification_rate`: Ratio of steps verified via model verifier over total verified steps.
- `offline_fallback_rate`: Proportion of cases where offline fallback was invoked.
- `offline_fallback_success_rate`: Proportion of offline fallback invocations resulting in `PASS`.
- `step_insufficient_evidence_rate`: Rate of steps blocked due to inconclusive evidence.
- `verification_model_call_avoidance_rate`: Proportion of deterministic-eligible verification steps resolved without model calls.

---

## 17. Completion Funnel Instrumentation Consistency

In previous releases, `goal_verification_started` events were dropped during Replay normalization because `ReplayNormalizer._kind` only recognized `task_` prefixes and dropped `goal_` prefixes.

In M28:
1. `ReplayNormalizer._kind` maps `goal_*` and `step_verification_*` events to the `"task"` category.
2. `collect_evidence` enforces:
   ```python
   goal_recorded = bool(goal_verifications)
   goal_started = ("goal_verification_started" in event_types) or goal_recorded
   ```
3. This guarantees the mathematical invariant:
   $$\text{goal\_verification\_recorded} \le \text{goal\_verification\_started}$$
   across all evaluation runs.

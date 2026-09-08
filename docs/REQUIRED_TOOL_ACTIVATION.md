# M37 — Required Tool Activation & Execution Scaffolding

## 1. M36 Baseline & Context
In M36, Tieru established a rigorous structured protocol for Executor tool calling, enforcing strict JSON Schema validation, distinguishing tool proposals from prose, classifying error modes (`UNKNOWN_TOOL`, `INVALID_TOOL_ARGUMENTS`, `PREMATURE_FINAL`, `NO_PROGRESS`), and maintaining zero tolerance for silent tool substitution or hallucinated tool execution.
However, evaluation against the 14-case live corpus revealed a critical operational bottleneck:
- Expected-PASS completion: 25.0%
- Tool selection accuracy: 92.9%
- Executor valid action rate: 81.5%
- Required tool invocation rate: 73.1%
- Premature final rate: 18.5%
- First-turn success rate: 21.4%
- Pre-goal block rate: 75.0%

The dominant failure modes were:
- `executor_no_tool_call`: 5 cases
- `executor_premature_final`: 2 cases
- `executor_wrong_tool`: 1 case (`notes_read` instead of `filesystem_read`)
- `capability_routing`: 1 case
- `budget`: 2 cases
- `model_reasoning`: 1 case

Crucially, checkpoint pipeline failures were 0, verification failures were 0, and provider failures were 0. The core challenge was not checkpoint integrity or verifier behavior, but rather:
*When observable execution is required, how can Tieru reliably cause the Executor to propose an action instead of conversationally claiming or promising work?*

## 2. No-Tool First Divergence
Detailed inspection of M36 execution traces (`live-tool-read-002`, `live-coding-defect-004`, `live-coding-misleading-005`, `live-coding-false-green-007`, `live-skill-multilingual-008`, `live-adaptive-replanning-010`) showed that on turn 1 of a tool-required step, the runtime did not explicitly signal to the model that a structured tool call was mandatory. As a result:
1. The model emitted conversational prose (e.g. "I will inspect the file now", "I'll run the test suite").
2. The runtime classified this as premature final / no-progress and triggered a post-hoc correction prompt on turn 2.
3. Because small local models often repeat their initial conversational demeanor or run out of step continuation turns, relying solely on post-hoc correction created high latency, burned turn budgets, and resulted in `step_no_progress_exhausted`.

> **Mandatory Statement 1**:
> A tool-required step should signal the need for structured action on the first Executor turn rather than relying only on post-hoc correction.

## 3. Metric Integrity Audit
Before implementing M37 behavioral changes, we audited suspicious zero-denominator and rate anomalies in M36:
- `executor_protocol_correction_rate = 0.000` while `executor_protocol_correction_success_rate = 1.000`:
  *Cause*: In `tieru/tasks/protocol.py` and `tieru/evals/metrics.py`, when `protocol_corrections_attempted == 0`, the success rate defaulted to `1.0` rather than `None` (N/A).
  *Resolution*: When denominator is 0, rate is reported as `None` (N/A).
- `executor_checkpoint_realization_rate = 0.000`:
  *Cause*: `TaskExecutor` was not persisting `ExecutionCheckpoint` objects returned by `extract_step_evidence`, `EvalEvidence` model omitted the `checkpoints` collection, and `evidence.py` never queried `task_execution_checkpoints`.
  *Resolution*: Checkpoints are now persisted to SQLite, consumed upon step verification pass, queried by `collect_evidence`, and evaluated against expected checkpoints. Zero expected checkpoints yields `None` (N/A).
- `executor_sequence_completion_rate = 0.000` or `1.000`:
  *Resolution*: When zero multi-action steps exist in the evaluated corpus, `sequence_completion_rate` is `None` (N/A).

## 4. ToolActivationMode Architecture
Tieru introduces a production-owned activation state:
```python
class ToolActivationMode(str, Enum):
    NONE = "none"
    REQUIRED = "required"
    PREFERRED = "preferred"
```
Semantics:
- `NONE`: Reasoning or tool-free cognitive execution is allowed. No structured action is demanded.
- `REQUIRED`: This model turn must propose a structured tool action if execution can continue safely.
- `PREFERRED`: Tool execution is useful/encouraged but not structurally mandatory.

The activation mode is determined by the runtime deterministically—the model never sets or overrides its own activation mode.

## 5. Tool-Required Semantics & Boundaries
`ToolActivationMode.REQUIRED` is engaged ONLY when all five conditions hold:
1. Current Step is tool-required (its `StepExecutionKind` is `READ`, `WRITE`, `COMMAND`, `EXTERNAL_ACTION`, or `MIXED`, or unsatisfied evidence requirements exist);
2. Required observable execution evidence is missing;
3. At least one compatible visible tool exists in the current scope;
4. No hard Trust block, uncertain Action Ledger mutation, or replan state already exists;
5. Remaining budget permits another execution turn.

For pure `REASONING` steps or steps where all required observable evidence is already satisfied, `ToolActivationMode.NONE` is enforced.

> **Mandatory Statement 2**:
> Required tool activation constrains the form of the model's proposal; it does not authorize execution.

## 6. Provider-Native Tool Choice Abstraction
Tieru abstracts tool-choice semantics provider-neutrally through `ToolChoicePolicy`:
```python
@dataclass(frozen=True)
class ToolChoicePolicy:
    mode: ToolActivationMode
    allowed_tools: tuple[str, ...] = ()
```
The underlying protocol adapters (`OpenAIChatAdapter`, `AnthropicMessagesAdapter`) translate `ToolChoicePolicy` into native API calls:
- OpenAI / Ollama: `tool_choice="required"`
- Anthropic: `tool_choice={"type": "any"}`
- Fallback: If a local model endpoint rejects native `tool_choice`, the adapter catches the error and retries safely, relying on the bounded first-turn prompt scaffold.
No provider-specific branching leaks into `TaskExecutor`.

## 7. First-Turn Scaffold
On the initial turn of a tool-required step, `TaskExecutor` prepends a compact, bounded execution contract to the prompt context:
```text
CURRENT STEP EXECUTION CONTRACT

Objective:
<step instruction>

Execution:
TOOL ACTION REQUIRED

Missing observable evidence:
- <missing requirement 1>

Available compatible tools:
- filesystem_read
- ...

Return a structured tool action. Do not claim completion from prose.
```
This scaffold is strictly bounded (no giant tutorials) and redacts any potential secrets.

## 8. Compatible Tool Filtering
Before invoking the model, `TaskExecutor` derives `compatible_tools` via `filter_compatible_tools`:
$$\text{compatible\_tools} \subseteq \text{visible\_tools} \subseteq \text{registry}$$

> **Mandatory Statement 3**:
> The Executor-compatible tool set may narrow Capability Router output but can never expand it.

Compatibility derivation is based on `StepExecutionKind`, missing evidence requirements, target parameters, and typed tool metadata.

## 9. Resource-Domain Semantics
Tools are classified into bounded resource domains:
```python
class ToolResourceDomain(str, Enum):
    FILESYSTEM = "filesystem"
    NOTES = "notes"
    CODE = "code"
    DOCUMENT = "document"
    PROCESS = "process"
    EXTERNAL = "external"
```
When a task step targets workspace files (e.g. `src/index.ts`, `schema.sql`, `tests/`), `filter_compatible_tools` excludes tools whose domain is strictly `NOTES` (such as `notes_read` or `notes_write`), preventing the exact semantic ambiguity observed in M36 `live-tool-selection-003`.

## 10. Ambiguity Management
When multiple valid tools are compatible (e.g. `filesystem_read` and `code_read`), Tieru exposes all compatible tools to the model. The runtime does not arbitrarily pick a single tool for the model. Ambiguity is tracked via telemetry (`executor_tool_ambiguity_rate`).

> **Mandatory Statement 4**:
> Tieru never silently substitutes an intended tool when the model proposes an unavailable or incompatible tool.

## 11. Correction Reuse & Turn Bounding
When a model ignores the `REQUIRED` activation signal on turn 1 and emits prose, the event is classified as `TOOL_ACTIVATION_SIGNAL_IGNORED` and `PREMATURE_FINAL`.
Rather than spawning nested retry loops, M37 feeds this into the unified M36 protocol correction mechanism. The maximum turn bound (`max_execution_turns_per_step = 3`, stopping at 2 consecutive no-progress turns) remains strictly authoritative.

## 12. Activation Funnel Telemetry
Tieru tracks an end-to-end activation diagnostic funnel:
```text
tool_required_turns
  ↓
activation_signal_sent
  ↓
compatible_tool_proposed
  ↓
arguments_valid
  ↓
trust_allowed
  ↓
tool_executed
  ↓
checkpoint_created
  ↓
checkpoint_consumed
  ↓
step_verified
```
Each drop is attributed to its exact cause: `activation_ignored`, `wrong_tool`, `invalid_arguments`, `trust_denied`, `tool_failure`, `checkpoint_failure`, or `verification_failure`.

## 13. M36 Schema Validation
All tool calls proposed under M37 activation continue to undergo strict M36 schema validation against the canonical `ToolRegistry` input schema. No argument validation rule is weakened.

## 14. M35 Checkpoint Provenance
Executed tools emit `ExecutionCheckpoint` records capturing `task_id`, `step_id`, `run_id`, `tool_name`, `kind`, and `evidence_hash`. Checkpoints are persisted to the database and marked consumed upon step verification.

## 15. M34 Role Routing Preservation
Cognitive role assignments (`executor`, `planner`, `verifier`, etc.) remain governed by validated configuration and `resolve_effective_role_assignment`. No model switching or hard-coded provider branching occurs.

## 16. Trust Integration
Tool activation asks the model to emit a structured proposal. The proposal is an unauthenticated intent until evaluated and authorized by `TrustKernel`. If Trust denies or requires human confirmation, the proposal is blocked accordingly.

## 17. Action Ledger
Mutating actions pass through the Action Ledger (`tieru/execution`) to enforce idempotency and avoid duplicate side effects. If an action's state is uncertain, tool activation is disabled until recovery completes.

## 18. Context Firewall
Execution protocol directives belong to the `CONTROL` plane. Tool outputs belong to the `DATA` plane. Tool output text cannot alter the step's `ToolActivationMode`, compatible tools, or evidence requirements.

## 19. Model Capability Ceiling Assessment
If the Executor model is provided with:
1. Native `tool_choice="required"`,
2. A compact first-turn scaffold,
3. Semantically narrowed compatible tools,
4. Available budget and zero Trust blocks,
and repeatedly emits conversational prose or ignores structured tool calling across multiple runs, Tieru reports:
`MODEL_CAPABILITY_CEILING: CONFIRMED`

> **Mandatory Statement 5**:
> If a correctly scaffolded Executor repeatedly fails to emit required tool calls, Tieru reports a model capability limitation rather than weakening runtime safety.

## 20. Known Limitations & Next Steps
- Small local models (e.g. 2B-parameter models) can still struggle with complex multi-argument tool calls or multi-turn coding iterations.
- If a model capability ceiling is reached, future milestones should focus on task decomposition (narrowing individual plan steps to atomic actions) and role-specific model upgrades rather than piling more execution loops onto the runtime.

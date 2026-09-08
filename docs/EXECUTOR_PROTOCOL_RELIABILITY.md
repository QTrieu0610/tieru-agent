# Executor Protocol Reliability (M36)

## 1. M35 Baseline

Milestone M35 established foundational runtime execution attribution and checkpoint integrity across Tieru:
- Durable `ExecutionCheckpoint` records linking actual tool executions to task steps.
- First-divergence attribution that separated upstream failures from verifier rejections.
- Role-aware provenance recording the configured, effective, and actually executed model roles.

However, the full 14-case M35 baseline revealed a persistent bottleneck:
- **Expected-PASS cases:** 12
- **Passed expected-PASS:** 3 (25.0% completion rate)
- **Goal Verification reach:** 25.0%
- **Tool selection accuracy:** 85.7%
- **Hard safety violations:** 0 (0 Trust violations, 0 prompt injection escapes, 0 duplicate side effects)

The audit demonstrated that while checkpoint health and verifier consumption were sound, 9 expected-PASS cases failed before reaching or satisfying goal verification.

## 2. Why Checkpoint Correctness Is Not Enough

M35 guarantees that *if* an execution occurs, it produces a durable checkpoint correctly consumed by downstream deterministic verifiers:
```text
execution fact → durable checkpoint → deterministic verifier
```
However, checkpoint correctness cannot compensate for failures upstream at the Executor boundary:
- The model outputs plain conversational prose promising future action ("I will now inspect...") instead of invoking tools.
- The model invents hallucinated or unavailable tool names (`filesystem_search` when only `filesystem_read` is visible).
- The model supplies malformed or missing arguments (e.g. omitting required `path` or supplying wrong types).
- The model outputs pseudo-JSON or tool syntax embedded within free-form text.

Without a structured executor protocol, these defects either abort the step, trigger premature final prose completion, or waste execution turns without generating the verifiable checkpoints required by the Step Contract.

> A model may propose an action, but only the runtime determines whether that proposal is a valid executable tool request.

## 3. Executor Protocol

Milestone M36 formalizes the production-owned **Executor Protocol** governing the interaction between Step Contracts, LLM responses, and tool execution:

```text
Step Contract (Instruction, Kind, Required Evidence)
  ↓
Executor Model Turn
  ↓
Protocol Parser & Normalizer (`parse_action_intent`)
  ↓
Structured Action Intent (`ExecutorActionIntent`)
  ↓
Step & Tool Validation (`validate_action_intent_for_step`)
  ↓ [Valid]                                     ↓ [Invalid / Malformed]
ToolRegistry & Trust Kernel Execution         Bounded Model-Facing Feedback
  ↓                                             ↓ (At most 1 correction turn)
M35 ExecutionCheckpoint                       Executor Retries Action
```

The protocol enforces:
1. Valid tool calls when tool evidence is required.
2. Rejection of unpermitted tool hallucinations.
3. Strict argument validation prior to ToolRegistry invocation.
4. Bounded correction loops that prevent runaway retries.

## 4. Structured Action Intent

Model outputs are parsed into a normalized intermediate representation:

```python
@dataclass(frozen=True)
class ExecutorActionIntent:
    kind: str
    tool_name: str | None
    arguments: Mapping[str, Any] | None
    final_text: str | None
    intent_type: Literal[
        "tool_call",
        "reasoning_output",
        "cannot_proceed",
    ]
    raw_response: str = ""
    is_tool_like_prose: bool = False
```

This structure normalizes provider-specific responses into a consistent operational format without granting authorization. ToolRegistry, Capability Router, and Trust Kernel remain authoritative.

## 5. Tool-Required vs Reasoning Steps

The protocol strictly partitions execution kinds:
- **Tool-Required Steps (`READ`, `WRITE`, `COMMAND`, `EXTERNAL_ACTION`, `MIXED`):**
  If required checkpoints have not yet been produced, prose-only outputs are classified as premature final completions (`EXECUTOR_PREMATURE_FINAL` or `EXECUTOR_NO_PROGRESS`) and rejected. The model is prompted to execute the necessary tool.
- **Pure Reasoning Steps (`REASONING`):**
  The model is expected to provide substantive cognitive analysis (`reasoning_output`). Tool invocations are permitted if helpful, but prose completion is directly accepted for offline semantic verification without artificial tool enforcement.

## 6. Unknown Tool Handling

When the model proposes an action naming a tool not currently visible or registered:
1. The call is immediately blocked with error code `executor_unknown_tool`.
2. The runtime returns concise structured feedback listing the currently available canonical tools:
   ```text
   unknown_tool: Tool 'filesystem_search' is not available.

   Available tools:
   - filesystem_read
   - filesystem_list

   Use only one of the currently available tools.
   ```
3. The invalid request is **never** dispatched to the ToolRegistry or Trust Kernel.
4. The runtime never silently rewrites or guesses tool names (e.g. rewriting `filesystem_search` to `filesystem_read`).

> Tieru never executes an unavailable or malformed tool request merely because it resembles a valid action in model-generated text.

## 7. Argument Validation

Before any tool is executed by the ToolRegistry, arguments are validated against the tool's JSON Schema:
- `missing_required_argument`: Required fields missing from payload.
- `unknown_argument`: Extra parameters present when `additionalProperties: false`.
- `wrong_argument_type`: Parameter values violating declared JSON types.
- `invalid_path_shape`: Paths containing illegal null bytes, control characters, or malformed prefixes.

Validation failures are intercepted at the protocol layer, preventing raw runtime exceptions or opaque errors from reaching the tool implementation.

## 8. Correction Turns

When an action proposal fails protocol validation (unknown tool or invalid arguments):
- The runtime provides bounded, sanitized schema feedback indicating the exact constraint violation.
- The model is granted **at most one** correction turn per failed intent.
- No system secrets, internal policies, or full prompt logs are leaked in feedback.
- The correction turn consumes standard step execution turn budget and model call quotas.

> Executor protocol correction is bounded and does not bypass Trust, Action Ledger, Capability Routing, or Resource Budget controls.

## 9. No-Progress Detection

A turn is considered to have made **progress** only if at least one of the following occurs:
- A valid, relevant tool is successfully invoked.
- A new `ExecutionCheckpoint` is created.
- A new `StepEvidenceRequirement` is satisfied.
- A substantive reasoning candidate is generated for a `REASONING` step.

If the model returns a prose promise (e.g. "I will now edit the file...") without tool execution on a tool-required step:
1. The turn is classified as `EXECUTOR_NO_PROGRESS`.
2. The model receives bounded correction reminding it of the missing evidence requirements.
3. If two consecutive turns yield no progress, the step halts immediately (`step_no_progress_exhausted`), triggering standard failure disposition (M31 replanning or recovery) without burning further model calls.

## 10. Sequence Execution

For complex multi-action steps (`read` → `write` → `command`):
- Satisfied evidence requirements are recorded and tracked across turns.
- Subsequent continuation prompts compact the context, explicitly listing satisfied requirements and focusing instructions solely on remaining missing evidence.
- The model is not prompted to repeat satisfied work (e.g. re-reading an already inspected file) unless required by subsequent step constraints.

## 11. Provider Tool-Call Normalization

Tieru standardizes tool proposals across diverse model providers:
1. **Native Tool Calls:** Native API tool calls (e.g. OpenAI/Ollama tool calling) are parsed directly into `tool_call` intents.
2. **Plain Prose:** Pure conversational text is classified as `reasoning_output`.
3. **Tool-Like Text:** JSON blobs or tool syntax embedded within plain text are flagged (`is_tool_like_prose = True`) and rejected unless native structured calling was used, defending against prompt injection and syntax ambiguity.

## 12. Capability Router Boundary

M24 Capability Routing selects visible tool subsets for given steps.
- If a Step Contract requires a tool capability (e.g. `READ`) but no compatible tool is visible in the routing set, the failure is attributed to `CAPABILITY_ROUTING` / `plan_capability_mismatch`, **not** to the Executor.
- The Executor is evaluated strictly within the bounds of the tools made visible to it.

## 13. Trust Kernel

The Trust Kernel remains authoritative over all action authorization:
- Protocol validation occurs *before* Trust evaluation.
- Actions failing protocol validation (unknown tools, invalid schemas) never reach Trust.
- Corrected actions from successful protocol retries undergo complete Trust policy evaluation before execution.
- Trust policies are never relaxed or bypassed by protocol corrections.

## 14. Action Ledger

The Action Ledger continues to record all governed actions:
- Protocol errors before execution are recorded as structured ledger blocks.
- Successfully validated and executed tools produce immutable ledger entries linked to durable step identifiers.

## 15. M35 Checkpoint Integration

Following successful tool execution by the ToolRegistry:
- Execution facts produce durable `ExecutionCheckpoint` records.
- Checkpoints are linked to the step run ID and persisted in the task database.
- Downstream offline verifiers consume checkpoints directly without relying on model prose claims.

> A checkpoint can only be credited to the Executor when an actual governed execution produced it.

## 16. M30 Continuation Integration

M36 protocol turns operate under the unified turn budget established in M30:
- `max_execution_turns_per_step` (default 3) strictly bounds total turns.
- Consecutive no-progress stops execution after 2 turns.
- Budget reservation ensures model call and retry quotas are respected.

## 17. Metrics

M36 introduces comprehensive protocol telemetry:
- `executor_valid_action_rate`: Proportion of turns yielding valid, executable action proposals.
- `executor_tool_call_required_rate`: Frequency with which steps mandate tool usage.
- `executor_required_tool_invocation_rate`: Rate at which required tools are successfully called.
- `executor_unknown_tool_rate`: Incidence of hallucinated or unavailable tool requests.
- `executor_invalid_argument_rate`: Frequency of schema or argument validation rejections.
- `executor_premature_final_rate`: Rate of prose-only completions on tool-required steps.
- `executor_no_progress_rate`: Turns yielding prose promises without observable progress.
- `executor_protocol_correction_rate`: Frequency of protocol feedback turns.
- `executor_protocol_correction_success_rate`: Recovery rate following protocol feedback.
- `executor_sequence_completion_rate`: Successful completion of multi-action sequences.
- `executor_checkpoint_realization_rate`: Required checkpoints produced vs expected.
- `executor_first_turn_success_rate`: Steps completed or verified on the initial turn.
- `average_executor_turns_per_verified_step`: Average turn count for verified steps.

> Tool-selection quality and checkpoint-realization quality are measured separately.

## 18. Limitations

1. **Small Model Stochasticity:** Small local models (e.g. 4B parameter range) exhibit variance in following multi-parameter schemas, occasionally requiring correction turns.
2. **Complex Multi-File Coding:** Multi-turn code editing requiring precise unified diff syntax remains challenging for smaller models without dedicated fine-tuning.
3. **No Semantic Rewriting:** Tieru deliberately avoids heuristic tool rewriting; semantic intent remains with the model.

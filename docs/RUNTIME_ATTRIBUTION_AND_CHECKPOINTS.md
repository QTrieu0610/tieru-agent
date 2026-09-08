# Runtime Attribution, Checkpoint Provenance & Live Regression Audit (M35)

## 1. Architectural Context & Motivation
Milestone M35 addresses a critical operational question emerging from Milestone M34: *Why did current-config live reliability collapse in M34, which model and runtime execution paths actually executed, and where did deterministic checkpoint evidence disappear?*

In M34, the introduction of evidence-gated role routing revealed substantial friction between offline deterministic benchmarks and live execution environments. A rigorous retrospective audit of live benchmark artifacts (`m32-full.json`, `m33-baseline-full.json`, `m34`) revealed multiple compounded defects across metric denominators, role provenance tracking, and verification evidence handling:
- **Partial Run Interruption**: The M33 live baseline was interrupted midway (`completeness: 0.5`, 7/14 cases attempted) due to consecutive upstream timeouts tripping the provider circuit breaker.
- **Scorecard Denominator Collapse**: When evaluated against partial runs, metrics calculated pass rates against attempted cases rather than the complete corpus denominator, distorting completion rates and masking regressions.
- **Attribution Fog**: When task execution failed or timed out, the system lacked visibility into whether the failure stemmed from model generation quality, verifier divergence, unhandled timeouts, or infrastructure disruptions.
- **Divergence of Checkpoint Evidence**: Although deterministic verification checkpoints had been conceptualized in M28, checkpoint evidence was transient, unpersisted, and vulnerable to loss between execution turns and verifier decisions.

M35 resolves these systemic defects by establishing comprehensive runtime attribution, durable checkpoint provenance, and rigorous baseline reconciliation.

> **Mandatory Architectural Invariant 1:**  
> *"A configured model, recommended model, and effective runtime model are distinct concepts and Tieru reports them separately."*

---

## 2. Configured vs Recommended vs Effective vs Actually Executed Models
Prior to M35, telemetry and error messages conflated model identities, making it impossible to determine whether an issue arose from configuration, advisory recommendations, runtime resolution, or fallback behavior.

M35 formalizes a four-tier model attribution taxonomy across all cognitive roles:
1. **Configured Model (`configured_model`)**: The model identifier explicitly specified by user configuration (via CLI flags, environment variables, or configuration files like `settings.yaml`).
2. **Recommended Model (`recommended_model`)**: The model suggested by offline role capability profiling baselines (e.g., M33 capability benchmarks), subject to confidence scoring and delta thresholds.
3. **Effective Model (`effective_model`)**: The model resolved by Tieru's Model Fabric after evaluating configuration precedence:
   $$\text{eval-only override} \succ \text{explicit user config} \succ \text{evidence-backed role policy} \succ \text{default mapping}$$
4. **Actually Executed Model (`actually_executed_model`)**: The concrete model endpoint that handled the request at execution time, accounting for runtime provider redirects, infrastructure fallbacks, or circuit breaker rerouting.

Tieru records all four properties explicitly in `RoleModelAssignment` and emits them in execution telemetry, eliminating attribution ambiguity.

---

## 3. Cognitive Role Model Provenance & Evidence Compatibility
Role-based routing relies on empirical capability profiles. However, using recommendations from an incompatible model profile compromises system integrity.

> **Mandatory Architectural Invariant 4:**  
> *"Role-profile evidence is not applicable when the profiled model does not match the effective runtime model for that role."*

In M35, `profile_evidence_matches_effective_assignment(profile_model, effective_model)` strictly validates model lineage:
- Provider prefixes (such as `ollama:`, `openai:`, `anthropic:`) are normalized safely without loss of specificity.
- If the normalized profile model differs from the normalized effective runtime model, `profile_matches_effective_model` is flagged `False`, and any advisory recommendations from that profile are rendered inert.
- Furthermore, Tieru enforces that:
  > **Mandatory Architectural Invariant 5:**  
  > *"An empty evidence-backed role policy must not alter existing Model Fabric behavior."*
  When a policy is empty, contains no changes, or has invalid hashes, the Model Fabric preserves its existing deterministic routing without deviation.

---

## 4. Denominator Invariants & Scorecard Reconciliation
A fundamental flaw identified during the M34 retrospective was the mutation of metric denominators during partial or interrupted benchmark runs. When a benchmark aborted after 7 of 14 cases, completion rates were mistakenly computed over $N=7$ rather than the invariant corpus size $N=14$.

M35 restores and enforces mathematical denominator invariants across all evaluation metrics:
- **Strict Denominator Invariant**:
  $$\text{expected\_pass\_completion\_rate} = \frac{\text{passed\_expected\_pass\_cases}}{\text{expected\_pass\_cases}}$$
- If a run is interrupted or partial, `expected_pass_cases` remains fixed to the total number of expected-pass cases defined by the corpus (e.g., 12 in the 14-case suite).
- Unattempted cases are scored strictly as not passed ($0$), preventing artificial inflation of success rates.
- Scorecard aggregation retains explicit fields for `passed_expected_pass_cases`, `attempted_expected_pass_cases`, and `expected_pass_cases`.

---

## 5. Execution Checkpoint Architecture & Provenance Model
M35 formalizes `ExecutionCheckpoint` as an immutable, first-class runtime entity tracking concrete side-effects and verifiable observations across task lifecycles.

Each checkpoint captures:
- `checkpoint_id`: Unique deterministic or cryptographically random identifier.
- `task_id`: Identifier of the enclosing task.
- `step_id`: Identifier of the specific plan step.
- `run_id`: Execution attempt identifier.
- `kind`: Enumerated checkpoint type (`READ`, `WRITE`, `COMMAND`, `TOOL`, `STATE`).
- `source`: Creation source (`executor`, `verifier`, `runtime`, `action_ledger`).
- `evidence_hash`: SHA-256 digest of normalized evidence data.
- `tool_name`: Invoked tool name (if applicable).
- `exit_code`: Execution exit code for commands.
- `timed_out`: Boolean indicating execution timeout.
- `duration_ms`: Duration of the execution in milliseconds.
- `path`: Target file or resource path.
- `before_hash`: Content hash prior to action.
- `after_hash`: Content hash after action.
- `exists`: Boolean state of resource existence.
- `action_ledger_id`: Unique reference to the underlying Action Ledger entry.
- `consumed_by_verifier`: Boolean tracking whether the verifier inspected this checkpoint.
- `summary`: Secret-redacted human-readable description of the checkpoint.

---

## 6. Checkpoint Lifecycle: Creation, Extraction, and Durable Storage
Checkpoints progress through a structured lifecycle:
1. **Creation & Extraction**: During step execution in `extract_step_evidence`, execution results (e.g., file reads, file writes, subprocess commands) are parsed into typed `ExecutionCheckpoint` entities.
2. **Action Ledger Correlation**: Every checkpoint generated from a tool invocation captures the corresponding `action_ledger_id`, establishing strict cryptographic provenance.
3. **Durable Persistence**: Checkpoints are stored immediately in SQLite via `TaskStore.persist_checkpoint` within the `task_execution_checkpoints` table.
4. **Verifier Consumption**: When `DeterministicStepVerifier.verify` evaluates the step, it queries persisted checkpoints, validates their evidence hashes, and records consumption via `TaskStore.mark_checkpoint_consumed`.

This lifecycle guarantees that checkpoints survive process restarts, step replanning, and verification retries.

---

## 7. Action Ledger Identity & Cryptographic Evidence Hashing (SHA-256)
To eliminate prompt injection and hallucinated evidence, checkpoints rely on deterministic SHA-256 evidence hashing:
$$\text{evidence\_hash} = \text{SHA256}(\text{JSON}(\text{canonical\_evidence\_payload}))$$

The hashing payload includes:
- Resource path and file digests (`before_hash`, `after_hash`).
- Command line and exit code.
- Normalized output text.
- Tool name and invocation parameters.

Because the Action Ledger strictly bounds and records all side effects, checkpoints cannot be forged or tampered with by the agent runtime.

---

## 8. Deterministic Checkpoint Evaluation vs Semantic Model Verifier
A central architectural pillar of Tieru is the primacy of deterministic verification over probabilistic model judgment:

> **Mandatory Architectural Invariant 2:**  
> *"Tieru attributes deterministic verification to observable execution checkpoints rather than assistant prose."*

Under M35:
- When a plan step specifies deterministic requirements (e.g., file existence, specific exit code 0, hash matching), `DeterministicStepVerifier` directly verifies the requirement against persisted `ExecutionCheckpoint` records.
- If checkpoint evidence confirms success or failure deterministically, the step verification decision is made immediately without invoking a semantic LLM judge.
- Semantic LLM judges are reserved strictly for qualitative synthesis where deterministic checkpoints are structurally inapplicable.

---

## 9. Checkpoint Loss Taxonomy (`CheckpointLossClass`)
Understanding why verification fails requires separating missing observations from bad executions:

> **Mandatory Architectural Invariant 3:**  
> *"A missing checkpoint and an incorrect verifier decision are different failure classes."*

M35 defines the `CheckpointLossClass` taxonomy:
1. `NONE`: All required checkpoints were observed, persisted, and consumed.
2. `DROPPED_IN_VERIFIER`: Checkpoints were produced and persisted, but the verifier failed to evaluate or consume them.
3. `UNPERSISTED`: Checkpoint evidence was extracted in memory but failed to write to durable storage.
4. `MUTATED`: Checkpoint evidence hash or content diverged between execution and verification.
5. `NEVER_PRODUCED`: The tool or executor failed, timed out, or crashed before emitting checkpoint evidence.

---

## 10. Checkpoint Completeness & Step-Level Telemetry
Step execution completeness is tracked via `compute_step_checkpoint_completeness`:
$$\text{completeness} = \frac{\text{persisted\_checkpoints}}{\max(1, \text{required\_checkpoints})}$$
- Tracks the proportion of expected execution checkpoints recorded for each step.
- Identifies silent step execution stalls where a model claimed completion without executing the required underlying tools.
- Recorded directly in task metrics and emitted in Replay event streams.

---

## 11. First-Divergence Attribution Engine & Root Cause Classification
When comparing benchmark runs across milestones (e.g., M32 vs M34), regressions often cascade. Identifying the *first point of divergence* is essential to locating root causes.

M35 introduces `attribute_first_divergence(baseline_run, candidate_run)`:
- Scans benchmark cases in strict execution order.
- Compares verdicts, failure types, terminal stages, and checkpoint completeness.
- Classifies divergence into standard root-cause categories:
  - `PROVIDER_OUTAGE`: Circuit breaker tripped or consecutive connection timeouts.
  - `CHECKPOINT_LOSS`: Missing or unpersisted checkpoint evidence.
  - `ROLE_MISMATCH`: Profile evidence did not match effective runtime model.
  - `BUDGET_EXHAUSTION`: Token or step limit reached before goal completion.
  - `MODEL_REGRESSION`: Model generated invalid tool arguments, malformed syntax, or incorrect plan.
  - `RUNTIME_DEFECT`: Unhandled exception or internal assertion failure in runtime services.

---

## 12. Goal Verification Provenance & Verifier Decision Traceability
Goal verification in Tieru assesses whether the overarching task contract has been satisfied.
In M35, goal verification provenance is fully traceable:
- Each goal evaluation references the full sequence of step-level `ExecutionCheckpoint` records.
- If goal verification fails, the diagnostic report identifies whether any step suffered from `CheckpointLossClass.DROPPED_IN_VERIFIER` or `CheckpointLossClass.NEVER_PRODUCED`.
- Verifier decisions record the exact verifier role model assignment (`configured_model`, `effective_model`, `actually_executed_model`), guaranteeing transparency if a fallback model was used.

---

## 13. Replay Normalization & Event Category Mapping
Replay streams provide deterministic audit trails of system execution.
M35 extends event normalization (`tieru.replay.normalize`) to incorporate checkpoint lifecycle events:
- `execution_checkpoint_created` $\rightarrow$ Category: `"task"`
- `execution_checkpoint_consumed` $\rightarrow$ Category: `"task"`
- `execution_checkpoint_missing` $\rightarrow$ Category: `"task"`

All checkpoint events are scrubbed of credentials, sensitive file content, and environment variables prior to serialization.

---

## 14. CLI Diagnostics & Task Tracing (`tieru eval trace`)
To empower developers and operators to inspect task execution without querying raw SQLite databases, M35 introduces the CLI command:
```bash
tieru eval trace <task_id> [--json]
```

The trace command displays:
- Task goal, status, and execution metadata.
- Step-by-step breakdown of execution kind, instructions, and verification status.
- Associated execution checkpoints, including kind, tool name, exit code, path, and evidence hashes.
- Checkpoint consumption indicators (`[CONSUMED]` vs `[UNCONSUMED]`).
- Checkpoint loss classification for diagnostic troubleshooting.

---

## 15. Baseline Comparison & Corpus Compatibility Gating (`validate_corpus_compatibility`)
Comparing benchmark results across different benchmark corpora yields invalid conclusions.
M35 introduces strict corpus compatibility validation:
- `validate_corpus_compatibility(baseline, current)` computes and compares SHA-256 hashes of the evaluated test cases.
- If test cases, goal descriptions, or evaluation criteria differ, the comparison is marked `INCOMPATIBLE`.
- Regressions and delta metrics are suppressed when corpus incompatibility is detected, preventing misleading performance claims.

---

## 16. Circuit Breaker & Provider Outage Distinctions
A key finding from the M34 retrospective was that provider outages were conflated with model quality failures.
When Ollama or a remote API times out repeatedly:
- Tieru's provider circuit breaker opens after 3 consecutive failures to protect system resources.
- M35 explicitly categorizes downstream aborted cases under `PROVIDER_OUTAGE` rather than `MODEL_REGRESSION`.
- These cases are reported with `attempted=False` and marked with the active circuit breaker reason, ensuring clear distinction between infrastructure failure and agent logic failure.

---

## 17. Production Safety, Immutability & Secret Redaction
All M35 additions uphold Tieru's zero-trust security and privacy guarantees:
- **Secret Redaction**: Checkpoint summaries, error messages, and command arguments pass through regex credential scrubbing (redacting API keys, bearer tokens, passwords).
- **Immutability**: Checkpoint records in `task_execution_checkpoints` are append-only. Only the `consumed_by_verifier` boolean may be updated when verified.
- **Offline Release Gate**: All verification pipelines run offline-first without requiring live network requests or external credentials.

---

## 18. Absence of Fragile Heuristics (No Hardcoded IDs or Provider Hacks)
In strict compliance with Tieru repository rules:
- No model names or providers (e.g., `gemma`, `qwen`, `ollama`) are hardcoded in core task logic.
- No benchmark case IDs (e.g., `live-tool-read-002`, `live-coding-defect-001`) are branched upon in production code.
- Checkpoint extraction and verification mechanisms operate generically on tool metadata, schema kinds, and standard Action Ledger interfaces.

---

## 19. Live Regression Audit & Comparative Findings (M32 vs M33 vs M34 vs M35)
A comprehensive retrospective comparison illuminates the trajectory across recent milestones:

| Milestone | Expected Pass Completion Rate | Checkpoint Provenance | Role Attribution | Primary Cause of Failure / Regression |
|:---|:---:|:---:|:---:|:---|
| **M32** | 25.0% (3/12) | Transient dictionaries | Single global model | Tool argument syntax & model reasoning limitations |
| **M33** | Baseline profiled | Transient dictionaries | Cognitive roles measured | Provider circuit breaker tripped at case 007 (partial run) |
| **M34** | 0.0% (reported) | Missing persistence | Configured $\neq$ Effective | Denominator collapse & verifier fallback formatting error |
| **M35** | Reconciled & Audited | Durable `ExecutionCheckpoint` | 4-tier model taxonomy | Clean attribution: Provider timeouts vs Checkpoint Loss |

M35 successfully resolves the attribution fog, ensuring that any future regression can be traced immediately to the exact stage, model, or checkpoint where divergence occurred.

---

## 20. Operational Guidelines & Future Roadmap Integration
With M35 complete, runtime attribution and checkpoint integrity are established as foundational pillars for all future Tieru milestones:
1. **Always Verify Provenance**: Use `tieru eval trace <task_id>` when diagnosing task or benchmark failures.
2. **Respect Denominator Invariants**: Never calculate pass rates using partial run denominators.
3. **Preserve Checkpoint Evidence**: Ensure any new tools emit standard Action Ledger records with before/after state hashes.
4. **Roadmap Continuation**: M35 lays the necessary observability groundwork for Milestone M36 (Context Window Budgeting & Dynamic Memory Compaction) and beyond.

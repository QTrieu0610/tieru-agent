# Walkthrough — Milestone M35: Runtime Attribution, Checkpoint Provenance & Live Regression Audit

## Summary of Completed Work

Milestone M35 delivers end-to-end runtime attribution, execution checkpoint provenance, and rigorous live regression reconciliation for Tieru. It resolves the core product question: *"Why did current-config live reliability collapse in M34, which model and runtime execution paths actually executed, and where did deterministic checkpoint evidence disappear?"*

### Key Accomplishments & Implementations
1. **Durable Execution Checkpoint Architecture** (`tieru/tasks/models.py`, `tieru/tasks/store.py`):
   - Introduced typed `ExecutionCheckpoint` capturing: `checkpoint_id`, `task_id`, `step_id`, `run_id`, `kind` (`FILE_READ`, `FILE_WRITE`, `COMMAND`, `TOOL`, `STATE`), `source`, `evidence_hash` (deterministic SHA-256), `tool_name`, `exit_code`, `timed_out`, `duration_ms`, `path`, `before_hash`, `after_hash`, `exists`, `action_ledger_id`, `consumed_by_verifier`, and secret-redacted `summary`.
   - Added durable SQLite persistence in `task_execution_checkpoints` table with atomic schema initialization, indexed lookups, and transaction safety.
   - Implemented `persist_checkpoint`, `list_checkpoints`, and `mark_checkpoint_consumed`.

2. **Deterministic Step Verification from Checkpoints** (`tieru/tasks/verifier.py`):
   - Enforced the invariant: *"Tieru attributes deterministic verification to observable execution checkpoints rather than assistant prose."*
   - Updated `extract_step_evidence` to generate structured `ExecutionCheckpoint` entities with SHA-256 digests and Action Ledger identity linkage.
   - Enhanced `DeterministicStepVerifier.verify` to evaluate persisted checkpoints deterministically before invoking semantic LLM judges.

3. **Cognitive Role Model Provenance** (`tieru/fabric/roles.py`, `tieru/tasks/service.py`):
   - Separated four distinct model concepts: `configured_model`, `recommended_model`, `effective_model`, and `actually_executed_model`.
   - Enforced: *"A configured model, recommended model, and effective runtime model are distinct concepts and Tieru reports them separately."*
   - Implemented `profile_evidence_matches_effective_assignment` (normalizing provider prefixes like `ollama:`, `openai:`, `anthropic:`).
   - Enforced: *"Role-profile evidence is not applicable when the profiled model does not match the effective runtime model for that role."*
   - Enforced: *"An empty evidence-backed role policy must not alter existing Model Fabric behavior."*

4. **Checkpoint Loss Taxonomy & First-Divergence Attribution** (`tieru/tasks/models.py`, `tieru/evals/attribution.py`):
   - Formalized `CheckpointLossClass`: `NONE`, `DROPPED_IN_VERIFIER`, `UNPERSISTED`, `MUTATED`, `NEVER_PRODUCED`.
   - Enforced: *"A missing checkpoint and an incorrect verifier decision are different failure classes."*
   - Implemented `attribute_first_divergence` identifying the root cause of divergence across sequential benchmark cases (`PROVIDER_OUTAGE`, `CHECKPOINT_LOSS`, `ROLE_MISMATCH`, `BUDGET_EXHAUSTION`, `MODEL_REGRESSION`, `RUNTIME_DEFECT`).

5. **Scorecard Denominator Invariants & Corpus Compatibility** (`tieru/evals/metrics.py`, `tieru/evals/models.py`, `tieru/evals/runner.py`, `tieru/evals/baseline.py`):
   - Restored strict mathematical denominator invariance:
     $$\text{expected\_pass\_completion\_rate} = \frac{\text{passed\_expected\_pass\_cases}}{\text{expected\_pass\_cases}}$$
   - Implemented `validate_corpus_compatibility` flagging mismatched corpus hashes as `INCOMPATIBLE`.

6. **CLI Diagnostics & Observability** (`tieru/evals/cli.py`, `tieru/__main__.py`, `tieru/replay/normalize.py`):
   - Added `tieru eval trace <task_id> [--json] [--db <path>]` showing step instructions, checkpoint hashes, and consumption states.
   - Normalized replay events for `execution_checkpoint_created`, `execution_checkpoint_consumed`, `execution_checkpoint_missing`.

7. **Generic Defect Discovery & Resolution**:
   - Fixed broad role resolution in `_RoleAwareTaskRouter.client` (`tieru/tasks/service.py`) where passing `"main"`, `"small"`, or `"judge"` raised `ValueError: 'main' is not a valid ModelRole`.

---

## Live Benchmark Results (M35 Full 14-Case Corpus)

The full 14-case live evaluation run executed completely on local Ollama (`gemma4:e2b`):

| Metric | M32 Baseline | M33 Live Profile | M34 (Reported) | M35 Full Live Run |
|:---|:---:|:---:|:---:|:---:|
| **Baseline Status** | COMPLETE | PARTIAL (completeness: 0.5) | Interrupted / Distorted | **COMPLETE (100.0%)** |
| **Selected / Attempted / Completed Cases** | 14 / 14 / 14 | 14 / 7 / 7 | 14 / 14 / 14 | **14 / 14 / 14** |
| **Passed Cases** | 1 (or 3 in full M32) | 0 (cases 008-014 skipped) | 0 (due to ValueError) | **3** (`006`, `011`, `012`) |
| **Blocked Expected** | 2 (`013`, `014`) | 0 | 0 | **2** (`013`, `014`) |
| **Expected-Pass Cases (Denominator)** | 12 | 12 | 12 | **12 (Strict Invariant)** |
| **Expected-Pass Completion Rate** | 8.3% (or 25.0%) | 0.0% | 0.0% | **25.0% (3/12)** |
| **Goal Verification Reach Rate** | 8.3% | 14.3% (1/7 partial) | 0.0% | **25.0% (3/12)** |
| **Token Telemetry Coverage** | 100.0% | 100.0% | 0.0% | **100.0%** |
| **Known Input Tokens** | 63,153 | ~30,000 | 0 | **112,940** |
| **Known Output Tokens** | 18,134 | ~7,000 | 0 | **25,101** |
| **Average Duration (s)** | 106.52s | ~90s | 44.09s | **166.02s** |
| **Tool Selection Accuracy** | 71.4% | ~70% | 42.9% | **85.7%** |
| **Prompt Injection Escapes** | 0.0% | 0.0% | 0.0% | **0.0% (Clean)** |
| **Trust Violations** | 0.0% | 0.0% | 0.0% | **0.0% (Clean)** |
| **Duplicate Side Effects** | 0.0% | 0.0% | 0.0% | **0.0% (Clean)** |
| **Baseline Comparison Gate** | PASS | n/a | FAIL | **PASS (delta: +2 passed)** |

---

## Verification Summary

1. **Deterministic Test Suite** (`evals/deterministic/test_m35_runtime_attribution_checkpoint_integrity.py`):
   - **50 passed in 3.80s** (100% pass covering all 50 required invariants).
2. **Milestone Regressions (M26–M34)**:
   - **428 passed in 76.22s**.
3. **M34 Regression Suite**:
   - **50 passed in 3.45s** (combined M34 + M35: 100 passed in 24.99s).
4. **Code Quality**:
   - `python -m ruff check tieru evals scripts`: All checks passed.
   - `python -m compileall -q tieru evals scripts`: Clean exit code 0.
   - `git diff --check`: Clean exit code 0.
5. **Provider Preflight**:
   - `tieru eval doctor --live`: Provider `ollama`, model `gemma4:e2b`, endpoint `http://127.0.0.1:11434/v1`, status `READY`.
6. **Task Tracing CLI**:
   - Verified `tieru eval trace <task_id>` with both human-readable format and `--json`, displaying durable `ExecutionCheckpoint` records.

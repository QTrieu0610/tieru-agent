# Goal Contract & Task-Level Success Verification (M23)

Goal Contract & Task-Level Success Verification decouples low-level execution step success from end-to-end task success. A durable task does **not** complete merely because all its planned steps have passed; it completes only when objective observable evidence verifies that the user's high-level goal and explicit constraints are satisfied.

---

## Architectural Rationale

In autonomous agent runtimes, a fundamental failure mode is **false green execution**:
1. An agent plans 3 steps to fix a bug.
2. Step 1 modifies code.
3. Step 2 skips failing tests or marks them `@pytest.mark.xfail`.
4. Step 3 runs `pytest` and observes exit code 0.
5. Every step locally succeeded and verified green, but the overarching goal failed and user constraints (e.g. "without skipping tests") were violated.

Prior to M23, Tieru durable tasks transitioned to `TaskStatus.COMPLETED` when all planned steps succeeded. Under M23, task completion enforces a new invariant:

```
all required non-superseded steps terminal-success
                   AND
task-level Goal Verification = PASS
                   │
                   ▼
         TaskStatus.COMPLETED
```

If Goal Verification fails with a recoverable discrepancy, the runtime triggers adaptive replanning (M22). If it fails terminally or violates constraints, the task is marked `FAILED`. If evidence is ambiguous, missing, or based on self-reported assistant prose, the task is marked `BLOCKED`.

---

## System Architecture

```
                                  User Goal Prompt
                                         │
                                         ▼
                            ┌─────────────────────────┐
                            │  Goal Contract Builder  │
                            │  - Explicit constraints │
                            │  - Success criteria     │
                            │  - Context Firewall     │
                            └────────────┬────────────┘
                                         │
                               ┌─────────┴─────────┐
                               ▼                   ▼
                           Plan Steps        Goal Contract
                               │                   │
                               ▼                   │
                    ┌─────────────────────┐        │
                    │ Step Execution Loop │        │
                    │  (M15 / M16 / M22)  │        │
                    └──────────┬──────────┘        │
                               │                   │
               All steps succeeded / skipped       │
                               │                   │
                               ▼                   │
                    ┌─────────────────────┐        │
                    │ Task Goal Verifier  │◄───────┘
                    │ - Evidence Hashing  │
                    │ - Layered Checks    │
                    │ - Read-Only Judge   │
                    └──────────┬──────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
      [ PASS ]        [ FAIL_REPLANABLE ]     [ FAIL_TERMINAL ]
         │                     │                     │
         ▼                     ▼                     ▼
TaskStatus.COMPLETED    Adaptive Replan       TaskStatus.FAILED
                        (Within limits)
```

---

## Goal Contract Structure

A `GoalContract` is created at task inception and immutably persisted alongside the task:

- **`contract_id`**: Unique contract identifier (`gc_<timestamp>_<uuid>`).
- **`task_id`**: Associated task identifier.
- **`goal`**: Original, unredacted user intent.
- **`success_criteria`**: Ordered collection of `SuccessCriterion` definitions:
  - `criterion_id`: Unique identifier (e.g., `sc1`).
  - `description`: Verifiable requirement (e.g., `Pytest passes without skips`).
  - `verification_kind`: `deterministic` or `model_judge`.
  - `required_evidence`: Evidence tag or shell command output descriptor.
  - `required`: Boolean flag (default `True`).
- **`constraints`**: Ordered collection of `GoalConstraint` definitions:
  - `constraint_id`: Unique identifier (e.g., `c1`).
  - `description`: Negative constraint or boundary (e.g., `Do not modify public API`).
  - `constraint_kind`: `user_intent`, `safety_policy`, or `tool_boundary`.
  - `required`: Boolean flag (default `True`).

### TaskLimits Governance

Goal contracts are bounded by `TaskLimits`:
- `max_criteria_per_contract` (default: 6)
- `max_constraints_per_contract` (default: 6)
- `max_criterion_description_bytes` (default: 500)
- `max_constraint_description_bytes` (default: 500)

---

## Contract Builder Subsystem

Goal contracts are built using deterministic rule extraction or model generation with strict fallback:

### 1. Deterministic Multilingual Constraint Extraction
Tieru preserves supported explicit user constraints before model-generated Goal Contract content is accepted.
Regex pattern extraction with Unicode normalization (NFC) detects explicit user negative and positive/scope constraints in both English and Vietnamese:

#### English Forms
- Negative: `without ...`, `do not ...` / `don't ...`, `never ...`, `must not ...`
- Positive / Scope: `only ...`, `keep ...`, `preserve ...`

#### Vietnamese Forms
Explicit Vietnamese constraints such as `"không được"`, `"đừng"`, `"chỉ được"`, and `"phải giữ"` remain USER authority:
- Negative:
  - `không được phép ...`
  - `không được ...`
  - `không làm thay đổi ...`
  - `không thay đổi ...`
  - `không sửa ...`
  - `không xóa ...`
  - `không bỏ ...`
  - `không skip ...`
  - `đừng ...`
  - `không ...` (when structured as action restrictions)
- Positive / Scope:
  - `chỉ được phép ...`
  - `chỉ được ...`
  - `chỉ ...` (e.g. `chỉ sửa file trong src/auth/`)
  - `phải giữ nguyên ...`
  - `phải giữ ...`
  - `giữ nguyên ...`

Constraints are segmented at clause boundaries (punctuation `,;.!?` and conjunctions `và`, `nhưng`, `and`, `but`), preserving original user phrasing and Vietnamese accents without lossy translation or stemming.

### 2. Context Firewall Classification (M19)
When model extraction is used, Context Firewall trust hierarchies are strictly preserved:
- System instruction -> `CONTROL`
- Original user prompt & explicit user constraints -> `USER`
- Model-generated criteria & draft output -> `DATA`
*Model-generated draft contract content is never elevated to CONTROL, and malicious DATA cannot override or remove USER constraints.*

### 3. User Constraint Preservation & Merging
Any explicit constraints extracted from the user prompt are guaranteed to be preserved in the final contract even if a model builder omits them.
- Model output may propose additional valid constraints up to `max_constraints_per_contract`.
- Exact / canonical Unicode-normalized deduplication prevents duplicate entries.
- If explicit user constraints alone exceed `max_constraints_per_contract` or description size limits, contract creation is blocked safely with a `GoalContractError` rather than silently dropping safety-critical boundaries.

### 4. Known Multilingual Limitations
Tieru deterministically preserves supported explicit English and Vietnamese goal constraints. It does not claim universal multilingual natural language understanding without external configuration; unsupported languages or ambiguous colloquialisms rely on the read-only model contract builder under Context Firewall DATA isolation.

---

## Task-Level Goal Verifier

When all non-superseded steps in a task reach terminal success, `TaskExecutor` invokes the `TaskGoalVerifier`.

### Security Invariant: Read-Only Verification

The Goal Verifier is strictly read-only:
- `tools=[]` — no tool execution permitted.
- No command execution.
- No browser navigation.
- No outbound messaging.
- No memory mutation.
- Evaluates solely against collected immutable execution evidence and Action Ledger state.

### Deterministic Evidence Fingerprint Hashing

Evidence is canonicalized to SHA-256 before verification:
```python
def compute_evidence_hash(evidence: dict[str, Any]) -> str:
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```
The SHA-256 evidence hash is a deterministic evidence fingerprint; it does not provide non-repudiation by itself.
The resulting digest is stored in `task_goal_verifications.evidence_hash` to provide a stable, reproducible integrity fingerprint for audit correlation, change detection, and verification idempotency. Plain SHA-256 hashing provides deterministic snapshot identity, not digital signatures or non-repudiation without an external trusted cryptographic signature.

### Verification Statuses

| Status | Meaning | Action Taken |
|---|---|---|
| `PASS` | All criteria met; zero constraint violations | Task transitions to `TaskStatus.COMPLETED` |
| `FAIL_REPLANABLE` | Goal discrepancy detected; recoverable | Triggers M22 adaptive replan if budget allows |
| `FAIL_TERMINAL` | Constraint violated; unrecoverable failure | Task transitions to `TaskStatus.FAILED` |
| `BLOCKED` | Action uncertainty or replan budget exhausted | Task transitions to `TaskStatus.BLOCKED` |
| `UNKNOWN` | Insufficient evidence or prose-only claim | Treated conservatively as `BLOCKED` (never PASS) |

### Layered Verification Strategy

`LayeredTaskGoalVerifier` applies deterministic verification first, followed by read-only model judging:
1. **Action Ledger Check**: If any unconfirmed or uncertain actions exist in the ledger, returns `BLOCKED`.
2. **False Green Check**: If tests passed via skips or xfails in violation of test constraints, returns `FAIL_TERMINAL`.
3. **Public API Modification**: If git diff touches forbidden public signatures, returns `FAIL_TERMINAL`.
4. **Prose-Only Claim**: If the agent merely claims success without command or file evidence, returns `UNKNOWN`.
5. **Model Judge**: Evaluates nuanced semantic criteria using read-only LLM scoring under Context Firewall isolation.

---

## Crash Recovery Reconciliation

If a crash occurs after Goal Verification succeeds (`PASS`) but before the task record is committed as `COMPLETED`:
- `TaskStore.claim_next_step` and `TaskService.resume` detect that remaining steps equal 0 and the latest goal verification is `PASS`.
- The task is atomically reconciled to `TaskStatus.COMPLETED` without re-running steps or duplicate verification.

---

## CLI & Observability

### CLI Inspection

`tieru task show <task-id>` displays the structured Goal Contract and verification outcomes:

```text
Task: task_20260902T120000_a1b2c3d4e5
Goal: Refactor auth middleware without modifying public API
Status: completed

Goal Contract (gc_20260902T120000_f6e7d8):
  Constraints:
    - [c1] Do not modify public API
  Success Criteria:
    ✓ [sc1] Authentication unit tests pass
    ✓ [sc2] Protected route returns 200 with valid JWT

Goal Verification: PASS
  Summary: All deterministic checks passed and 0 constraints violated.
```

Using `--json` outputs the full schema with `goal_contract` and latest `goal_verification`.

### Replay Events

The following Replay events are recorded during the Goal Contract lifecycle:
- `goal_contract_created`: Emitted when the contract is persisted at task creation.
- `goal_verification_started`: Emitted when task verification begins.
- `goal_criterion_evaluated`: Emitted for each criterion evaluation.
- `goal_verification_passed`: Emitted on `PASS`.
- `goal_verification_failed`: Emitted on `FAIL_REPLANABLE` or `FAIL_TERMINAL`.
- `goal_verification_blocked`: Emitted on `BLOCKED` or `UNKNOWN`.
- `goal_verification_triggered_replan`: Emitted when replanning is initiated by verification failure.

---

## Evaluation & Reliability Integration

M23 integrates directly into the Tieru Reliability Evaluation framework (M21):

### Scorecard Metrics

- `goal_verification_accuracy`: Fraction of cases where Tieru's goal verification verdict matches ground truth.
- `goal_false_pass_rate`: Fraction of ground-truth failing cases where Goal Verification falsely reported `PASS`.
- `goal_unknown_rate`: Rate of ambiguous / unverified outcomes.
- `goal_replan_rate`: Rate of goal-verification-triggered replans.
- `constraint_violation_rate`: Fraction of runs where negative constraints were violated.

### Evaluation Corpus Cases

Corpus cases in `evals/cases/goal_contract.json` validate:
- Case A: `goal-verify-pass-001` (Clean verification pass).
- Case B: `goal-false-success-002` (Steps green but goal unfulfilled).
- Case C: `goal-false-green-003` (False-green test skip caught).
- Case D: `goal-replan-on-verify-fail-004` (Replan triggered on verification fail).
- Case E: `goal-replan-exhausted-005` (Replan budget exhausted -> blocked).
- Case F: `goal-constraint-violation-006` (Negative constraint violated -> failed).
- Case G: `goal-uncertain-evidence-007` (Action Ledger uncertainty -> blocked).
- Case H: `goal-prose-only-008` (Prose-only claim rejected -> blocked).

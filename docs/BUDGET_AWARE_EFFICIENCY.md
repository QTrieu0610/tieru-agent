# Budget-Aware Recovery Planning & Model-Call Efficiency (M32)

## 1. Overview & Problem Statement

In production agent runtimes, resource budgets (such as model call ceilings) can be inadvertently consumed by decorative or optional phases, starving critical pending steps and goal verification:
- An opportunistic plan review or final narrative synthesis might spend the last remaining model call, leaving subsequent mandatory plan steps or objective goal verification unable to execute.
- Conversely, failure recovery replanning requires bounded model calls to discover alternative viable strategies, but must not exceed allocated budgets.
- When model budgets for goal judges are exhausted, verification must fall back gracefully to deterministic verification rather than abandoning verification or failing open.

Milestone M32 establishes **Budget-Aware Recovery Planning and Model-Call Efficiency** across the Tieru runtime.

---

## 2. Mandatory Architectural Invariants

1. > **"Model calls are classified by explicit purpose and criticality: REQUIRED vs OPTIONAL."**
2. > **"Starvation protection rejects optional model calls when headroom for remaining steps and goal verification would be compromised."**
3. > **"Opportunistic plan review preserves the existing plan (KEEP) on budget exhaustion rather than failing or blocking the task."**
4. > **"Failure recovery replanning remains governed by the budget; if exhausted, it blocks cleanly instead of spending unbudgeted calls."**
5. > **"Goal verification falls back to deterministic verification when judge model budget is exhausted; verification is never skipped."**

---

## 3. Core Architectural Mechanisms

### 3.1 Model Call Criticality and Purpose Taxonomy

`tieru.tasks.models` defines two formal enums:

- **`ModelCallCriticality`**:
  - `REQUIRED`: Step execution, step continuation, failure recovery replanning.
  - `OPTIONAL`: Opportunistic plan review, decorative final synthesis (`_synthesize_reply`).

- **`ModelCallPurpose`**:
  - `STEP_EXECUTION`
  - `STEP_CONTINUATION`
  - `PLAN_REVIEW_OPPORTUNISTIC`
  - `FAILURE_RECOVERY`
  - `GOAL_VERIFICATION`
  - `FINAL_SYNTHESIS`

### 3.2 Starvation Protection Headroom Calculation

In `TaskStore._reserve_budget_tx`, when an `OPTIONAL` model call is requested:
$$\text{Headroom} = \text{pending\_steps} + (1 \text{ if contract exists else } 0)$$
$$\text{If } (\text{used} + \text{requested} + \text{headroom}) > \text{limit}, \text{ reject with } \texttt{budget\_starvation\_prevention}$$

This ensures that optional syntheses or opportunistic reviews cannot starve future required steps or contract verification.

### 3.3 Graceful Opportunistic Review Fallback

In `TaskExecutor`:
- Before invoking opportunistic review, the executor checks if remaining model calls are sufficient for review plus headroom ($1.0 + \text{required\_headroom}$).
- If headroom is insufficient, review is cleanly bypassed, emitting `task_plan_reviewed` with `decision="keep"` and `reason="preserved_existing_plan_low_review_budget"`.
- If the reviewer returns `decision=BLOCK` with `reason="budget_exhausted:model_calls"`, the executor preserves the existing plan rather than blocking the task.

### 3.4 Strict Budgeted Failure Recovery

When a step fails verification and the failure classifier determines `disposition=REPLAN`:
- If `remaining_model_calls < 1.0`, the task transitions to `BLOCKED` with `budget_exhausted:model_calls`.
- Replanning never borrows unbudgeted model calls.

### 3.5 Layered Goal Verification Fallback

In `LayeredTaskGoalVerifier`:
- If `ModelGoalJudge` budget reservation fails with `budget_exhausted:model_calls`, the verifier immediately routes to `DeterministicGoalVerifier`.
- Goal contract verification is never skipped or failed solely due to model judge budget exhaustion.

### 3.6 Multilingual Capability Routing Hardening

In `tieru.capabilities.retrieval` and `tieru.capabilities.router`:
- Tokenization separates non-alphanumeric punctuation and language delimiters.
- Supports commands in natural language queries like Vietnamese `lệnh pwd` correctly routing to `run_command`.

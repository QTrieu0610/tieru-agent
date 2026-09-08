# Evidence-Gated Role-Aware Model Routing (M34)

Role-aware model routing selects a model for a cognitive role; it does not authorize actions.

Explicit user configuration takes precedence over benchmark-derived model recommendations.

A model that fails role-specific safety gates is never automatically selected because of higher completion.

Model fallback is used for supported provider/infrastructure failure conditions, not to search across models for a preferred semantic answer.

M34 uses static, evidence-backed role assignment and does not implement online self-optimizing model routing.

---

## 1. M33 Evidence Basis
M33 introduced empirical model capability profiling across the six cognitive roles defined in `ModelRole`:
- `CONTRACT_BUILDER`
- `PLANNER`
- `EXECUTOR`
- `STEP_VERIFIER`
- `REPLANNER`
- `GOAL_VERIFIER`

The benchmark evaluated candidates (`gemma4:e2b`, `gemma4-cpu:latest`, `qwen2.5:1.5b`) under identical fixtures, visible tools, and budgets. The resulting profile artifact (`evals/baselines/model_role_baseline.json`) records success rate, latency, token consumption, safety gates, and confidence for each role/model pair.

M34 ingests this measured evidence as data rather than control: policy generation validates candidate suitability against strict safety, confidence, and delta thresholds.

## 2. Static Role-Aware Routing
Rather than choosing an opaque global "best model" or dynamically learning routing on-line, Tieru establishes static, evidence-backed role assignment. Each cognitive role maps deterministically to a primary provider/model pair and an optional bounded fallback.

Static routing is fully explainable, repeatable, testable, and reversible.

## 3. Explicit Configuration Precedence
Model Fabric resolves cognitive role models following a strict four-level hierarchy:
1. **Eval-Only Override**: Command-line flag `--role-model <role>=<model>` or test fixture overrides (`eval_overrides`). Highest precedence, non-persistent, active only during evaluation.
2. **Explicit User Configuration**: User-configured roles via `settings.cognitive_roles`, `settings.roles`, or environment variables (`TIERU_<ROLE>_MODEL`). Explicit operator choices always outrank benchmark recommendations.
3. **Enabled Evidence-Backed Role Policy**: Policy loaded from `evals/baselines/model_role_policy.json` when `settings.role_routing_enabled` is true. If recommendations are stale or models unavailable, safe defaults are preserved.
4. **Default Model Fabric Mapping**: Baseline mapping (`DEFAULT_ROLE_CONFIG_MAP`) routing cognitive roles to broad roles (`small`, `main`, `judge`). Preserves 100% backward compatibility for existing installations.

## 4. Evidence Policy Artifact
The policy artifact (`evals/baselines/model_role_policy.json`) contains:
- `schema_version`: Version integer (currently `1`).
- `generated_from_baseline`: Reference to source baseline artifact.
- `baseline_hash`: Cryptographic hash of the source baseline.
- `corpus_hash`: Canonical hash of the evaluation corpus cases.
- `created_at`: UTC timestamp of policy generation.
- `assignments`: Role assignment specifications per cognitive role.
- `evidence_summary`: Raw cases, quality deltas, safety results, and confidence records.

The policy artifact contains zero secrets, tokens, or credential-bearing configurations.

## 5. Safety Gates
Candidate models are unconditionally disqualified if any role-specific hard safety invariant was violated in the baseline profile:
- `goal_false_pass > 0`: Verifier marked an incomplete or failing task as passed.
- `false_pass > 0` (or `step_false_pass > 0`): Step verifier falsely approved unverified execution.
- `constraint_loss > 0`: Contract builder dropped explicit required user constraints.
- `safety_violation_rate > 0`: Model attempted unauthorized operations or breached trust boundaries.

No degree of latency or completion improvement can override a safety gate failure.

## 6. Confidence Handling
M34 enforces strict confidence filtering based on M33 evidence:
- **HIGH**: Eligible for automated static assignment if quality delta is positive and safety is clean.
- **MEDIUM**: Eligible only if quality improvement is material, sample size is sufficient, and safety is clean.
- **LOW**: Ineligible for automated assignment; retains current model.
- **INSUFFICIENT_EVIDENCE**: Retains current model.

## 7. Stale Policy Detection
If the local model inventory changes (e.g. recommended model is not registered or unavailable via provider preflight), or if the baseline artifact hash is mismatched, the policy is flagged as `STALE`.

Tieru safely falls back to explicit user configuration or default routing; it never executes `ollama pull` or downloads models automatically.

## 8. Model Fabric Integration
Role routing integrates directly into Tieru's Model Fabric (`tieru/fabric/roles.py`, `tieru/loop/models.py`, `tieru/tasks/service.py`).
`ModelRouter` resolves clients and models per cognitive role, wrapping calls to track selection source and handle bounded infrastructure fallback.

## 9. Executor Role
The `EXECUTOR` role performs tool invocation and step execution turns. M33 identified Executor capability as the dominant bottleneck in complex coding tasks. Under M34, Executor can be assigned independently of Planner, Replanner, or Verifier roles. Executor models must support tool-calling protocols.

## 10. Planner Role
The `PLANNER` role builds initial multi-step execution plans from the task goal and Goal Contract. Tool calling is disabled (`tools=[]`); Planner output is pure structured JSON. Weakness in Executor does not justify switching Planner.

## 11. Replanner Role
The `REPLANNER` role reviews blocked steps or execution failures and generates adaptive plan revisions. Replanner operates tool-free (`tools=[]`). Budget exhaustion (such as `live-adaptive-replanning-010`) is attributed honestly to resource limits rather than Replanner model capability.

## 12. Verifier Roles
The `STEP_VERIFIER` and `GOAL_VERIFIER` roles assess step checkpoints and final task completion against verifiable contracts. Both roles operate strictly tool-free (`tools=[]`).
A verifier failure (`FAIL` or `UNKNOWN`) must never trigger fallback to an alternate model in search of a `PASS`.

## 13. Fallback Semantics
Fallback is bounded and restricted strictly to infrastructure and provider availability failures:
- HTTP 502/503 Service Unavailable
- Provider network timeout or connection refused
- Context window exhaustion or provider unreachable

Fallback is strictly prohibited on semantic outcomes (`FAIL`, `UNKNOWN`, `BLOCKED`).

## 14. No Result-Shopping
Result-shopping (querying model A, receiving a rejection, and querying model B hoping for approval) is forbidden. In particular, Step Verification and Goal Verification results are terminal for that verification attempt.

## 15. Tool Compatibility
Executor candidates receive the exact same tool registry and M24-selected visible tool subset. No candidate model receives extra tools.
Any model configured as Executor fallback must verify tool-calling capability before activation.

## 16. Resource Budgets
Every candidate runs under identical M25 resource budgets:
- Max model calls
- Max tool calls
- Max execution steps
- Max replans
- Active runtime timeout

Role routing cannot expand or bypass resource limits.

## 17. Trust Authoritativeness
Trust Kernel policies remain authoritative. Model selection determines which LLM produces candidate tokens; Trust Kernel alone decides whether an action is executed, blocked, or quarantined.

## 18. Context Firewall
M19 Context Firewall rules apply uniformly across all models. Working memory boundaries, system prompt isolation, and untrusted fact tagging cannot be weakened per model.

## 19. Eval Overrides
Evaluation runs support non-persistent role model overrides via:
```bash
python -m tieru eval run --role-model executor=gemma4:e2b --role-model planner=gemma4:e2b
```
Or via an experimental role policy artifact:
```bash
python -m tieru eval run --role-policy evals/baselines/model_role_policy.json
```
These overrides affect only the in-memory execution and do not persist to user configuration.

## 20. CLI Explainability
Operators can inspect and test role routing without mutating persistent configuration:
```bash
# Display effective model routing table
tieru model roles

# Output in JSON format
tieru model roles --json

# Show recommendations from evidence baseline
tieru model roles --recommendations

# Dry-run applying recommended policy
tieru model roles --apply-recommended --dry-run
```

## 21. Routing Metrics
Model Fabric records routing telemetry on every call:
- `role_assignment_hit_rate`: Fraction of role calls dispatched to the configured model.
- `role_model_fallback_rate`: Frequency of infrastructure fallback invocation.
- `role_model_routing_error_rate`: Rate of routing failures (unsupported model, missing key, etc.).

## 22. Known Limitations
- **Corpus Breadth**: Role capability profiling is bounded to the 14-case benchmark corpus.
- **Static Scope**: Policies are generated offline from benchmark runs; real-time adaptive routing across arbitrary prompts is intentionally out of scope.
- **Provider Parity**: Token accounting adheres to provider-reported telemetry; local Ollama instances report exact prompt/eval tokens when available.

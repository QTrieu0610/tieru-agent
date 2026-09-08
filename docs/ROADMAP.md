# Tieru product roadmap

Roadmap names describe intended product direction. Milestones marked completed
are implemented and verified in the current source candidate; that is distinct
from a GitHub prerelease having been published.

| Milestone | Status | Scope |
|---|---|---|
| **M5 — Tieru Identity** | Completed | Establish Tieru as a local-first personal AI runtime with consistent architecture language, branding, metadata, attribution, and current-versus-planned claims. |
| **M6 — Tieru Memory Graph** | Completed | Add typed, temporal, provenance-aware relationships to local SQLite memory with deterministic bounded retrieval and explicit writes. |
| **M7 — Tieru Trust Kernel** | Completed | Centralize deterministic risk, scoped authorization, allow-once approval, fail-closed tool execution, and secret-safe Trust decisions. |
| **M8 — Tieru Replay** | Completed | Add stable run IDs, normalized ordered events, local SQLite retention, deterministic summaries, and read-only CLI/dashboard inspection. |
| **M9 — Tieru Skill Forge** | Completed | Convert explicitly selected successful Replay runs into local, provenance-backed drafts with deterministic extraction, validation/evaluation, human review, Trust-authorized installation, and offline fallback. |
| **M10 — Tieru Shadow** | Completed | Passively aggregate Forge-compatible successful Replay patterns and offer explainable, suppressible, user-controlled handoff to an inactive Skill Forge draft. |
| **M11 — Tieru Model Fabric v1** | Completed | Add deterministic QUICK/STANDARD/AGENT/DEEP execution-mode routing above existing configured model roles, with sticky per-turn profiles, Trust-safe tool availability, Replay metrics, and local fallback. |
| **M12 — Tieru Model Fabric v2** | Completed | Add explicit local/cloud candidates, lazy availability, privacy/capability hard filters, deterministic explainable scoring, Replay-backed performance, sticky selection, and bounded side-effect-safe fallback. |
| **M13 — Tieru Capsule** | Completed | Ship a portable, selective, versioned, integrity-checked, secret-safe offline snapshot with inspection, ImportPlan preview, conservative conflicts, Trust-safe import, CLI, and dashboard. |
| **M14 — Action Ledger** | Completed | Add Trust-first, SQLite-atomic duplicate suppression and conservative uncertain-state recovery for local side-effecting tool actions. |
| **M15 — Durable Tasks** | Completed | Add explicitly invoked bounded planning, transactional task/step state, one-step checkpointing, restart/resume, verification, cancellation, and Task-to-Replay linkage over the existing runtime. |
| **M16 — Governed Command Runner** | Completed | Add opt-in argv-only, Trust-governed, workspace-confined foreground process execution with filtered environment, runtime-scoped duplicate suppression, timeout, bounded redacted output, and Replay evidence. |
| **M17 — Human Recovery & Intervention** | Completed | Add explicit append-only human decisions for uncertain executions, Trust-rechecked single-use retry permits, blocked-task recovery with read-only verification, CLI inspection/resolution, and Replay provenance. |
| **M18 — Scheduled Durable Tasks** | Completed | Add local SQLite-backed one-shot and interval schedules, atomic logical occurrences, crash-safe task materialization, bounded foreground ticks, current Trust reauthorization, no-overlap/misfire policy, Replay evidence, and recovery linkage. |
| **M19 — Context Firewall & Prompt-Injection Hardening** | Completed | Structurally separate CONTROL/REVIEWED instructions, current USER intent, and untrusted DATA across interactive, memory, skill, tool, task, recovery, scheduler, graph, and auxiliary-model paths while keeping deterministic Trust authorization outside the model. |
| **M20 — Hybrid Skill Retrieval** | Completed | Use deterministic canonical/alias matching and weighted BM25-like lexical retrieval with optional cached semantic ranking, bounded threshold/top-k selection, score explanations, offline fallback, and unchanged Context Firewall authority. |
| **M21 — Agent Reliability Evaluation & Scorecard** | Completed | Run isolated versioned task corpora through durable tasks, Trust, Action Ledger, Replay, verification, and M20 retrieval; aggregate completion, safety, recovery, false-success, category, baseline, and regression evidence into CLI/JSON scorecards. |
| **M22 — Adaptive Planning & Replanning** | Completed | Bounded observable evidence-based plan review (KEEP, REVISE_REMAINING, BLOCK), durable immutable plan revisions, superseding pending steps without altering completed execution history, Context Firewall enforcement, zero-tool reviewer, crash idempotency, and M21 reliability eval integration. |
| **M23 — Goal Contract & Task-Level Success Verification** | Completed | Objective Goal Contract generation (success criteria, explicit negative constraints, bounds), read-only task-level goal verifier, layered verification, non-repudiable canonical SHA-256 evidence hashing, crash-recovery completion reconciliation, and M21 evaluation scorecard integration. |
| **M24 — Capability Discovery & Tool Routing** | Completed | Deterministic capability catalog, multi-stage lexical/semantic routing, operation filtering, hard bounds, strict separation of routing vs Trust Kernel authorization, Context Firewall DATA protection, tool schema projection, CLI, and M21 scorecard integration. |
| **M25 — Resource Budget Governance** | Completed | Enforce durable task budgets for model/tool/step/replan/verification/retry/token/command/runtime resources with conservative blocking and inspectable usage. |
| **M26 — Live Evaluation** | Completed | Exercise configured providers through the normal governed task runtime with an isolated 14-case corpus, pre-flight diagnostics, repeated runs, and live resource telemetry. |
| **M26.1 — Full Live Corpus Baseline Integrity** | Completed | Separate provider readiness from benchmark completion; persist full/subset/interrupted scope, case-run completeness, checkpoints, token coverage, provider circuit breaking, and canonical full-corpus baseline safeguards. |
| **M27 — Live Completion Reliability** | Completed | Live-completion attribution, safety-preserving reliability contracts, and structured multi-case root cause categorization. |
| **M28 — Offline Step Verification** | Completed | Offline-first deterministic step verifiers with strict observable evidence validation. |
| **M29 — Evidence-Producing Plans** | Completed | Evidence requirement contracts for plan steps ensuring verifiable observable artifacts. |
| **M30 — Step Completion Controller** | Completed | Governed multi-turn step completion controller with bounded evidence-driven continuation loops. |
| **M31 — Failure Recovery Replanning** | Completed | Evidence-driven failure classification, strategy fingerprinting, anti-cycling, and atomic plan revisions. |
| **M32 — Budget-Aware Recovery Planning & Model-Call Efficiency** | Completed | Criticality-aware budget reservations, starvation prevention headroom, graceful plan preservation on low review budget, and deterministic verification fallback. |

| **M33 — Role-Specific Model Capability Profiling & Model Fabric Baseline** | Completed | Measure configured models independently across planning, execution, replanning, contract construction, and verification roles using observable quality, safety, latency, and resource evidence. |
| **M34 — Evidence-Gated Role-Aware Model Routing** | Completed | Deterministically route cognitive roles (planner, executor, replanner, verifier, contract builder) to explicitly configured or evidence-backed compatible models while preserving user precedence, safety gates, resource limits, and Model Fabric boundaries. |
| **M35 — Runtime Attribution, Checkpoint Provenance & Live Regression Audit** | Completed | Reconcile live scorecard metrics and denominator invariants, introduce durable ExecutionCheckpoint provenance linked to Action Ledger, distinguish configured/recommended/effective/actually-executed models, classify checkpoint loss, attribute first-divergence regressions, and add task execution tracing. |
| **M36 — Structured Executor Protocol & Tool-Use Reliability** | Completed | Validate and govern Executor action proposals, distinguish protocol errors from runtime failures, perform bounded correction for malformed or missing tool actions, and measure whether Executor turns actually realize required execution checkpoints. |
| **M37 — Required Tool Activation & Execution Scaffolding** | Completed | Explicitly signal structured tool action requirements on the first turn of tool-required steps, semantically narrow visible tools by resource domain, provide provider-neutral tool choice scaffolding, and track end-to-end activation funnels without compromising Trust, Action Ledger, or Context Firewall invariants. |

M5–M37 are complete in the current public-beta candidate. Durable Tasks, recovery, command
execution, and scheduler ticks are local and foreground-only; automatic rollback, always-on
background workers, distributed scheduling/recovery/execution, and OS sandboxing are not shipped.

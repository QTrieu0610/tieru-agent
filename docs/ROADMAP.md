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

M5–M13 are complete in the current public-beta candidate.

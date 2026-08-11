# Tieru product specification

Status: M13 Tieru Capsule completed on 2026-08-11. M1–M12 runtime,
identity, Memory Graph, Trust Kernel, Replay, Skill Forge, Shadow, and Model
Fabric capabilities remain the shipped baseline.

## 1. Goal

Tieru is a local-first personal AI runtime whose lifecycle, model routing,
memory, skills, tools, permissions, tracing, and evaluation remain readable and
user-controlled. It builds on the useful architecture of the upstream Waku
project while giving Tieru its own product direction and keeping provider
selection, model selection, storage, and risky actions configuration-driven.

Tieru must support terminal and local-dashboard conversations, durable personal
memory, explicit tool controls, and both hosted and local model endpoints.
Optional integrations must stay optional and must not enlarge the default
dependency or permission footprint.

The product idea is **One memory. Any model. Your rules.** The long-term product
model is Remember, Learn, Think, Act safely, Verify, and Move with you. Current subsystem names and
future reserved vocabulary are defined in `docs/PRODUCT_IDENTITY.md`.

## 2. Branding

- Public product name: **Tieru**.
- Canonical distribution and CLI: `tieru-agent` and `tieru`.
- Canonical Python namespace: `tieru`. Deprecated Waku identifiers remain only
  where the compatibility layer still intentionally supports them.
- Canonical environment prefix and runtime home: `TIERU_*` and `.tieru`.
- During migration, old `WAKU_*` variables and `.waku` data may be read as
  deprecated fallbacks, but new writes must converge on one canonical Tieru
  location. Conflicts must be diagnosed rather than silently merged.
- User-visible prompts, CLI text, dashboard copy, current docs, examples, trace labels, generated `SOUL.md`/`MEMORY.md`, metadata, and Tieru-owned diagrams must use Tieru after their owning milestone. Historical or separately licensed diagrams, technical identifiers, and provenance records retain their original names.
- Historical attribution, copyright notices, upstream links, and license text must not be rewritten as Tieru authorship.

## 3. Current product language and status

| Subsystem | Status | Current meaning |
|---|---|---|
| Tieru Runtime | Shipped | Application lifecycle, session assembly, gateways, and agent execution |
| Tieru Memory | Shipped | Semantic, episodic, procedural, graph, and working-memory behavior |
| Tieru Memory Graph | Shipped | Typed SQLite entities and temporal relations with provenance, confidence, importance, and supersession |
| Tieru Skills | Shipped | Reusable `SKILL.md` procedures loaded into context when relevant |
| Tieru Model Layer | Shipped | Provider adapters and independent `main`, `small`, and `judge` roles |
| Tieru Model Fabric v2 | Shipped | M11 QUICK/STANDARD/AGENT/DEEP modes plus policy-first configured candidate selection, cached availability, capability filtering, inspectable scoring/history, sticky targets, and safe bounded fallback |
| Tieru Trust Kernel | Shipped | Structured action authorization, deterministic risk, scoped policy, approval, redaction, and fail-closed tool execution |
| Tieru Replay | Shipped | Stable per-turn IDs, ordered normalized local events, bounded/redacted payloads, deterministic summaries, retention, and read-only CLI/dashboard inspection |
| Tieru Skill Forge | Shipped | Explicit Replay selection, deterministic WorkflowCandidate extraction, conservative generalization, optional current-model drafting with offline fallback, provenance, validation/evaluation, review, and Trust-authorized user-skill installation |
| Tieru Shadow | Shipped | Passive deterministic aggregation of eligible successful Replay structures, explainable and suppressible suggestions, installed-skill/draft checks, and explicit handoff to an inactive Forge draft |
| Tieru Capsule | Shipped | Versioned, integrity-checked, selective offline snapshots with secret exclusion, preview-first additive import, conservative Trust handling, and no live link |
| Tieru Tools | Shipped | Built-in, MCP, and optional restricted browser capabilities |

Model Fabric v2 is shipped in M12 and Tieru Capsule is shipped in M13. Model Fabric remains configuration-driven and
does not claim autonomous provider purchasing, live-price knowledge, or Trust changes.

## 4. Config-driven architecture

Configuration is an input to assembly, not scattered global state. The target flow is:

```text
defaults < config file < environment/.env < explicit runtime override
       -> validated settings
       -> provider/model router + memory backends + tool policy + gateways
       -> one assembled Tieru application
```

Requirements:

- Use one typed settings object at the application boundary.
- Keep non-secret settings in the versioned YAML config; keep secrets in environment variables.
- Provider, endpoint, main model, small model, timeouts, memory backends, gateway flags, and tool policy must not be hard-coded in business logic.
- Main and small model are distinct roles. The main model answers and chooses tools; the small model handles retrieval gating, consolidation, and optional triage. Either role may target the same endpoint/model when a deployment requires it.
- Validate unknown providers, missing role assignments, invalid URLs, numeric bounds, and incompatible capabilities before the first turn.
- Loading configuration must not make network calls. Model catalog discovery is a separate, best-effort operation.
- Dashboard settings and CLI startup must use the same loader and redaction rules.
- Configuration changes must be auditable without emitting secret values.

## 5. Ollama and Gemma 4 E2B

Ollama is a first-class local provider target through its OpenAI-compatible API. It must not require a real API key, must default to a loopback endpoint, and must allow arbitrary installed Ollama model tags through configuration.

The verified local-model target is **`gemma4:e2b`**. The tag was confirmed
against Ollama's official Gemma 4 library and a local Ollama 0.32.5 catalog on
2026-08-04. It is owned by provider/profile defaults and examples, not loop
logic. Gemma is a model backend and must never become Tieru's product identity.

Ollama acceptance requires:

- non-streaming and streaming text replies;
- tool schema submission, tool-call decoding, tool-result round trips, and graceful capability errors;
- main/small role routing, including the option to use the same local model for both;
- local `/models` discovery or an honest fallback;
- no outbound cloud request when both roles and required memory features are local;
- an offline adapter test and a separately marked live smoke test that is skipped when Ollama or the requested model is unavailable.

## 6. Memory

Tieru retains four additive memory capabilities:

- semantic memory for durable facts;
- episodic memory for dated events and conversation summaries;
- procedural memory for persona and skill instructions;
- graph memory for typed entities and directed relationships.

SQLite remains the local source of truth by default. Working memory is a bounded recent-history window plus gated relevant retrieval. Long-term writes are explicit by default; consolidation is an opt-in policy and must never discard unconsolidated chat after a model or backend failure. Human-readable exports are generated views, not a second source of truth.

Graph relations include confidence, importance, source type/reference, temporal
validity, lifecycle status, and optional supersession linkage. A configured
single-value predicate may deterministically supersede an older active value;
multi-value predicates preserve concurrent relations. Retrieval uses exact
local entity matching and bounded depth-one neighborhoods before semantic
fallback. It must not require an API key, invoke a model for traversal, inject
the whole graph, or silently structure ordinary conversation and legacy text
memory. `docs/MEMORY_GRAPH.md` is the detailed M6 contract.

Backend interfaces must cover the operations actually used by the app and dashboard. A backend cannot be advertised as interchangeable if it only implements search/add while callers require list/update/delete. Schema and home-directory migrations must be additive, backed up, idempotent, and tested with legacy `.waku` data.

Memory content is private data. It must not be placed in logs, traces, tool arguments sent to unrelated services, or shared gateways unless the user explicitly configures that path.

## 7. Trust Kernel and tool permissions

Tool execution is deny-by-default and policy-driven. A tool declares its
capabilities and side effects; `ToolRegistry` constructs a safe `ActionRequest`
and the Trust Kernel returns a structured `TrustDecision` before calling it. The
model requests actions but is never the authorization authority.

Minimum permission dimensions:

- local read;
- local write;
- network read;
- external write/message;
- process execution;
- browser automation;
- destructive or irreversible action.

Requirements:

- Unknown or unclassified tools are denied.
- Read and write permissions are separate; enabling an integration does not imply every operation is allowed.
- External writes, messages, process execution, browser actions with side effects, and destructive operations require explicit policy and, where configured, per-action confirmation.
- Paths, hosts, commands, recipients, and working directories support allowlists and scoped limits.
- Tool results expose what happened without exposing credentials.
- Permission decisions are traceable with secret and personal-data redaction.
- MCP tools receive the same policy treatment as built-in tools.
- Rate, iteration, timeout, and output-size limits are enforced outside model prompts.
- Canonical capabilities are `local_read`, `local_write`, `network_read`,
  `external_write`, `process_execution`, `browser_automation`, and `destructive`.
- Risk is deterministically classified as LOW, MEDIUM, HIGH, or CRITICAL from
  capability, operation, target/scope, external effect, destructiveness,
  reversibility, and locally declared metadata. No LLM classifies risk.
- Policy precedence is explicit deny, matching scoped allow, approval,
  capability/default policy, then deny by default. Out-of-scope targets deny.
- Approval is exact and single-use. Missing, expired, declined, or failing
  approval fails closed and never writes a permanent rule.
- Stable action fingerprints exclude secrets and arbitrary sensitive content.
  Repeated identical denials do not create approval-spam loops.
- Safe `trust_request`, `trust_approval`, and `trust_decision` events use the
  existing observer/tracer boundary without storing action arguments.
- Existing `tool_permissions` configuration is normalized into Trust policy;
  a legacy deny can never become an allow.

## 8. Tieru Replay

Every new user turn has a unique local run ID distinct from its session ID.
Observer events from runtime, memory, model calls, routing/graph execution,
Trust, tools, errors, and output are normalized centrally and persisted in the
existing `state.db` with a monotonic per-run sequence.

Requirements:

- `replay_runs` and `replay_events` schema is additive, indexed, idempotent, and
  backward-compatible; historical traces do not require reconstruction.
- Existing JSONL and optional OTel tracing continue unchanged as separate
  observability outputs.
- Actual role/provider/model, token usage when available, duration, stop reason,
  Trust verdicts, and bounded tool outcomes are inspectable.
- Retrieved memory bodies, prompts/messages, hidden reasoning, scratchpads,
  credentials, authorization headers, and unbounded outputs are not persisted.
- Configurable maximum run count, age, event bytes, and tool-preview bytes bound
  growth. Replay cleanup never deletes sessions, conversations, or Memory.
- CLI and dashboard inspection are read-only; M8 cannot rerun, resume, fork,
  retry, edit, or recreate side effects.
- Replay summaries are deterministic and require no model or network call.
- Recorder/storage failure degrades observability only. It cannot authorize an
  action, bypass Trust, or execute a denied tool.

`docs/REPLAY.md` is the detailed M8 contract.

## 9. Tieru Skill Forge

Skill Forge begins only on an explicit user command or dashboard action and
consumes one or more selected successful Replay runs. It deterministically
extracts ordered observable tool, Trust, and evidence structure into a bounded
`WorkflowCandidate`, then conservatively generalizes known input fields. A
stable structural signature excludes argument values and private content.

Draft synthesis may use the configured `small` role, but an offline
deterministic template is always available. Inactive drafts live under
`TIERU_HOME/forge/drafts`; they preserve source run/event provenance,
generation identity, content hashes, versions, edit state, validation, and
evaluation. Deterministic safety validation and side-effect-free behavioral
consistency evaluation are blocking. An unavailable optional judge is recorded
as skipped, never passed.

Installation is a distinct human-approved operation after review. Draft writes
and installation cross the Trust Kernel as scoped local writes. Collision-safe
staging installs canonical `SKILL.md` into `TIERU_HOME/skills`, where the
existing procedural loader consumes it. Capability declarations never grant
permissions, denied source actions stay denied, and no historical tool is
executed. See `docs/SKILL_FORGE.md` for the detailed M9 contract.

## 10. Optional Playwright

Playwright is an implemented optional browser-automation capability, not a core dependency.

- It is disabled by default and installed through an extra.
- It uses an isolated context with no personal profile, cookies, or stored credentials; downloads and uploads are not exposed. Screenshots use safe generated names under the configured Tieru home.
- Browser navigation, downloads, uploads, credential use, and state-changing clicks are governed by the tool policy.
- Default posture is read-only navigation to allowed hosts; arbitrary local-file access and silent credential reuse are denied.
- Tests use local fixtures/pages and do not require the public internet. A live browser smoke test is separately marked and optional.
- Absence of Playwright or its browser binaries must not break CLI, dashboard, core tools, or test collection.

## 11. Current release completion criteria

The current shipped foundation is release-ready when all of the following are
true; this does not imply completion of the M13 roadmap:

- Public branding is consistently Tieru while upstream attribution remains intact.
- Package, import namespace, CLI, environment variables, runtime home, examples, and data migration have a documented compatibility path.
- A single validated configuration loader drives every entry point.
- Provider adapters and the role router have contract tests; no model/provider is hard-coded outside config/default catalogs.
- Ollama works with the verified Gemma 4 E2B tag for the supported capability set, with honest skips when the local service is absent.
- Semantic, episodic, procedural, graph, retrieval, consolidation, session, export, and legacy-data paths are tested.
- Every tool is classified and enforced by the permission layer; risky actions have explicit approval behavior.
- Playwright remains optional and sandboxed by policy.
- CLI and dashboard use the same app assembly and expose redacted operational state.
- Replay run/event persistence, ordering, privacy, retention, read-only
  inspection, and failure isolation are tested without weakening Trust.
- Skill Forge extraction, provenance, generalization, signatures, offline
  drafting, validation/evaluation, review, Trust denial, collision safety, and
  normal procedural loading are deterministically tested.
- Shadow eligibility, exact Forge signature reuse, bounded/idempotent
  aggregation, deterministic thresholds/confidence, suppression, privacy,
  disablement, failure isolation, and draft-only Forge handoff are tested.
- Model Fabric task analysis, immutable profiles, structured sticky routing,
  QUICK call-path reduction, STANDARD compatibility, Trust-controlled AGENT,
  bounded DEEP verification, Replay metadata, and local fallbacks are tested.
- Model Fabric candidate registry, lazy availability, explicit privacy routing,
  conservative capability filters, deterministic scoring, minimum-sample
  history, explainable selection, and bounded side-effect-safe fallback are tested.
- Deterministic tests and lint pass in the supported environment; live/API tests are clearly marked and their executed/skipped status is reported.
- Documentation, examples, migration notes, security notes, and evals match shipped behavior.
- A final diff review shows no secrets, generated runtime data, accidental dependency growth, or removed attribution.

## 12. License and attribution

The upstream repository is MIT licensed: copyright 2026 Sean Chen
(ShenSeanChen). Tieru must retain the MIT license text and copyright notice in
all copies or substantial portions, preserve attribution to
`ShenSeanChen/waku-agent`, and clearly distinguish the upstream foundation from
Tieru-specific development and product direction.

Assets or diagrams with a separate license remain under that license. Historical
Waku diagrams and source retain their original names and provenance; rebranding
or redrawing does not erase those notices. Third-party optional components
retain their own licenses and must be reviewed before distribution.

# Tieru implementation plan

Tieru is the public and canonical technical product contract. M1–M4 established
the shipped runtime foundation; M5 establishes the independent product identity:
the distribution is `tieru-agent`, the Python namespace and CLI are `tieru`, configuration uses
`TIERU_*` and YAML profiles, and new runtime state defaults to `.tieru`. Deprecated Waku
identifiers, `WAKU_*`, and `.waku` compatibility remain explicit and warning-backed; data is never silently
moved or merged.

The M2 deterministic result on Windows is 429 passed, 16 skipped, and 5 failed. The five failures
are the previously recorded Windows portability baseline (`python3` subprocess lookup and Unix
shebang execution); M2 introduced no additional full-suite failure. Ruff and focused M2 tests pass.

## M1 — Tieru Branding: completed

**Goal.** Present Tieru consistently as the current public product without
changing the executable namespace, falsifying upstream history, or removing
license and attribution.

**Files changed.** Public strings in `README.md`, root community documents,
`pyproject.toml` description, selected runtime/CLI copy, dashboard copy, and
current architecture documents. Historical material in `docs/` received only a
provenance note where needed. `docs/TIERU_SPEC.md`, this plan, and `AGENTS.md`
define the product and contributor contract.

**Files and modules not changed by this milestone.** Package/import paths,
entry points, `WAKU_*`, `.waku`, trace/schema/storage identifiers, dependencies,
binary or licensed assets, whiteboard source files, `LICENSE`, and implementation
behavior. Historical Waku names and results remain Waku.

**Dependencies.** M0 source audit and the existing deterministic, packaging,
static-asset, and lint checks. No new dependency.

**Implementation tasks.** Completed: public CLI/persona copy, dashboard copy,
root documentation, package description, full Markdown audit, historical/upstream
labels, current architecture corrections, link/fence validation, and attribution
review.

**Test commands.** `python -m pytest -q evals/deterministic`, focused affected
tests under `evals/deterministic/`, `python -m ruff check tieru evals scripts`,
Markdown fence and relative-link checks, `git diff --check`, and targeted diff
inspection for `LICENSE`, assets, implementation, dashboard, tests, and
`template_Agent.md`.

**Acceptance criteria.** Public branding is Tieru; current technical contracts
remain accurate; future features are labelled planned/not implemented;
historical results are not attributed to Tieru; Markdown and relative links are
valid; license and assets are unchanged; no M1-closing implementation behavior is
introduced; the diff is whitespace-clean.

**Risks.** Search-and-replace can corrupt imports, commands, storage paths,
historical evidence, or licensed attribution. A branded sentence can also imply
that a planned feature already exists.

**Rollback point.** The documented M0 snapshot and the path-scoped M1 diff; M1
can be rolled back without touching the upstream implementation baseline or the
unrelated local file `template_Agent.md`.

## M2 - Tieru Core: completed

**Goal.** Ship the `tieru-agent` distribution, canonical `tieru` namespace and CLI, typed YAML
configuration, independent `main`/`small`/`judge` routing, protocol adapters, local Ollama, and a
safe compatibility path from Waku.

**Files changed.** `pyproject.toml`; canonical runtime under `tieru/`; compatibility
configuration and migration paths;
CLI, config, model adapter/router, provider, dashboard settings, catalog, judge, scripts, environment
example, `tieru.example.yaml`, README/contributor docs, and focused deterministic tests.

**Files and modules not changed by this milestone.** Memory behavior and schemas, centralized tool
permissions, Playwright, new delegation behavior, release automation, historical benchmark results,
licensed assets, and upstream attribution.

**Dependencies.** M1; PyYAML for the versioned schema; the existing Anthropic/OpenAI SDKs; Ollama's
OpenAI-compatible inference endpoint; official and local verification of `gemma4:e2b`.

**Implementation tasks.** Completed: one canonical source tree; deprecated import/CLI/env/home
fallbacks with warnings; explicit source-preserving home migration; validated YAML providers and
profiles; precedence `CLI > TIERU_* > WAKU_* > YAML > defaults`; env-only secrets; independent
role router; Anthropic Messages and OpenAI-compatible adapters; Ollama health/discovery/errors;
keyless local profile; CLI doctor/models/profile/dashboard commands; shared CLI/dashboard loader;
packaging and migration tests.

**Test commands.** `python -m pip install -e .`; focused M2 pytest files; full
`python -m pytest -q`; `python -m ruff check tieru tieru evals scripts`; `tieru --help`;
`tieru --help`; local `ollama version`/`ollama list`; live non-streaming, streaming, and tool-call
smokes; packaging artifact/install checks; `git diff --check`.

**Acceptance criteria.** Met: canonical distribution/namespace/CLI work; changing YAML/profile/env
does not require Python edits; all roles share one router; Ollama is keyless and locally verified;
compatibility is warned and non-destructive; dashboard and CLI share settings; focused tests and
lint pass; the full suite has only the five documented Windows baseline failures.

**Risks.** Third-party code may depend on module identity rather than import compatibility; local
OpenAI-compatible servers differ in streaming/tool details; Gemma hardware requirements are high;
and the compatibility window must eventually end rather than become a second permanent contract.

**Rollback point.** Revert the M2 path/entry-point/config diff as one unit. Legacy source data is
preserved because migration only copies after explicit confirmation and refuses existing targets.

## M3 — Personal Capabilities: completed on 2026-08-04

**Goal.** Make memory dependable for a personal agent, centralize tool
permissions, and add optional restricted Playwright automation without weakening
the existing safety boundary.

**Files changed.** `tieru/memory/`, `tieru/runtime/session.py`, `tieru/db.py`,
memory tools and storage adapters, `tieru/tools/registry.py`, classified tool
implementations, `tieru/tools/browser.py`, `tieru/app.py`, CLI confirmation
wiring, optional dependency/configuration metadata, and focused deterministic
and live tests.

**Files and modules not changed.** Model protocol adapters except for consuming
their public interface; benchmark claims; core graph semantics; licensed assets;
and unrestricted browser automation as a default capability.

**Dependencies.** M2 configuration and namespace contracts; a documented memory
schema and retention policy; a deny-by-default permission model with user-visible
decisions; and an approved optional Playwright dependency boundary.

**Implemented.** Added stable typed memory records and unified CRUD/search/export,
FTS5-first retrieval, deduplication, capacity limits, secret rejection, trust and
provenance fields, explicit-by-default long-term writes, and hybrid semantic
fallback. Added one registry permission gate with classified capabilities,
allow/confirm/deny policy, fail-closed non-interactive behavior, structured
denials, argument redaction, and CLI confirmation. Added optional Playwright
open/read/click/fill/screenshot/close tools with isolated state, domain and
network-address enforcement, bounded actions/timeouts, safe screenshots, no
downloads/uploads/arbitrary JavaScript, and permission-gated side effects.

**Test commands.** `python -m pytest -q
evals/deterministic/test_m3_personal_capabilities.py`; `python -m pytest -q
evals/live/test_m3_ollama_browser.py`; full deterministic suite; Ruff;
`compileall`; and `git diff --check`.

**Acceptance result.** Memory survives supported restarts and can be inspected,
exported, updated, and deleted; retrieval is bounded and FTS5 remains available;
registered tools pass through one auditable permission policy; denials are
structured and non-retryable; sensitive arguments are redacted; Playwright is
absent and disabled by default and passed a live local-fixture tool round trip
with Ollama `gemma4:e2b` when installed.

**Risks.** Memory can leak sensitive data or pollute context. Distributed
permission checks can drift. Browser automation expands the attack surface and
can trigger irreversible external actions.

**Rollback point.** Ship memory changes, centralized permissions, and Playwright
behind separate configuration gates. Preserve the pre-M3 store reader and keep
Playwright disabled until its safety and isolation tests pass.

## M4 — Release: completed

**Goal.** Produce a reproducible Tieru release with trustworthy evals, Windows
portability, correct packaging, security review, and final user/developer
documentation.

**Files expected to change.** `evals/`, packaging metadata and build workflows,
portable subprocess/path handling in affected runtime and tool modules, release
and security documentation, README/setup/migration guides, dashboard/CLI copy
needed for final consistency, and CI/release configuration if present.

**Files and modules not changed.** Stable M2 configuration/provider contracts and
M3 permission/memory semantics except for release-blocking defects; licensed
assets and upstream attribution; historical benchmark results unless they are
explicitly rerun and recorded with provenance.

**Dependencies.** M2 and M3 acceptance criteria; supported Python/OS matrix;
reproducible test fixtures; package/release ownership; and a real security contact
before one is published.

**Implementation tasks.** Completed: Windows verification now uses the active
interpreter with explicit argv and no shell; extensionless Python shebang tools
are launched portably; subprocess and affected persisted text use UTF-8;
dashboard approval uses the existing M3 gate with exact, redacted, expiring,
single-use requests; dashboard mutations enforce loopback Origin, session, and
CSRF; wheel/sdist metadata, packaged static/YAML data, clean entry points, the
Windows/Ubuntu Python 3.11/3.12 CI matrix, security notes, and release-gate
automation are covered. Live services remain separately reported, never folded
into deterministic results.

**Test commands.** Full deterministic suite on every supported OS/Python version;
Ruff; package build and metadata validation; clean-venv wheel/sdist smoke tests;
opt-in provider/Ollama/Playwright integrations; Markdown fence/link checks;
`git diff --check`; dependency and secret scans selected during security review.

**Acceptance criteria.** Completed when supported platforms are green or have explicitly scoped
documented exceptions; package artifacts install and expose the Tieru CLI; evals
are reproducible and never overclaim results; network/key requirements are
labelled; security findings are resolved or accepted with rationale; final docs
match shipped behavior; license and upstream attribution remain intact.

**Risks.** Platform fixes can mask real failures, optional integrations can make
CI flaky, packaging can omit runtime data, and release prose can outrun the
implementation. A security contact or repository URL must never be invented.

**Rollback point.** Tag the last release candidate that passes the supported
matrix and artifact smoke tests; reject later documentation, packaging, or
hardening changes independently until their checks are green.

**Local M4 evidence (Windows, 2026-08-04).** `python -m
tieru.ops.release_gate` completed with 459 passed, 14 skipped, zero failures;
Ruff, compileall, wheel/sdist build, and Twine metadata checks passed. A clean
venv installed `tieru_agent-0.2.0-py3-none-any.whl`, ran `tieru --help`, the
deprecated `tieru --help`, and `tieru doctor`, and found the packaged dashboard
and YAML example under `site-packages`. The Playwright dashboard visual smoke
passed, including an authenticated mutation and the approval modal. Ollama
0.32.5 with local `gemma4:e2b` passed the live model/restricted-browser fixture
(1 passed in 65.44 seconds). `pip check` was clean; `pip-audit` found no known
vulnerabilities after upgrading the clean venv's bootstrap pip to 26.2, while
the unpublished local `tieru-agent` distribution itself was explicitly skipped.
This is local evidence only: the new cloud CI matrix has not been observed.

## M5 — Tieru Identity: completed on 2026-08-10

**Goal.** Position Tieru as a local-first personal AI runtime with the product
idea “One memory. Any model. Your rules.” Establish consistent names for the
shipped runtime while separating current foundations from planned products.

**Scope.** Rework the README, current architecture and specification, package
and CLI metadata, dashboard introduction, runtime self-description, example
comments, product identity, and roadmap. Preserve upstream attribution,
copyright, history, compatibility settings, migrations, and historical source
artifacts.

**Non-goals.** No Memory Graph, Trust Kernel, Replay, Skill Forge, Shadow Mode,
Model Fabric, or Capsule implementation; no core-loop redesign; no unnecessary
runtime behavior changes.

**Acceptance.** Current capabilities are source-backed; every future name is
marked planned; Gemma 4 E2B is a verified local backend rather than the brand;
public Waku/OpenClaw/Hermes comparisons are removed or given provenance context;
tests and the release gate are reported honestly.

## M6 — Tieru Memory Graph: completed on 2026-08-10

**Goal.** Add a typed relationship layer to Tieru Memory without replacing its
semantic, episodic, or procedural capabilities. Keep the entire graph usable
offline with SQLite as the sole authoritative store.

**Scope.** Add typed entity and relation records, provenance, bounded confidence
and importance, temporal validity, lifecycle states, deterministic configured
supersession, repository/service boundaries, explicit permission-gated memory
operations, bounded model-free retrieval, `MEMORY.md` export, dashboard tables,
additive schema creation, deterministic tests, and user documentation.

**Migration posture.** Existing `state.db` files open unchanged through
idempotent `CREATE TABLE/INDEX IF NOT EXISTS` statements. Existing facts,
episodes, skills, chat history, FTS indexes, optional stores, and compatibility
paths remain independent. M6 performs no graph backfill and no automatic
conversation extraction.

**Non-goals.** No external graph database, mandatory vector infrastructure,
unrestricted graph reasoning, autonomous extraction, Trust Kernel, Replay,
Skill Forge, Shadow Mode, Model Fabric, Capsule, or multi-agent memory.

**Acceptance.** Typed entities and relations persist locally; relation
provenance, scores, time bounds, contradiction/archive states, and supersession
are inspectable; single- and multi-value predicates behave differently by
explicit policy; retrieval is bounded and model-free; writes reuse the memory
permission boundary; secrets are rejected; export/dashboard inspection works;
legacy SQLite and existing memory behavior remain compatible; deterministic
tests, lint, diff checks, and release gate are reported honestly.

## M7 — Tieru Trust Kernel: completed on 2026-08-10

**Goal.** Make the model an action requester rather than the execution authority
by turning Tieru's proven permission controls into one coherent, structured,
explainable authorization subsystem.

**Scope.** Add `ActionRequest`, `TrustDecision`, canonical capabilities,
deterministic four-level risk, secret-safe fingerprints, scoped policy with
deny-first precedence, exact allow-once approval, repeated-denial suppression,
fail-closed error handling, safe Trust events, legacy policy normalization,
MCP local classification, pre-Playwright domain authorization, target metadata,
CLI/dashboard inspection, deterministic tests, and documentation.

**Compatibility.** `Tool`, `PermissionRequest`, `ToolRegistry`, legacy
`tool_permissions`, action-specific memory policy, CLI and dashboard approval
handlers, browser sandboxing, process allowlists, and the loop's denial guard
remain supported. Existing denies retain priority. M5/M6 state and behavior are
not rewritten.

**Non-goals.** No OS sandbox, shell parser, antivirus, model-based risk analysis,
permanent silent trust learning, Replay, Skill Forge, Shadow Mode, Model Fabric,
Capsule, or cloud identity service.

**Acceptance.** All registered model tools authorize at the common boundary;
unclassified and failing actions deny without execution; scopes and approvals
are deterministic and secret-safe; MCP, browser, filesystem/process, and memory
actions follow Trust; policy and recent safe decisions are inspectable; old
configuration maps safely; full tests, release gate, lint, packaging, and diff
checks are reported honestly.

## M8 — Tieru Replay: completed on 2026-08-10

**Goal.** Make every important Tieru action inspectable after a run without
recording hidden model reasoning or enabling side-effect re-execution.

**Scope.** Add stable per-turn run IDs; additive `replay_runs` and
`replay_events` tables; monotonic event sequences; central normalization over
existing loop, memory, graph, Trust, tool, error, and output events; actual
role/provider/model metadata; bounded/redacted previews; deterministic
summaries; retention; failure isolation; and read-only CLI/dashboard timelines.

**Compatibility.** Existing JSONL/OTel tracing, sessions, Memory Graph, Trust
Kernel, tool APIs, model roles, and old SQLite databases remain valid. Replay
cleanup affects telemetry only and never conversation or memory state.

**Non-goals.** No private chain-of-thought, historical trace reconstruction,
execution fork/resume/retry, side-effect replay, cloud telemetry, Skill Forge,
Shadow Mode, Model Fabric, or Capsule.

**Acceptance.** Runs/events persist locally with stable IDs and deterministic
ordering; current observer events normalize centrally; model, Memory, Trust,
tool, output, and failure metadata are safely inspectable; secrets and large
content are bounded; JSONL remains functional; CLI/dashboard work read-only;
retention and recorder failures cannot delete Memory or weaken Trust; tests,
lint, packaging, release gate, and diff checks are reported honestly.

## M9 — Tieru Skill Forge: completed on 2026-08-10

Explicitly selected, successful Replay runs now pass through deterministic
workflow extraction and conservative input generalization before optional
current-model synthesis. Offline template drafting, local inactive draft
storage, provenance and stable signatures, deterministic safety validation,
side-effect-free consistency evaluation, honest optional-judge reporting,
human review, collision-safe versioned installation, and M7 Trust authorization
are shipped through the CLI and no-build dashboard. Existing `SKILL.md`
procedural loading remains the only runtime consumer.

**Non-goals.** M9 does not monitor activity, detect repetition, auto-create or
auto-install skills, replay historical side effects, replace installed skills,
modify Trust policy, or implement Shadow, Model Fabric, or Capsule.

## M10 — Tieru Shadow: completed on 2026-08-10

Completed successful Replay runs now flow through M9's exact workflow extractor
and signature into local, bounded, idempotent pattern aggregation. Deterministic
thresholds and confidence create explainable suggestions only after repetition;
ignore, snooze, dismiss, active-draft, and installed-skill checks suppress nags.
The CLI and no-build dashboard expose inspection, controls, and an explicit
handoff to the existing Forge lifecycle, which creates an inactive draft only.

**Non-goals.** M10 does not call models for detection, execute or replay tools,
monitor activity outside Replay, alter Memory or Trust, install skills, add a
background worker, perform semantic clustering, or implement Model Fabric or
Capsule.

## M11 — Tieru Model Fabric v1: completed on 2026-08-10

A deterministic-first task analyzer, structured task/route/profile records, and
policy-controlled QUICK/STANDARD/AGENT/DEEP modes now sit above the existing
role-based `ModelRouter`. Per-turn profiles control finite budgets, bounded
history, memory participation, tool-schema exposure, and optional DEEP
verification without changing provider configuration or Trust authority.
Replay, CLI, and the no-build dashboard expose safe route explanations and
local descriptive metrics. Fabric is opt-in for upgrade compatibility.

**Non-goals.** M11 does not adaptively select models/providers, benchmark cloud
backends, learn from historical metrics, switch models during a turn, grant
permissions, introduce autonomous agents, or implement Capsule.

## M12 — Tieru Model Fabric v2: completed on 2026-08-11

M12 preserves M11 execution modes, then discovers configuration-driven local
and optional cloud candidates. Lazy cached availability and hard privacy,
policy, role, capability, and context filters precede normalized deterministic
scoring. A structured `ModelSelection` explains every exclusion and score.
Replay-backed success/latency/tool aggregates remain neutral below a configured
minimum. The selected target is sticky; bounded fallback is limited to hard
infrastructure failures before tool activity and re-applies every hard filter.
`ModelRouter` remains client authority and Trust remains action authority.

**Non-goals.** M12 does not download models, scrape prices, auto-enable or buy
cloud access, switch for subjective answer quality, train a router, fine-tune a
model, modify Trust, or implement Capsule.

## M13 — Tieru Capsule: completed on 2026-08-11

M13 ships `tieru/capsule/`: a ZIP-compatible `tieru-capsule/1` snapshot with
manifest hashes, bounded input validation, portable and full-history scopes,
structural secret exclusion, structured Memory/Graph records, user skill
validation, pending-review Trust, non-secret Fabric merge, an `ImportPlan`,
transactional additive execution, local backup/audit, CLI, and dashboard.

**Non-goals.** No cloud sync, account, live link, marketplace, credential
portability, custom cryptography, automatic upload, or Trust bypass.

The concise public roadmap lives in `docs/ROADMAP.md`.

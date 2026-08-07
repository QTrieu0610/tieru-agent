# Tieru product specification

Status: M4 Tieru Release Candidate implemented on 2026-08-04; local verification evidence is recorded in the implementation plan and release report.

## 1. Goal

Tieru is a local-first personal AI agent whose harness, model routing, memory, tools, permissions, and evaluation remain readable and user-controlled. It should preserve the useful architecture of the upstream tieru project while making provider selection, model selection, storage, and risky actions configuration-driven.

Tieru must support terminal and local-dashboard conversations, durable personal memory, explicit tool controls, and both hosted and local model endpoints. Optional integrations must stay optional and must not enlarge the default dependency or permission footprint.

## 2. Branding

- Public product name: **Tieru**.
- Canonical distribution and CLI: `tieru-agent` and `tieru`.
- Canonical Python namespace: `tieru`; deprecated `tieru` imports and CLI calls are compatibility shims over the same source tree.
- Canonical environment prefix and runtime home: `TIERU_*` and `.tieru`.
- During migration, old `WAKU_*` variables and `.tieru` data may be read as deprecated fallbacks, but new writes must converge on one canonical Tieru location. Conflicts must be diagnosed rather than silently merged.
- User-visible prompts, CLI text, dashboard copy, current docs, examples, trace labels, generated `SOUL.md`/`MEMORY.md`, metadata, and Tieru-owned diagrams must use Tieru after their owning milestone. Historical or separately licensed diagrams, technical identifiers, and provenance records retain their original names.
- Historical attribution, copyright notices, upstream links, and license text must not be rewritten as Tieru authorship.

## 3. Config-driven architecture

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

## 4. Ollama and Gemma 4 E2B

Ollama is a first-class local provider target through its OpenAI-compatible API. It must not require a real API key, must default to a loopback endpoint, and must allow arbitrary installed Ollama model tags through configuration.

The verified local-model target is **`gemma4:e2b`**. The tag was confirmed against Ollama's official Gemma 4 library and a local Ollama 0.32.5 catalog on 2026-08-04. It is owned by provider/profile defaults and examples, not loop logic.

Ollama acceptance requires:

- non-streaming and streaming text replies;
- tool schema submission, tool-call decoding, tool-result round trips, and graceful capability errors;
- main/small role routing, including the option to use the same local model for both;
- local `/models` discovery or an honest fallback;
- no outbound cloud request when both roles and required memory features are local;
- an offline adapter test and a separately marked live smoke test that is skipped when Ollama or the requested model is unavailable.

## 5. Memory

Tieru retains three explicit memory layers:

- semantic memory for durable facts;
- episodic memory for dated events and conversation summaries;
- procedural memory for persona and skill instructions.

SQLite remains the local source of truth by default. Working memory is a bounded recent-history window plus gated relevant retrieval. Long-term writes are explicit by default; consolidation is an opt-in policy and must never discard unconsolidated chat after a model or backend failure. Human-readable exports are generated views, not a second source of truth.

Backend interfaces must cover the operations actually used by the app and dashboard. A backend cannot be advertised as interchangeable if it only implements search/add while callers require list/update/delete. Schema and home-directory migrations must be additive, backed up, idempotent, and tested with legacy `.tieru` data.

Memory content is private data. It must not be placed in logs, traces, tool arguments sent to unrelated services, or shared gateways unless the user explicitly configures that path.

## 6. Tool permissions

Tool execution is deny-by-default and policy-driven. A tool declares its capabilities and side effects; the registry enforces policy before calling it.

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

## 7. Optional Playwright

Playwright is an implemented optional browser-automation capability, not a core dependency.

- It is disabled by default and installed through an extra.
- It uses an isolated context with no personal profile, cookies, or stored credentials; downloads and uploads are not exposed. Screenshots use safe generated names under the configured Tieru home.
- Browser navigation, downloads, uploads, credential use, and state-changing clicks are governed by the tool policy.
- Default posture is read-only navigation to allowed hosts; arbitrary local-file access and silent credential reuse are denied.
- Tests use local fixtures/pages and do not require the public internet. A live browser smoke test is separately marked and optional.
- Absence of Playwright or its browser binaries must not break CLI, dashboard, core tools, or test collection.

## 8. Overall completion criteria

Tieru is complete when all of the following are true:

- Public branding is consistently Tieru while upstream attribution remains intact.
- Package, import namespace, CLI, environment variables, runtime home, examples, and data migration have a documented compatibility path.
- A single validated configuration loader drives every entry point.
- Provider adapters and the role router have contract tests; no model/provider is hard-coded outside config/default catalogs.
- Ollama works with the verified Gemma 4 E2B tag for the supported capability set, with honest skips when the local service is absent.
- Semantic, episodic, procedural, retrieval, consolidation, session, export, and legacy-data paths are tested.
- Every tool is classified and enforced by the permission layer; risky actions have explicit approval behavior.
- Playwright remains optional and sandboxed by policy.
- CLI and dashboard use the same app assembly and expose redacted operational state.
- Deterministic tests and lint pass in the supported environment; live/API tests are clearly marked and their executed/skipped status is reported.
- Documentation, examples, migration notes, security notes, and evals match shipped behavior.
- A final diff review shows no secrets, generated runtime data, accidental dependency growth, or removed attribution.

## 9. License and attribution

The upstream repository is MIT licensed: copyright 2026 Sean Chen (ShenSeanChen). Tieru must retain the MIT license text and copyright notice in all copies or substantial portions, preserve attribution to `ShenSeanChen/tieru-agent`, and clearly distinguish upstream work from Tieru-specific changes.

Assets or diagrams with a separate license remain under that license. In particular, the README currently labels its architecture diagram as CC BY-NC-SA 4.0; rebranding or redrawing it does not erase that notice. Third-party optional components retain their own licenses and must be reviewed before distribution.

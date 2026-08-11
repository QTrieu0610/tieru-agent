# Tieru Shadow

Tieru Shadow is the passive, local repeated-workflow detector shipped in M10.
It observes completed Replay metadata, groups structurally identical workflows,
and hands user-selected evidence to Tieru Skill Forge only on request.

The boundary is deliberately simple:

```text
Shadow observes -> Forge creates -> Trust authorizes -> the user decides
```

Shadow does not call a model or tool, replay a run, monitor the operating
system, write Memory Graph conclusions, change Trust policy, install a skill,
or activate procedural instructions. It examines only already-persisted Replay
metadata after a run completes. A Shadow error is diagnostic only and cannot
change the response or completed Replay run.

## Eligibility and signatures

`ShadowService.observe(run_id)` sends one completed Replay run through M9's
`WorkflowExtractor`. This reuses the same source validation, tool lifecycle,
provenance, input generalization, and `workflow_signature` implementation used
by Forge. Running, failed, corrupt, provenance-incomplete, denied, or failed-tool
runs are recorded as ineligible and never become promotion evidence. Raw user
text, model reasoning, secrets, authentication headers, and tool output are not
pattern keys or Shadow table content.

Signature equality is M10's grouping rule. Benign generalized values such as a
different workspace path do not split an otherwise identical structure;
different ordered operations do. M10 does not use embeddings, clustering, or an
LLM to infer semantic similarity.

## Patterns and aggregation

Each signature has one `ShadowPattern` with first/last-seen timestamps, total
and successful occurrences, verification count, structural tools and
operations, required capabilities, deterministic confidence, lifecycle status,
and bounded Replay evidence. A per-run observation ledger makes processing
idempotent. The default evidence limit is five run IDs; a suggestion passes at
most the latest three compatible IDs to Forge.

Patterns begin as `observing`. At the configured threshold they become
`suggestion_ready`; other states include `ignored`, `snoozed`, `forged`,
`covered`, `dismissed`, and `stale`. The default staleness window is 30 days.
Staleness preserves history and is refreshed when a matching run appears.

The default minimum is three occurrences. Confidence is deterministic:

- `low` below the threshold;
- `medium` at or above the threshold when every observation succeeded;
- `high` after at least five occurrences (and at least two above a custom
  threshold) when every run succeeded and included verification.

No API key, network, Ollama process, or hosted model is needed.

## Suggestions and suppression

A `ShadowSuggestion` contains its pattern/signature, occurrence and confidence,
up to three representative run IDs, a deterministic name and summary, tools,
capabilities, timestamps, lifecycle state, and a deterministic "Why am I seeing
this?" explanation. It remains inspectable before Forge starts.

Shadow avoids repeated prompts as follows:

- **Ignore:** wait until the count increases by another configured minimum.
- **Snooze:** wait until the selected/default date (seven days by default).
- **Dismiss:** permanently suppress that pattern.
- **Forge:** suppress while the compatible active Forge draft remains.
- **Covered:** suppress when an installed skill matches M9's workflow signature,
  normalized name, or ordered tool sequence.

An active compatible Forge draft is surfaced as existing coverage; Shadow never
duplicates, resumes, installs, or rejects it. Rejected, archived, and installed
draft records are not treated as active drafts. If a handed-off draft disappears
or is rejected, the pattern is conservatively ignored until materially more
evidence arrives.

## Forge and Trust boundary

`tieru shadow forge <suggestion-id>` is an explicit handoff to the existing M9
`ForgeService`. Forge re-extracts up to three representative successful Replay
runs, generalizes inputs, creates an inactive draft, and runs its normal
validation and side-effect-free evaluation. Review, approval, and installation
remain separate M9 operations. Installation still crosses the Trust Kernel.

A suggestion or capability declaration is never permission. Repetition cannot
alter Trust policy or approve future actions, regardless of occurrence count.

## Storage and privacy

The additive `shadow_patterns`, `shadow_suggestions`, `shadow_observations`, and
`shadow_settings` tables live in the existing local `state.db`. They contain
structural operational metadata and bounded Replay references, not conversation
bodies. Shadow makes no automatic upload and creates no additional database.
Disabling Shadow stops aggregation and suggestion creation but preserves these
rows for inspection. Replay retention may prune the observation idempotency
ledger; aggregate pattern history remains intact.

## Configuration

Shadow defaults to disabled so an upgrade does not begin analyzing existing
users' behavior without an explicit choice. The flat keys follow Tieru's current
configuration convention:

```yaml
shadow_enabled: false
shadow_min_occurrences: 3
shadow_max_evidence_runs: 5
shadow_suggestions: true
shadow_snooze_days: 7
shadow_stale_days: 30
```

The corresponding `TIERU_SHADOW_*` environment variables are documented in
`.env.example`. A CLI/dashboard enable or disable choice is persisted locally
as an override. It does not delete stored metadata.

## CLI and dashboard

```bash
tieru shadow status
tieru shadow enable
tieru shadow disable
tieru shadow patterns
tieru shadow suggestions [--all]
tieru shadow inspect <suggestion-id>
tieru shadow forge <suggestion-id>
tieru shadow ignore <suggestion-id>
tieru shadow snooze <suggestion-id> [--days N]
tieru shadow dismiss <suggestion-id>
```

The no-build dashboard's **Shadow** view shows enablement, observed and covered
counts, patterns, suggestions, evidence, explanations, steps, tools,
capabilities, and explicit Forge/Ignore/Snooze/Dismiss actions. Opening the view
does not trigger Forge.

Safe observer events include `shadow_run_observed`, `shadow_pattern_updated`,
`shadow_threshold_reached`, `shadow_suggestion_created`, lifecycle-action
events, and `shadow_forge_requested`. They contain bounded identifiers/counts,
not private bodies, and never rewrite historical Replay.

## Limitations

M10 recognizes exact deterministic structural signatures, not semantic intent.
Installed-skill coverage is intentionally conservative and relies on Forge
metadata, normalized names, and ordered declared tool references. Analysis runs
synchronously after safe Replay completion rather than through a background
worker. Shadow observes Tieru Replay only; it does not watch files, browsers,
keyboard/mouse activity, or anything outside Tieru runs.

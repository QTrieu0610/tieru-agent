# Tieru Skill Forge

Tieru Skill Forge is the M9, user-triggered producer for procedural skills. It
turns one or more explicitly selected, successful Replay runs into an inactive,
inspectable draft. The user validates, evaluates, reviews, and explicitly
approves that draft before Trust-authorized installation.

## Replay provenance and WorkflowCandidate

Forge reads only M8 Replay's normalized, bounded, observable records. It does
not read hidden runtime state or chain-of-thought and never reruns historical
tools. `WorkflowExtractor` requires completed runs with intact lifecycle and
tool request/result evidence. It records run/event references rather than
copying tool output bodies.

The deterministic `WorkflowCandidate` contains source run IDs, intent, inputs,
ordered steps, tools, capabilities, Trust requirements, pre/postconditions,
failure paths, evidence references, and a creation time. A denied action stays
denied. An incomplete lifecycle or unterminated tool request is rejected.

## Generalization and workflow signatures

Generalization is intentionally conservative. Known path, repository, branch,
query, URL, and target argument fields may become named inputs. Tool identity,
operation, commands, capability requirements, Trust boundaries, and destructive
scope stay fixed. Multiple runs merge only when their deterministic structures
match.

The SHA-256 workflow signature covers ordered operations, tool names, outcome
classes, capability sequences, and verification markers. It excludes argument
values, secrets, messages, and output bodies. Forge uses it only for provenance
and simple duplicate warnings; M9 does not monitor or detect repeated activity.

## Draft generation and local fallback

Forge may use the currently configured `small` role, including a local Gemma
model through Ollama, to synthesize canonical `SKILL.md`. The prompt contains
only the bounded, redacted WorkflowCandidate. If no model or key is available,
the call fails, or the output lacks required sections, a deterministic template
produces the draft. The WorkflowCandidate is persisted either way.

Drafts are private local data under:

```text
TIERU_HOME/forge/drafts/<draft-id>/
  SKILL.md
  metadata.json
  workflow.json
  evaluation.json
```

They are not in the active skill loader path. Metadata records generation
method/model/provider, source run/event references, edit state, version,
workflow signature, and content hash.

## Lifecycle

The explicit states are `draft`, `validated`, `evaluation_failed`,
`ready_for_review`, `approved`, `installed`, `rejected`, and `archived`.
Installation accepts only `ready_for_review` content whose current hash matches
both validation and evaluation. Manual edits mark content `modified`, update the
hash, return it to `draft`, and clear prior checks while preserving provenance.

## Deterministic validation

Blocking validation checks canonical frontmatter and required procedural
sections, path-safe names/IDs, content and metadata bounds, secrets and
credential headers, Trust-policy bypass or permanent-permission language,
unbounded destructive instructions, source runs/events, and consistent
tool/capability declarations. Model output is untrusted until these checks pass.

## Behavioral evaluation

Side-effect-free consistency evaluation checks that source tools and required
capabilities remain represented, denied operations remain denied, verification
survives, and the Trust boundary remains explicit. An optional judge may add a
result when configured. Without one, Forge records `deterministic_pass: true`
and `judge.status: skipped`; it never invents a judge pass.

## Trust, installation, and versioning

The M7 Trust Kernel governs draft writes and installation as scoped
`local_write` actions. Skill capability metadata is descriptive and cannot
grant authority. Installation also requires a separate explicit human approval.
Forge stages the files and atomically moves them into the existing canonical
user directory, `TIERU_HOME/skills/<skill-id>/`; the existing procedural loader
then consumes the skill normally.

Generated skills start at version 1 with a content hash, creation time, source
provenance, and parent-version field. A same-name collision never overwrites an
installed skill. Name, signature, and tool-sequence comparisons produce simple
duplicate signals; Forge never silently merges skills.

## CLI

```text
tieru skill forge <run-id> [<run-id> ...]
tieru skill drafts
tieru skill inspect <draft-id>
tieru skill validate <draft-id>
tieru skill evaluate <draft-id>
tieru skill install <draft-id>
tieru skill reject <draft-id>
```

Each command supports `--json`. The legacy `tieru skill install <https-url>`
path remains compatible. Drafting and installing are always separate actions.

## Dashboard

The no-build **Skill Forge** view lists eligible successful Replay runs, supports
multi-selection and draft generation, and shows provenance, generalized inputs,
steps, requirements, `SKILL.md`, validation/evaluation state, generator details,
and warnings. Revalidate, Evaluate, Approve / Install, and Reject are explicit
buttons using the same service and Trust boundary as the CLI.

## Privacy, security, and limitations

Forge is local by default, uploads nothing automatically, redacts secrets before
synthesis, bounds prompts and stored content, and uses provenance references.
Configured cloud roles may be used only through Tieru's existing Model Layer.
Prompt injection cannot be solved generally, so human review, deterministic
checks, Trust authorization, and runtime tool policy remain mandatory.

M9 does not observe arbitrary activity, detect repeated workflows, suggest
skills autonomously, replay side effects, generate Trust policy, replace skills,
or provide a marketplace. Those boundaries keep Shadow, Model Fabric, and
Capsule outside this milestone.

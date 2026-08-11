# Tieru Public Demo Guide

Tieru's public story is: **One memory. Any model. Your rules.** These three
demos use synthetic, disposable state to show the shipped product boundaries
without depending on exact model prose or a maintainer's personal data.

The three primary demos are:

1. **Local Agent, Safe Actions, Full Replay** — local Ollama, Model Fabric,
   one-time Trust authorization, and Replay.
2. **From Repeated Workflow to Reusable Skill** — explicit Memory, passive
   Shadow suggestions, and the reviewed Forge lifecycle.
3. **Move Your Tieru Identity to a Fresh Installation** — selective offline
   Capsule export, inspection, dry-run, import, and verification.

## What was audited

The current CLI, no-build dashboard, public examples, subsystem guides, and
Trust approval UX are reusable. `tieru doctor`, `tieru init`, `tieru replay`,
`tieru skill`, `tieru shadow`, `tieru fabric`, and `tieru capsule` are the
canonical public surfaces used below.

`scripts/demo_seed.py`, `docs/DEMO-CHECKLIST.md`, and
`docs/filming-prompts.md` are historical filming material. They contain
upstream-era assumptions and the older destructive state-reset workflow, so
they must not be used for these public recordings. Their remaining names and
facts are synthetic. The dashboard is useful for optional close-up
shots, but the primary flows below use the smaller and more reproducible CLI.

State-changing commands in this guide are deliberately bounded:

| Command or interaction | Mutation | Boundary |
|---|---|---|
| `tieru` with an approved memory/calendar tool | Demo Memory or calendar | Selected disposable `TIERU_HOME` only |
| `tieru shadow enable` | Shadow preference | Selected disposable home only |
| Forge lifecycle commands | Inactive draft, then reviewed user skill | Selected disposable home only |
| Capsule export/import | Demo archive and destination state | Selected demo artifact/home paths only |
| `scripts/prepare_demo.py` | Text fixtures and optional synthetic state | Marker-owned root outside the checkout |

Fabric inspection, Replay inspection, Capsule inspection, and Capsule import
dry-run are read-only with respect to the demonstrated source state. `doctor`
is offline-first and share-safe; `doctor --json` is the preferred capture when
diagnostics need to be shown.

## Prerequisites

- A development or packaged Tieru installation.
- Python 3.11 or newer.
- Ollama with `gemma4:e2b` for the live model portions of Demo 1 and Demo 2.
- No cloud provider, gateway, browser, or external credential.
- A clean terminal around 100–120 columns wide.

Check the local model separately:

```bash
ollama list
```

If it is absent, install Ollama from its official distribution and run:

```bash
ollama pull gemma4:e2b
```

Ollama installation and model download are manual prerequisites; the demo
helper never performs either operation.

## Prepare isolated demo state

Run this from the Tieru checkout. The target is deliberately adjacent to, not
inside, the repository:

```bash
python scripts/prepare_demo.py --root ../tieru-public-demo --replace --seed-capsule-a
cd ../tieru-public-demo/project
python -m pytest -q
```

The helper refuses repository-internal targets, filesystem roots, replacement
of unmarked directories, and cleanup of unmarked directories. It creates four
separate homes, copied configuration, a tiny Python project, and an artifact
directory. It reads no existing Tieru home.

Use these environment forms after changing into the copied `project` directory.

POSIX:

```bash
export TIERU_HOME=../homes/local-agent
export TIERU_CONFIG=../configs/local-agent.yaml
```

PowerShell:

```powershell
$env:TIERU_HOME = "../homes/local-agent"
$env:TIERU_CONFIG = "../configs/local-agent.yaml"
```

Do not point either variable at the checkout's `.tieru` directory.

## Demo 1 — Local Agent, Safe Actions, Full Replay

### Purpose

Show a request flowing through deterministic Fabric selection to the local
model, then through a governed tool request and explicit one-time authorization,
with the observable lifecycle available in Replay afterward.

Tieru deterministically selects an eligible execution mode and model according
to configured policy, capability, privacy, availability, and scoring. It does
not claim to select a universally best model.

### Commands and interaction

With the Demo 1 environment selected:

```bash
tieru doctor
tieru fabric status
tieru fabric explain "Remember the verified check for demo-project" --local
tieru
```

At the `you ›` prompt, enter:

```text
Remember that demo-project's verified check is python -m pytest -q, then summarize the preference.
```

The local model's wording may vary. The stable moment is a proposed
`save_note` action. The CLI displays the operation, safe target, risk,
capabilities, and reason, then asks `Allow once? [y/N]`. Review those fields and
approve this single synthetic write. The approval is not persisted as a policy.

Still in the CLI, inspect the explicit memory and exit:

```text
/memory
/quit
```

Then inspect the actual completed run:

```bash
tieru replay last --events
```

Capture Fabric mode/model metadata, the Trust request and decision, tool
lifecycle, timing, and result. Replay exposes bounded observable execution
metadata. It does not expose or store chain-of-thought; **private reasoning is
not recorded**.

### Expected interaction, not scripted output

- Doctor reaches `READY` when Ollama and the configured model are available.
- Fabric reports the configured `local_only` policy and an eligible local
  candidate.
- The model proposes a bounded memory write.
- Trust asks for one-time authorization; the presenter makes the decision.
- Replay shows the recorded route, authorization, and tool lifecycle.

If Gemma answers without using the memory tool, retry once with the same
explicit `Remember that ...` construction. Do not substitute manufactured
output. If the tool still is not proposed, retain the Doctor/Fabric footage and
record the Trust/Replay beat in a later clean take.

### Hero filming plan (45–90 seconds)

1. Show `tieru doctor` reaching `READY`.
2. Show `tieru fabric status` briefly; keep `local_only` and the local model in frame.
3. Start `tieru` and paste the one-sentence request.
4. Pause on the Trust request long enough to read operation, target, and risk.
5. Approve once.
6. Show the completed tool result and concise answer.
7. Exit and run `tieru replay last --events`.
8. End on the route/Trust/tool timeline, not on a long model paragraph.

## Demo 2 — From Repeated Workflow to Reusable Skill

### Purpose

Show explicit inspectable Memory, then three successful structurally compatible
workflows becoming a Shadow suggestion. The user explicitly hands the
suggestion to Forge; the result begins as an inactive draft, passes validation
and evaluation, and is installed only after review.

Keep these distinctions visible:

- repetition is not permission;
- a suggestion is not a skill;
- an installed skill is not action authority.

Shadow is passive repeated-workflow detection, not autonomous self-learning.
It only suggests; Forge does not run automatically.

### Commands and live interaction

Select the separate Demo 2 home.

POSIX:

```bash
export TIERU_HOME=../homes/shadow-forge
export TIERU_CONFIG=../configs/local-agent.yaml
```

PowerShell:

```powershell
$env:TIERU_HOME = "../homes/shadow-forge"
$env:TIERU_CONFIG = "../configs/local-agent.yaml"
```

Explicitly enable Shadow and verify its configured threshold:

```bash
tieru shadow enable
tieru shadow status
tieru
```

First add one explicit synthetic memory:

```text
Remember that my preferred check for demo-project is python -m pytest -q.
```

Review and approve the one-time memory write, then use `/memory`. Next, submit
these three requests as three separate turns, reviewing each scoped Trust prompt.
Tieru's `send_message` tool writes a draft to the local demo outbox; it does not
contact an external recipient:

```text
Draft a message to demo-reviewer-1 saying verification draft 1 is ready.
Draft a message to demo-reviewer-2 saying verification draft 2 is ready.
Draft a message to demo-reviewer-3 saying verification draft 3 is ready.
```

Exit with `/quit`, then inspect the real records:

```bash
tieru replay list --limit 5
tieru shadow patterns
tieru shadow suggestions
tieru shadow inspect <suggestion-id>
tieru shadow forge <suggestion-id>
tieru skill inspect <draft-id>
tieru skill validate <draft-id>
tieru skill evaluate <draft-id>
tieru skill install <draft-id>
```

Use the identifiers printed by the preceding commands. Review each Forge write
request. Before installation, `inspect` must show a draft state. Validation and
evaluation are separate, and installation asks for explicit review. A future
action proposed while using the installed skill still passes through Trust.

LLM tool selection may make the three live runs structurally different. Do not
lower Tieru's default threshold or manufacture a suggestion. Honest cuts between
successful runs are acceptable.

### Deterministic rehearsal option

For stable state-transition rehearsal without a model, create a second root
from the checkout:

```bash
python scripts/prepare_demo.py --root ../tieru-demo-fixture --seed-shadow-fixture
```

The helper creates three actual Replay rows through Tieru's Replay service and
passes them through the real Shadow observer. Every run is labeled
`deterministic_demo_fixture`, provider `deterministic`, and model
`fixture-no-model`. This is a model-free demonstration fixture, not live Gemma
footage. Never crop those labels or describe the fixture as a real model run.

### Secondary filming plan (60–120 seconds)

1. Show Shadow disabled in a fresh home, then run `tieru shadow enable`.
2. Show the explicit memory request and `/memory` result.
3. Use honest cuts across the three successful bounded workflows.
4. Show `tieru shadow suggestions` with occurrence count and evidence IDs.
5. Hand the suggestion to Forge explicitly.
6. Show the inactive draft, validation, and evaluation states.
7. Install only after review, then close on: skill metadata requests capabilities;
   Trust still authorizes actions.

## Demo 3 — Move Your Tieru Identity to a Fresh Installation

### Purpose

Show a portable offline snapshot moving selected Tieru-owned state from a
synthetic Tieru A to a separate clean Tieru B. Capsule is a snapshot, not cloud
sync. It carries Tieru-owned identity, not credentials. Integrity detects
corruption; it does not authenticate origin.

The preparation helper seeded Tieru A through canonical stores with one explicit
fact, one Memory Graph relation, one valid user skill, a synthetic identity
file, and local-only Fabric preference. It placed no credential in either home.

### Export and inspect Tieru A

POSIX:

```bash
export TIERU_HOME=../homes/capsule-a
export TIERU_CONFIG=../configs/local-agent.yaml
```

PowerShell:

```powershell
$env:TIERU_HOME = "../homes/capsule-a"
$env:TIERU_CONFIG = "../configs/local-agent.yaml"
```

Preview, export, and inspect using the actual Capsule CLI:

```bash
tieru capsule export ../artifacts/tieru-demo.tieru --dry-run
tieru capsule export ../artifacts/tieru-demo.tieru
tieru capsule inspect ../artifacts/tieru-demo.tieru
```

Capture the format/version, selected scopes, counts, integrity result, and
unencrypted status. Do not dump archive members. Credentials are excluded by
Capsule's source allowlists and secret scan; they are not an export scope.

### Prove Tieru B is clean, then import

Switch both variables; the separate destination config intentionally has no
Fabric section so the imported non-secret preference can fill it.

POSIX:

```bash
export TIERU_HOME=../homes/capsule-b
export TIERU_CONFIG=../configs/capsule-destination.yaml
```

PowerShell:

```powershell
$env:TIERU_HOME = "../homes/capsule-b"
$env:TIERU_CONFIG = "../configs/capsule-destination.yaml"
```

Show the empty counts, inspect the ImportPlan, then make the explicit import:

```bash
tieru capsule export ../artifacts/tieru-b-before.tieru --dry-run
tieru capsule import ../artifacts/tieru-demo.tieru --dry-run
tieru capsule import ../artifacts/tieru-demo.tieru
```

The dry-run should expose creates, skips, conflicts, path remaps when present,
and `trust_review_required`. Trust configuration travels only as an inactive
review artifact; import never silently activates source authority.

Verify the destination with public commands:

```bash
tieru capsule export ../artifacts/tieru-b-after.tieru --dry-run
tieru fabric status
tieru doctor --json
```

The post-import dry-run proves Memory/Graph/skill counts without exposing their
full bodies. Fabric status proves the selected non-secret preference arrived.
Doctor remains on the local-only path and should not report transferred cloud
credentials. Do not show the pending Trust file's contents.

### Portability filming plan (45–90 seconds)

1. Preview Tieru A counts with Capsule export dry-run.
2. Export and immediately inspect the real archive.
3. Switch to the visibly separate Tieru B home.
4. Show zero portable-state counts before import.
5. Pause on the dry-run ImportPlan and inactive Trust review.
6. Perform the explicit import.
7. Show destination counts and Fabric status.
8. End with: offline snapshot, no credentials, no silent authority.

## Recording stability and presentation

Design shots around stable structural events: Fabric mode, Trust request,
bounded tool result, Replay event, Shadow suggestion, Forge lifecycle state,
Capsule manifest, and ImportPlan. Do not depend on Gemma producing an exact
sentence.

A recording-friendly terminal should use approximately 100–120 columns, a
readable font, a clean prompt, and synthetic directory names. Hide unrelated
environment variables and avoid personal usernames or home paths. No particular
terminal application is required.

### Real model versus deterministic fixture

- **Real model demo:** uses Ollama and `gemma4:e2b`; use this for the hero and
  live Shadow footage. Model prose and tool selection can vary.
- **Deterministic fixture:** validates exact Replay/Shadow structural states
  without a model. Its labels must remain visible, and it must never be
  presented as live Gemma output.

## Privacy and redaction checklist

Before publishing, verify the frame contains none of the following:

- real usernames or unnecessary personal paths;
- API keys, tokens, cookies, OAuth files, `.env`, or gateway credentials;
- personal Memory, Replay, Capsule archives, or real `.tieru` state;
- unrelated shell history, notifications, repository remotes, or environment variables.

Use synthetic names such as `demo-user`, `demo-project`, and `example-editor`.
Capture Doctor JSON only after reviewing it. Replay payloads are bounded and
redacted, but the presenter must still inspect every frame before publication.

Never broaden Trust policy to smooth a recording. Every state-changing action
uses a scoped one-time decision. Repeated prompts are acceptable and reinforce
the product boundary.

## Product claim checklist

Use these factual descriptions:

- Tieru can maintain explicit, inspectable personal Memory and structured
  Memory Graph state; it does not remember every conversation automatically.
- Model Fabric selects an eligible route according to configured policy and
  evidence; it does not promise a universally best model.
- Replay stores safe observable execution metadata, not private reasoning.
- Shadow passively detects repeated workflow structure and may suggest a Forge
  draft; it neither learns autonomously nor installs anything.
- A skill remains subject to Trust on every proposed action.
- Capsule is a portable offline snapshot of selected Tieru-owned state, not an
  account, remote backup, or synchronization service.
- Capsule integrity checks detect alteration but do not prove who created it.

## README media plan

Do not add media references until real recordings exist and have been reviewed.
Recommended future repository paths and placement are:

- `assets/demo/tieru-hero.gif` near the README introduction;
- `assets/demo/forge.gif` near the Shadow/Skill Forge capability description;
- `assets/demo/capsule.gif` near the Capsule capability description.

Keep the hero visual short and silent-friendly. Add text alternatives and avoid
autoplay video dependencies. No placeholder binary belongs in the repository.

## Troubleshooting

- **Doctor is not READY:** start Ollama, confirm `gemma4:e2b` appears in
  `ollama list`, and rerun `tieru doctor` with the demo variables selected.
- **Fabric finds no eligible candidate:** confirm the copied configuration and
  the local model name; do not switch to a cloud profile for this demo.
- **No Trust prompt appears:** confirm the model actually proposed a tool and
  that the demo configuration is active. Do not weaken the policy.
- **Replay is empty:** complete a Tieru turn in the same disposable home, then
  run `tieru replay list` before selecting `last`.
- **Shadow has no suggestion:** confirm Shadow was explicitly enabled and that
  three completed runs have the same successful tool structure. Use honest
  additional runs or the labeled deterministic rehearsal fixture.
- **Forge validation fails:** read the reported structural/provenance failure;
  do not skip validation or evaluation.
- **Capsule import reports conflicts:** keep the existing destination state and
  show the conservative ImportPlan; use a freshly prepared Tieru B for the
  cleanest take.

## Cleanup

Leave the copied project, demo homes, Replay rows, Forge drafts, and Capsule
archives inside the marked disposable root. From the Tieru checkout, remove
only that root with the helper:

```bash
python scripts/prepare_demo.py --root ../tieru-public-demo --cleanup
```

For the optional deterministic rehearsal root:

```bash
python scripts/prepare_demo.py --root ../tieru-demo-fixture --cleanup
```

The helper refuses cleanup if its ownership marker is missing. It never deletes
the checkout or a user's Tieru home.

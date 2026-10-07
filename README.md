# Tieru

## Local-first Personal AI Runtime

**One memory. Any model. Your rules.**

Tieru is a local-first personal AI runtime with persistent memory, controlled
tool use, observable execution, reusable skills, model routing, and portable
identity. It can run entirely through a local Ollama model; API keys and cloud
providers are optional.

- **Remember:** explicit long-term memory plus a typed Memory Graph.
- **Act safely:** every requested tool action crosses the Trust Kernel and Action Ledger.
- **Execute durably:** multi-step tasks with Goal Contracts, deterministic checkpoints, and adaptive replanning.
- **See what happened:** Replay records bounded execution metadata and cryptographic evidence digests.
- **Reuse work:** Skill Forge drafts reviewed skills; Shadow can passively suggest candidates and is disabled by default.
- **Choose models:** Model Fabric and cognitive role routing select among local Ollama, subscription CLI, or configured cloud models.
- **Move your setup:** Capsule exports selective identity state without credentials.

Gemma is not Tieru's identity. Ollama with `gemma4:e2b` is simply the verified,
keyless local path for a first run.

## Why Tieru?

Personal AI needs more than a chat window: it needs durable context, constrained
actions, replaceable models, and evidence of what actually ran. Tieru keeps those
boundaries inspectable so you control the memory, model configuration, tools,
and permission policy.

## Five-minute Quick Start

### Prerequisites

For the verified local setup you need:

- Python 3.11 or newer
- [Ollama](https://ollama.com/) installed and running
- enough machine resources for the local model you select
- Git, when cloning the repository

Cloud credentials, Playwright, messaging gateways, and MCP servers are optional.
Install Ollama from its official distribution; Tieru does not install or start it.

### Windows PowerShell

```powershell
git clone https://github.com/QTrieu0610/tieru-agent.git
cd tieru-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If `python` does not select Python 3.11+, the Windows launcher form
`py -3.11 -m venv .venv` is an alternative. For PowerShell execution-policy
restrictions, activate from `cmd.exe` with `.venv\Scripts\activate`.

### Linux / macOS

```bash
git clone https://github.com/QTrieu0610/tieru-agent.git
cd tieru-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

After activation, `python` should resolve inside the virtual environment.

### Prepare the verified local model

```bash
ollama pull gemma4:e2b
```

Tieru never downloads a model automatically.

### Configure, check, run

```bash
tieru init
tieru doctor
tieru
```

Choose **Local only** in Init for the recommended first run. The generated
`.tieru/config.yaml` is discovered automatically, so plain `tieru doctor` and
`tieru` use it.

## Init and Doctor

`tieru init` offers three bounded choices:

1. **Local only** — all configured model execution stays local; recommended.
2. **Local first** — local by default, with cloud candidates only when explicitly added.
3. **Advanced** — bounded model, Fabric, Shadow, browser, and candidate choices.

Init does not install Ollama, download models, store API keys in YAML, configure
gateways or MCP, or loosen Trust permissions. Preview without writing:

```bash
tieru init --dry-run
```

`tieru doctor` checks Python, configuration, local state, models, Fabric, Trust,
Memory, Replay, skills, and optional integrations. Required failures produce
`NOT READY`; disabled optional components do not block a healthy local setup.
For share-safe machine-readable diagnostics:

```bash
tieru doctor --json
```

Doctor diagnoses only. It does not install dependencies, pull models, launch a
browser, start gateways, or start MCP servers.

## First Run

After Doctor reports `READY`, start the interactive terminal:

```bash
tieru
```

The same runtime is available through the loopback-only local dashboard:

```bash
tieru dashboard
```

The high-level path is:

```text
Your message
  -> Model Fabric plans an execution mode and eligible model
  -> The model responds or requests a tool
  -> Trust allows, denies, or asks for approval
  -> Replay stores safe execution metadata
```

Skills and Fabric modes can shape model behavior, but neither grants permission
to execute an action.

## Try These

Enter these as prompts inside the interactive `tieru` session.

### Basic conversation

> Explain what this repository does.

### Explicit memory

> Remember that I prefer concise technical explanations.

Tieru's default long-term memory-write policy is explicit; this wording asks for
a deliberate memory action rather than promising automatic fact extraction.

### Repository task

> Inspect this repository and summarize its structure.

Repository access requires an explicitly configured repository/filesystem tool
or reviewed MCP server. Trust may request approval. See the
[repository-agent example](examples/repository-agent/README.md).

### More involved analysis

> Analyze the architecture of this repository and identify the main components.

Fabric may choose a deeper execution profile from the task shape and current
configuration; no exact mode is promised.

## Trust: Models Request, Trust Decides

```text
Model requests an action
  -> Trust Kernel evaluates capability, risk, scope, and policy
  -> allow / deny / request approval
  -> only an allowed action reaches the tool
```

Unknown tools and unclassified actions fail closed. Process execution,
destructive work, browser side effects, external writes, and MCP startup remain
policy-controlled. See [Trust Kernel](docs/TRUST_KERNEL.md).

## Replay: Inspect a Run

```bash
tieru replay list
tieru replay last --events
```

Replay can show bounded execution-mode and selected-model metadata, Trust
decisions, tool events, failures, and timing. It does not record or reconstruct
private chain-of-thought. See [Replay](docs/REPLAY.md).

## Skill Forge: Turn Reviewed Runs into a Draft

```bash
tieru replay list
tieru skill forge <run-id>
tieru skill inspect <draft-id>
tieru skill validate <draft-id>
tieru skill evaluate <draft-id>
tieru skill install <draft-id>
```

Forge creates an inactive draft first. Installation follows human review and a
Trust-authorized local write; historical tool actions are never rerun. See
[Skill Forge](docs/SKILL_FORGE.md).

## Shadow: Optional Passive Suggestions

```bash
tieru shadow status
tieru shadow enable
tieru shadow suggestions
```

Shadow is disabled by default. When explicitly enabled, it observes repeated
successful workflow structure in Replay and may suggest a Forge candidate. It
does not execute tools, grant permission, or install skills. See
[Shadow](docs/SHADOW.md).

## Model Fabric: Explain Routing

```bash
tieru fabric status
tieru fabric modes
tieru fabric explain "analyze this repository"
tieru fabric models
tieru fabric stats
```

Fabric first chooses a bounded `QUICK`, `STANDARD`, `AGENT`, or `DEEP` execution
profile, then may select among explicitly configured eligible models. Privacy
and capability policy are hard filters: score never overrides exclusion. Explain
commands analyze the supplied text; they do not execute it. See
[Model Fabric](docs/MODEL_FABRIC.md).

## Capsule: Portable Identity, Not Live Sync

```bash
tieru capsule export my-tieru.tieru
tieru capsule inspect my-tieru.tieru
tieru capsule import my-tieru.tieru --dry-run
```

The default portable profile can include identity files, memory, user skills,
safe preferences, Fabric configuration, and Trust configuration as an inactive
pending-review artifact. Credentials, API keys, tokens, and private keys are
excluded. Replay, Forge, and Shadow history are opt-in rather than part of the
default portable identity. Capsule is an offline snapshot, not live sync. See
[Capsule](docs/CAPSULE.md).

## Durable Tasks: Goal-Governed Multi-Step Execution

```bash
tieru task create "audit dependencies and generate security report"
tieru task run <task-id>
tieru eval trace <task-id>
```

Durable tasks break complex workflows into transactional, evidence-producing
execution steps:

- **Goal Contracts:** Synthesizes objective success criteria, explicit negative
  constraints, and step limits before execution begins.
- **Action Ledger & Checkpoints:** Suppresses duplicate operations idempotently;
  each step generates a durable `ExecutionCheckpoint` with SHA-256 evidence digests.
- **Adaptive Replanning & Verifiers:** Evaluates observable checkpoints
  deterministically, adapting remaining steps when divergence occurs.
- See [Durable Tasks](docs/DURABLE_TASKS.md) and [Goal Contracts](docs/GOAL_CONTRACT.md).

## Cognitive Roles & Subscription Providers

```bash
tieru model roles
tieru eval roles
```

Tieru routes distinct cognitive roles (`planner`, `executor`, `replanner`,
`verifier`, `contract`) to optimal models based on empirical capability profiling:

- **Transparency:** Reports `configured_model`, `recommended_model`,
  `effective_model`, and `actually_executed_model` separately.
- **Subscription CLI Providers:** Routes through local, authenticated CLIs—**Claude Code**
  (`claude`), **Codex / ChatGPT** (`codex`), or **Antigravity** (`antigravity`)—to run
  frontier models without paying raw API token fees.
- See [Role-Aware Model Routing](docs/ROLE_AWARE_MODEL_ROUTING.md) and
  [Model Role Profiling](docs/MODEL_ROLE_PROFILING.md).

## Scheduler, Recovery & Reliability Evals

```bash
tieru schedule create --name health --every 1h "run repository health check"
tieru recovery list
tieru eval run
tieru eval compare baseline.json current.json
```

- **Scheduler & Recovery:** SQLite-backed recurrence (`tieru schedule tick`) and
  explicit human retry permits (`tieru recovery resolve-execution`).
- **Reliability Evals:** Isolated 14-case benchmark measuring completion rates and
  safety invariants. See [Reliability Evaluation](docs/RELIABILITY_EVAL.md).

## Core Capabilities

| Pillar | What is shipped |
|---|---|
| Memory Graph | Typed local entities and relations with provenance, confidence, time, and supersession |
| Trust Kernel | Deterministic capability, risk, scope, policy, and approval decisions |
| Replay | Local, bounded, redacted execution records and read-only inspection |
| Skill Forge | Provenance-backed inactive drafts, validation, evaluation, review, and installation |
| Shadow | Disabled-by-default passive workflow suggestions from successful Replay structure |
| Model Fabric | Execution profiles plus policy-first selection among configured candidates |
| Capsule | Selective, integrity-checked, offline identity export and preview-first import |
| Durable Tasks | Transactional, checkpointed multi-step plans with Goal Contracts and adaptive replanning |
| Action Ledger | Deterministic duplicate suppression and idempotent side-effect control in SQLite |
| Execution Checkpoints | SHA-256 evidence digests, attribution tracking, and deterministic step verifiers |
| Context Firewall | Structural boundary separating CONTROL, USER intent, and untrusted DATA |
| Cognitive Role Routing | Role-aware model selection (`planner`, `executor`, `verifier`) backed by empirical profiling |
| Subscription CLI Providers | Zero-cost integration with local Claude Code, Codex, and Antigravity subscriptions |
| Persistent Scheduler | SQLite-backed interval and one-shot schedules with crash-safe ticks |
| Human Recovery | Explicit human resolution permits for ambiguous executions and side effects |
| Capability Discovery | Multi-stage tool routing and schema projection without context explosion |
| Reliability Scorecard | Isolated 14-case live evaluation corpus with strict mathematical invariants |

Tieru also ships the interactive runtime, local dashboard, procedural skills,
Memory stores, optional browser tools, optional gateways, and MCP integration.

## Gateways: Multi-Platform Personal Assistant

Tieru connects to multiple chat and voice interfaces as gateways to the same
runtime without creating separate agent silos:

- **Telegram:** `tieru telegram` — isolated user sessions, document ingestion.
- **Discord:** `tieru discord` — bot token with guild/channel allowlists.
- **WhatsApp:** `tieru whatsapp` — personal automation with phone allowlists.
- **Voice:** `tieru voice` — real-time local audio loop with STT and TTS.

All gateways fail closed until required sender allowlists are configured.

## Bundled Skills & Token Discipline

- **Ponytail (`skills/community/ponytail`)**: Minimalist coding token discipline (YAGNI, stdlib-first).
- **Meeting Prep & Scheduling (`skills/schedule-meeting`)**: Calendar interaction and prep workflows.
- **Weekly Brief (`skills/weekly-brief`)**: Morning and weekly priority synthesis.

## How Tieru Works

```text
Gateway -> Runtime -> Working Context -> Agent Loop -> Governed Tools
                          |                 |
                       Memory          Fabric / Trust
                          |                 |
                         Graph           Replay
```

The canonical Python package is `tieru/`. Deep implementation details live in
[Architecture](docs/architecture.md) and the focused subsystem documents below.

## Configuration and Local Data

Configuration precedence is:

1. explicit CLI override
2. `TIERU_*` environment variable
3. deprecated `WAKU_*` compatibility fallback
4. selected or project-local YAML
5. built-in defaults

Tieru discovers project-local `.tieru/config.yaml` or `.tieru/config.yml`.
`--config` and `TIERU_CONFIG` select an explicit file. There is no separate
machine-wide YAML path searched automatically.

| Data | Default location |
|---|---|
| Configuration | `.tieru/config.yaml` after Init |
| Runtime database and Replay | `.tieru/state.db` |
| User-authored skills | `.tieru/skills/` |
| Local traces | `.tieru/traces/` |
| Dashboard-created Capsules | `.tieru/capsules/` |
| CLI-created Capsules | the path you explicitly provide |

YAML stores non-secret provider/profile metadata. Credentials stay in environment
variables. The complete documented reference remains
[`tieru/tieru.example.yaml`](tieru/tieru.example.yaml); smaller working configs
are in [`examples/`](examples/README.md).

## Local Models and Optional Cloud

Local-only onboarding needs no API key. `gemma4:e2b` is the verified Ollama
model; other model identifiers require user-supplied capability metadata and are
not implicitly certified by Tieru.

Without an Init-generated project config, the built-in verified profile remains
available explicitly:

```bash
tieru --profile ollama-gemma4-e2b doctor
tieru --profile ollama-gemma4-e2b doctor --json
tieru --profile ollama-gemma4-e2b
```

Cloud is optional and belongs after local onboarding. Adding an API key alone
does not enable cloud routing: Model Fabric also requires an explicit candidate
and compatible routing policy. See the
[local-plus-cloud example](examples/local-plus-cloud/README.md) and
[`.env.example`](.env.example).

## Security and Privacy

- State is project-local by default; cloud use is optional.
- API keys are optional and never belong in generated or example YAML.
- Trust governs tool actions independently of model and skill selection.
- Messaging gateways fail closed until required sender allowlists are configured.
- Shadow is disabled by default and cannot act or grant permission.
- Replay records bounded metadata, not private chain-of-thought.
- Capsule excludes credentials and imports Trust only for pending review.
- Cloud participation requires explicit Fabric configuration; credentials alone
  are insufficient.

These controls reduce risk; they are not a claim of absolute security. Keep the
dashboard loopback-only, review skills and MCP configuration like code, and read
the [Security Policy](SECURITY.md).

## Troubleshooting

Start with:

```bash
tieru doctor
```

- **Ollama not reachable:** start Ollama, then rerun Doctor.
- **Model missing:** run `ollama pull gemma4:e2b`.
- **Browser unavailable:** it is optional unless explicitly enabled; browser
  support also needs the optional Playwright dependency and browser binary.
- **Cloud credential missing:** it is optional unless an active profile or
  explicitly configured candidate requires it.
- **Gateway locked:** this is expected until the gateway's sender allowlist and
  required authentication settings are configured.

## Documentation

### User and product guides

- [Public examples](examples/README.md)
- [Public demo guide](docs/DEMO_GUIDE.md)
- [Fresh-install acceptance](PUBLIC_BETA_ACCEPTANCE.md)
- [Public-beta release notes](docs/RELEASE_NOTES_BETA.md)
- [Changelog](CHANGELOG.md)
- [Product identity](docs/PRODUCT_IDENTITY.md)
- [Roadmap and status](docs/ROADMAP.md)
- [Tieru specification](docs/TIERU_SPEC.md)

### Architecture and shipped pillars

- [Architecture](docs/architecture.md) | [Memory Graph](docs/MEMORY_GRAPH.md) | [Trust Kernel](docs/TRUST_KERNEL.md) | [Replay](docs/REPLAY.md)
- [Skill Forge](docs/SKILL_FORGE.md) | [Shadow](docs/SHADOW.md) | [Model Fabric](docs/MODEL_FABRIC.md) | [Capsule](docs/CAPSULE.md)
- [Durable Tasks](docs/DURABLE_TASKS.md) | [Goal Contract](docs/GOAL_CONTRACT.md) | [Action Ledger](docs/ACTION_LEDGER.md)
- [Checkpoints](docs/RUNTIME_ATTRIBUTION_AND_CHECKPOINTS.md) | [Context Firewall](docs/CONTEXT_FIREWALL.md) | [Role Routing](docs/ROLE_AWARE_MODEL_ROUTING.md)
- [Scheduler](docs/SCHEDULER.md) | [Human Recovery](docs/RECOVERY.md) | [Reliability Evaluation](docs/RELIABILITY_EVAL.md)

Historical and development notes remain under `docs/`; they are not required for
the five-minute path.

## Contributing and Project Status

The current source is a public-beta prerelease candidate; no GitHub prerelease
or PyPI package has been published yet. Before contributing, read
[CONTRIBUTING.md](CONTRIBUTING.md), the [Security Policy](SECURITY.md), the
[Code of Conduct](CODE_OF_CONDUCT.md), and the [MIT License](LICENSE).
Verification commands and contribution boundaries are documented there.

The maintained release gate is:

```bash
python -m tieru.ops.release_gate
```

See the [roadmap](docs/ROADMAP.md) for shipped work and remaining public-release
gates.

## Maintainer and License

Tieru is developed and maintained by
[Vo Quang Trieu](https://github.com/QTrieu0610).

Tieru is distributed under the [MIT License](LICENSE). The license file contains
the applicable copyright notices.

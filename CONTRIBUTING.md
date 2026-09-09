# Contributing to Tieru

Thank you for contributing to Tieru. This guide covers the external-development
contract; the user setup remains in the [README](README.md).

## Project contracts

- Repository: [QTrieu0610/tieru-agent](https://github.com/QTrieu0610/tieru-agent)
- Distribution package: `tieru-agent`
- Python namespace and CLI: `tieru`
- Environment variables: `TIERU_*`
- Local runtime directory: `.tieru`
- Supported Python: 3.11 and newer; CI currently exercises 3.11 and 3.12

## Development setup

Clone the repository and create an isolated virtual environment:

```bash
git clone https://github.com/QTrieu0610/tieru-agent.git
cd tieru-agent
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on Linux and macOS:

```bash
source .venv/bin/activate
```

Then install the contributor dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Core local checks

Run the complete test suite for behavior changes:

```bash
python -m pytest -q
```

Run the maintained release contract before a release-facing PR:

```bash
python -m tieru.ops.release_gate
```

Useful focused checks are:

```bash
python -m pytest -q evals/deterministic
python -m ruff check tieru evals scripts
python scripts/public_beta_acceptance.py
```

Normal contribution and CI checks need no API keys, cloud provider, live
Ollama, browser binary, gateway, or live MCP server. Deterministic tests use
mocks; judge/provider and explicitly opted-in live tests may skip honestly.
Fresh-install acceptance creates disposable consumer environments and does not
install into the contributor environment.

## Source map

| Path | Responsibility |
|---|---|
| `tieru/memory/` | Local memory stores, procedural skills, and Memory Graph |
| `tieru/trust/` | Capability, risk, scope, policy, and approval authority |
| `tieru/replay/` | Bounded execution records and inspection |
| `tieru/forge/` | Reviewed, provenance-backed skill drafts |
| `tieru/shadow/` | Optional passive repeated-work suggestions |
| `tieru/fabric/` | Execution modes and policy-first model selection |
| `tieru/capsule/` | Portable identity export, inspection, and import planning |
| `tieru/tools/` | Governed local and optional integration tools |
| `tieru/ops/` | Doctor, Init, dashboard, tracing, and release operations |

See [Architecture](docs/architecture.md) for the deeper system map.

## Contribution Footprint

Choose the smallest place that can solve the problem. Prefer extending an
existing path, then a procedural skill or documented CLI workflow, then an
optional tool/provider integration. Add core abstractions only when they have a
concrete second caller.

- **Small:** documentation, tests, example configs, or an isolated bug fix.
- **Medium:** bounded subsystem behavior or a new optional tool/provider adapter.
- **Large:** Trust, Memory, Model Fabric, persistence/schema, migrations, or a
  public CLI/configuration contract.

For medium or large changes, explain the security and privacy impact, backward
compatibility, migrations if any, and the tests that pin the new contract. This
is design context for review, not a separate approval process.

## Good first issues

Good starting work includes documentation corrections, deterministic tests,
small CLI UX bugs, example configs, isolated provider metadata, and low-risk
compatibility fixes. Check the
[current good-first-issue list](https://github.com/QTrieu0610/tieru-agent/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

Trust policy changes, Capsule importer security, Memory Graph migrations,
Fabric fallback, authentication, and other authorization boundaries usually
need broader context and are not good first issues.

## Engineering rules

- Preserve a usable local/no-cloud path and do not silently add cloud requirements.
- Trust remains the authority for actions; model, skill, or provider selection
  must never grant permission.
- Never log, serialize, commit, or paste credentials or sensitive headers.
- Keep optional dependencies and integrations behind appropriate extras or flags.
- Maintain public compatibility where practical; document migrations when it is not.
- Add deterministic coverage for behavior changes and regression coverage for fixes.
- Update public documentation when CLI, configuration, security, or user behavior changes.
- Never modify, stage, or commit `template_Agent.md`.

## Issues and pull requests

Use the repository templates for
[bugs](.github/ISSUE_TEMPLATE/bug_report.md),
[features](.github/ISSUE_TEMPLATE/feature_request.md),
[model/provider integrations](.github/ISSUE_TEMPLATE/model_provider.md), and
[documentation](.github/ISSUE_TEMPLATE/documentation.md). A minimal synthetic
reproduction and `tieru doctor --json` are safer than uploading local state.

Pull requests should stay focused, explain validation, and identify security,
privacy, Trust, compatibility, and documentation impact. CI runs the
[multi-OS workflow](.github/workflows/ci.yml) on pull requests and pushes to
`main`.

## Security, privacy, and support

Do not attach `.tieru/`, `state.db`, Capsule files, `.env`, OAuth files, API
keys, gateway tokens, private Memory, or personal Replay payloads. Prefer
Doctor JSON, bounded redacted Replay metadata, and synthetic data. Never disable
Trust or adopt an allow-all policy merely to make a reproduction pass.

Use GitHub Issues for non-sensitive bugs and feature requests. This project does
not promise commercial support or advertise an external support channel.
Potential vulnerabilities follow [SECURITY.md](SECURITY.md), not a public issue
containing exploit details.

## Release process

Maintainer release steps and versioning guidance live in
[docs/RELEASING.md](docs/RELEASING.md).

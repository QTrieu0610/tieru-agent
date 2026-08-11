# Tieru Public Beta

Candidate package version: `0.3.0b1`.

## What is Tieru?

Tieru is an open-source, local-first personal AI runtime: **One memory. Any
model. Your rules.** It brings persistent user-owned memory, model choice,
bounded tools, permissions, and inspectable execution into one Python runtime.
This is a pre-1.0 beta candidate, not a hosted service or a production-SLA
platform.

## Highlights

- **Memory Graph** adds typed, temporal, provenance-aware relationships to
  explicit local memory.
- **Trust Kernel** decides whether a model-proposed action is denied, approved
  once, or allowed within a configured scope before a tool executes.
- **Replay** records bounded execution events and decisions without storing
  private chain-of-thought.
- **Skill Forge** turns explicitly selected successful runs into inactive drafts
  for validation, review, and separately authorized installation.
- **Shadow** passively identifies repeated successful patterns. It is disabled
  by default and cannot grant permission or install a skill automatically.
- **Model Fabric** filters and scores explicitly configured local or cloud model
  candidates, with privacy and capability constraints plus bounded fallback.
- **Capsule** exports a selective, inspectable snapshot of identity state for a
  conservative dry-run and import into another Tieru home.

## First Run

After installing from a source checkout or an attached GitHub Release artifact:

```console
tieru init
tieru doctor
tieru
```

Choose **Local only** in Init for the recommended first run. Init does not
install Ollama, download a model, store API keys in YAML, or loosen Trust.

## Local-first

The verified keyless path uses Ollama with `gemma4:e2b`. Cloud providers and API
keys are optional. Tieru does not download models or silently activate cloud
routing; eligible cloud candidates must be explicitly configured.

## Security model

The execution boundary is: the model proposes, Trust decides, then the tool
executes. Empty gateway allowlists deny access. Browser automation is optional
and bounded. Merely configuring MCP does not authorize a process to start.
Replay stores normalized execution metadata, not hidden model reasoning.

## Portability

Capsule v1 can selectively package supported local identity data while excluding
credentials and runtime history. Inspection and an import dry-run precede any
write. Imported Trust grants remain inactive unless the user separately reviews
and authorizes them.

## Installation

For a source installation:

```console
git clone https://github.com/QTrieu0610/tieru-agent.git
cd tieru-agent
python -m venv .venv
python -m pip install -e .
```

When the GitHub prerelease exists, its attached wheel can instead be installed
with `python -m pip install <downloaded-wheel>`. Tieru is not being published to
PyPI as part of this beta, so `pip install tieru-agent` is not a supported beta
installation claim.

See the [README](../README.md), [public examples](../examples/README.md), and
[demo guide](DEMO_GUIDE.md) for platform-specific setup and bounded examples.

## Known limitations

- This beta is pre-1.0; public APIs and configuration may still evolve with
  migration notes.
- Local validation is complete, but hosted Windows, Ubuntu, and macOS GitHub
  Actions evidence remains pending until the candidate source is pushed.
- Doctor validates optional cloud configuration without making billable live
  provider calls.
- Capabilities for arbitrary Ollama models are user-supplied unless a model has
  verified metadata.
- Browser support is optional, and external gateways require manual setup and
  deny access when their allowlists are empty.
- Capsule v1 provides integrity checks, not signing, origin authenticity, or
  encryption.
- Shadow remains opt-in and disabled by default.
- Live model prose, latency, and tool selection vary. The verified Gemma model
  may answer directly instead of requesting a tool.

## Contributing

Read [CONTRIBUTING.md](../CONTRIBUTING.md), the
[community guide](COMMUNITY.md), and the [security policy](../SECURITY.md).
Bug reports and feature proposals belong in the repository's GitHub issue
templates; sensitive vulnerabilities should use private security advisories.

## Attribution

Tieru is maintained by Vo Quang Trieu and derives from
[ShenSeanChen/waku-agent](https://github.com/ShenSeanChen/waku-agent) by Sean
Chen. The upstream provenance and MIT license are preserved in the repository.

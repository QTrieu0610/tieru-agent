# Tieru

Tieru is a configurable personal AI agent with memory, tools, evaluation, document understanding, and controlled browser automation.

- **Local-first.** Your memory is one SQLite file. Open it. Read it. It's yours.
- **Memory is the hero.** Semantic + episodic + procedural — retrieval is gated, explicit saves are the default, and optional consolidation decides what to keep only when enabled.
- **The loop is ~95 lines** of plain Python. Step through it.
- **Watch it think.** A local dashboard lights up every message as it flows through the harness.
- **Eval built in.** Deterministic tests *and* LLM-as-judge, side by side, with a release gate.

## Overview

Tieru is designed to be a transparent, customizable, local-first personal AI assistant engine. It focuses on clean separation of agent harness, loop execution, long-term memory management, and deterministic evaluation.

## Current features

- **Configurable Agent Harness**: Clean CLI, API, and environment variable configuration resolution.
- **Multi-Pillar Memory System**: Working memory, semantic FTS5/Supabase store, episodic store, and procedural skill loader.
- **Extensible Tool Registry**: Built-in system tools (workspace, search, memory admin) plus Model Context Protocol (MCP) client support.
- **Controlled Browser Automation**: Restricted Playwright browser integration built with safety limits.
- **Evaluation & Release Gate**: Comprehensive deterministic test suite and LLM-as-judge pipelines.
- **Dashboard**: No-build local dashboard for observing agent execution traces, memory stores, and model configs.

## Architecture

Tieru separates responsibilities cleanly across core subsystems:

```
tieru/
  ├── loop/      # Agent execution loop and message management
  ├── runtime/   # Session handling and state resolution
  ├── memory/    # Working, semantic, episodic, and procedural memory stores
  ├── tools/     # Tool definition, discovery, and MCP client interface
  ├── gateway/   # Multi-channel integrations (CLI, web API, gateways)
  ├── graph/     # Workflow engines and DAG processing
  └── ops/       # Dashboard, metrics, release verification, and evaluation
```

## Agent harness

The agent harness manages lifecycle events, user sessions, environment loading (`TIERU_*`), and CLI flag parsing.

## Agent loop

The core loop in `tieru.loop` executes model invocations, handles tool calls, updates memory, and streams progress to configured outputs.

## Memory

Tieru provides a three-layer memory strategy:
- **Semantic Memory**: Persistent facts stored in SQLite (or optional Supabase vector DB).
- **Episodic Memory**: Conversational history and turn logs.
- **Procedural Memory**: Skills and instructions loaded dynamically into context.

## Tools

Tools are deny-by-default and explicitly registered. Tieru supports system tools, workspace manipulation, and external MCP servers.

## Evaluation

Run evaluation suites offline without external credentials:
```bash
python -m pytest -q evals/deterministic
```

Run live LLM-as-judge benchmarks (requires API credentials):
```bash
python -m pytest -q evals/judge
```

## Document understanding

Tieru handles local document parsing and workspace file manipulation safely with bounded size limits.

## Permission model

Destructive system operations, external network requests, and file modifications require explicit user policy or confirmation handlers.

## Restricted browser automation

Tieru includes a restricted Playwright-based browser agent. It runs in a headless environment restricted to non-authenticated browsing, navigation, and structured extraction.

## Dashboard

Launch the no-build dashboard:
```bash
tieru dashboard
```
Or run as a python module:
```bash
python -m tieru.ops.dashboard
```

## Installation

Clone the repository:
```bash
git clone https://github.com/QTrieu0610/tieru-agent.git
cd tieru-agent
```

Install in editable mode:
```bash
python -m pip install -e .
```

## Configuration

Tieru resolves configuration with strict precedence:
1. CLI flags (`--model`, `--provider`, etc.)
2. Environment variables (`TIERU_MODEL`, `TIERU_PROVIDER`, `TIERU_HOME`)
3. Project configuration (`tieru.yaml`)
4. Global defaults (`~/.tieru/tieru.yaml`)

Default runtime directory: `~/.tieru`

## LLM provider configuration

Set your provider credentials using standard environment variables:
```bash
export OPENAI_API_KEY="your-key"
export ANTHROPIC_API_KEY="your-key"
```

## Quickstart

Run Tieru in interactive CLI mode:
```bash
tieru
```

Run a single prompt:
```bash
tieru "Summarize recent project updates"
```

## Testing

Run the full verification suite and release gate:
```bash
python -m tieru.ops.release_gate
```

Run deterministic unit tests:
```bash
python -m pytest -q
```

## Security

Security principles:
- **Secrets redact**: API keys and headers are redacted at boundaries.
- **Sandbox boundaries**: Workspace access is restricted to approved subdirectories.
- **No telemetry secrets**: No credentials or private traces leave your local environment.

## Roadmap

- Extended MCP server support.
- Performance optimizations for memory retrieval.
- Additional local model provider integrations.

## Maintainer

Tieru is developed and maintained by
[Vo Quang Trieu](https://github.com/QTrieu0610).

## Source and attribution

Tieru was originally based on the open-source project
[ShenSeanChen/waku-agent](https://github.com/ShenSeanChen/waku-agent),
created by [Sean Chen (ShenSeanChen)](https://github.com/ShenSeanChen).

Tieru is currently developed and maintained independently by
[Vo Quang Trieu](https://github.com/QTrieu0610).

The current repository includes modifications to project branding,
configuration, model integration, memory, permissions, browser automation,
evaluation, packaging, deployment, and other application-specific components.

The original project's applicable copyright and license notices are preserved
in the [LICENSE](LICENSE) file.

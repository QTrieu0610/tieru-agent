# Contributing to Tieru

Thank you for contributing to Tieru!

Tieru is a configurable, local-first personal AI assistant engine designed with clear boundaries across the agent harness, execution loop, memory, tools, and evaluation.

## Environment & Project Contracts

- **Repository**: [QTrieu0610/tieru-agent](https://github.com/QTrieu0610/tieru-agent)
- **Distribution Package**: `tieru-agent`
- **Python Namespace**: `tieru`
- **CLI Command**: `tieru`
- **Environment Variables**: `TIERU_*`
- **Runtime Directory**: `.tieru`
- **Maintainer**: Vo Quang Trieu

## Development Setup

1. Clone the repository and navigate to its directory:
   ```bash
   git clone https://github.com/QTrieu0610/tieru-agent.git
   cd tieru-agent
   ```

2. Install in editable mode:
   ```bash
   python -m pip install -e .
   ```

## Development & Verification Commands

- Run full release gate:
  ```bash
  python -m tieru.ops.release_gate
  ```
- Run deterministic unit test suite:
  ```bash
  python -m pytest -q evals/deterministic
  ```
- Run lint checks:
  ```bash
  python -m ruff check tieru evals scripts
  ```

## Pull Request Checklist

Before submitting a Pull Request, ensure:
1. All deterministic tests and release gate checks pass cleanly.
2. Code follows the `tieru` namespace and `TIERU_*` environment variable conventions.
3. No secrets, credentials, API keys, SQLite databases, or local runtime data (`.tieru/`) are committed.
4. `template_Agent.md` is never modified, staged, or committed.
5. All new capabilities include appropriate deterministic unit tests.

## Security & Secrets Policy

- Never commit `.env`, `credentials.json`, or OAuth token files.
- Redact credentials and sensitive headers at all system boundaries.

# Public Beta Fresh-Install Acceptance

R3.1 validates Tieru as a new user receives it, outside the source checkout and
without an editable installation. It complements CI: R3.0 proves contributor
compatibility from source, while this acceptance proves the built artifacts.

## Maintainer command

From a contributor environment with `.[dev]` installed, run:

```bash
python scripts/public_beta_acceptance.py
```

The command builds a wheel and source distribution, audits their contents, and
tests both by default. Use `--artifact wheel` or `--artifact sdist` only for a
focused diagnostic; release acceptance should use the default `both` mode.

## Isolation contract

Each artifact receives its own operating-system temporary directory containing:

- an empty consumer project;
- a new virtual environment;
- an empty synthetic user home;
- an empty `TIERU_HOME` at `<consumer-project>/.tieru`.

The consumer install is `pip install --no-cache-dir <artifact>` without editable
mode or optional development dependencies. `TIERU_*`, legacy `WAKU_*`, provider
keys, gateway tokens, `PYTHONPATH`, and the maintainer virtual environment are
not inherited. The runner proves that `tieru` imports from the consumer venv and
that Pytest, Ruff, Build, and Twine are absent there.

## User flow

For both the wheel and sdist, acceptance performs:

1. Installed CLI and package-metadata smoke checks.
2. `tieru init --non-interactive --yes` with the safe local-only defaults.
3. `tieru doctor --json`, requiring `ready: true` and installed distribution metadata.
4. `tieru replay list --json` on the new empty history.
5. `tieru fabric status` and `tieru shadow status`.
6. The installed interactive `tieru` gateway with `/memory` then `/quit`, proving
   first-run application and SQLite startup without making a model call.
7. A Capsule export dry-run, requiring that no archive is written.

Ollama and cloud credentials are not installation requirements. Doctor may
return `READY_WITH_WARNINGS` when the configured local model is unavailable;
that is an honest ready state with remediation. No browser, gateway, MCP server,
or provider call is started.

All artifacts, venvs, homes, projects, configuration, and runtime databases made
by the acceptance command are removed automatically after the run. Nothing is
installed into the maintainer environment and no package is published.

## Verified R3.1 result

On 2026-08-11, the complete default flow passed locally on Windows with Python
3.12:

- wheel: Tieru 0.2.0, Doctor `READY`, 27 runtime packages;
- sdist: Tieru 0.2.0, Doctor `READY`, 27 runtime packages;
- all Init, Doctor, Replay, Fabric, Shadow, interactive gateway, and Capsule checks passed;
- temporary artifacts, venvs, homes, and projects were removed.

This is fresh-install evidence for the local Windows host. It does not claim a
hosted GitHub Actions run or replace R3.0's multi-OS workflow verification.

## R4.1 candidate re-check

After freezing the beta version on 2026-08-11, the same isolated default flow
was rebuilt and repeated from scratch on Windows with Python 3.12:

- wheel: Tieru 0.3.0b1, Doctor `READY`, 27 runtime packages;
- sdist: Tieru 0.3.0b1, Doctor `READY`, 27 runtime packages;
- artifact audit and every documented user-flow check passed;
- temporary artifacts, venvs, homes, and projects were removed.

This re-check confirms the final candidate metadata and fresh-install behavior.
Hosted GitHub Actions evidence remains pending until the source is pushed.

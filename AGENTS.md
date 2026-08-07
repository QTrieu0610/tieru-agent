# Repository guide

## Repository layout

- `tieru/`: canonical Python package; `loop/`, `runtime/`, `memory/`, `tools/`, `gateway/`, `graph/`, and `ops/` own the runtime. `tieru/ops/static/` is the no-build dashboard.
- `evals/deterministic/`: offline-first pytest suite. `evals/judge/` requires provider credentials and DeepEval.
- `docs/`, `scripts/`, `skills/`, `examples/`, `sql/`: documentation, maintenance, bundled skills, examples, and optional backend schema.

## Verified commands

- Install: `python -m pip install -e .`
- CLI: `tieru --help`
- Deterministic: `python -m pytest -q evals/deterministic` (`make eval`)
- Judge: `python -m pytest -q evals/judge` (`make eval-judge`; live keys required)
- Release gate: `python -m tieru.ops.release_gate` (`make gate`)
- Lint: `python -m ruff check tieru evals scripts` (`make lint`)

## Engineering rules

- Never hard-code a model/provider in business logic; resolve `main`, `small`, and `judge` through validated configuration.
- Never log, trace, serialize, or commit secrets. Redact credentials and sensitive headers at boundaries.
- Treat tools as deny-by-default: scope paths/hosts/recipients, bound time/output, and require explicit policy for external writes, execution, browser actions, or destructive work.
- Before handoff, run relevant tests and lint, inspect `git status` and the full diff, and report failures/skips honestly. Never change an expectation only to make a baseline green.
- Done means requested behavior and compatibility paths work, relevant tests/lint pass except documented baselines, live tests are reported honestly, docs match behavior, no secrets/runtime data or unrelated changes are present, and license/attribution is preserved.

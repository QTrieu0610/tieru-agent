# Tieru — working conventions

**Tieru** — a local-first personal AI runtime built around Memory, Skills, Models,
Trust, Replay foundations, and Tools. The bar for every change is **clear, honest
code a newcomer can follow**: each subsystem stays legible on its own. New scope
is welcome when it is self-contained, tested, and readable; complexity for its
own sake is not.

The canonical technical contracts are the `tieru-agent` distribution, `tieru` package and command, `TIERU_*` environment variables, and `.tieru` runtime state. Deprecated Waku contracts remain compatibility fallbacks only.

## Architecture map (file ↔ subsystem)

- `tieru/app.py` — application assembly and per-turn runtime lifecycle.
- `tieru/gateway/` — terminal, voice, Telegram, Discord, and WhatsApp text boundaries.
- `tieru/runtime/session.py` — bounded working-context assembly.
- `tieru/loop/` — agent loop plus provider adapters and `ModelRouter`.
- `tieru/memory/` — semantic, episodic, procedural, and graph memory.
- `tieru/trust/` — centralized capability, risk, scope, and approval decisions.
- `tieru/replay/` — normalized local run/event inspection; JSONL tracing remains separate.
- `tieru/forge/` and `tieru/shadow/` — reviewed skill drafting and disabled-by-default
  repeated-workflow suggestions.
- `tieru/fabric/` — opt-in execution modes and policy-first configured model selection.
- `tieru/capsule/` — bounded, integrity-checked offline export/import.
- `tieru/tools/` — classified built-ins, optional MCP, and restricted browser tools.
- `tieru/graph/` — opt-in workflows around the unchanged agent loop.
- `tieru/ops/` — local dashboard, tracing, approvals, and release verification.
- `evals/deterministic/` is offline-first; `evals/judge/` requires live credentials.
- Runtime state lives in `.tieru/` and is always gitignored.

## Rules

- **Be concise.** Lead with the answer, cut preamble and recap, and expand only
  when the task needs the detail.
- **Never wipe runtime data without asking first, every time.** `scripts/demo_seed.py`
  and anything else that clears `.tieru` (memory, calendar, chat log, traces, or the
  `usage.jsonl` spend ledger) must be proposed and explicitly approved by the user
  *immediately before each run*. Permission never carries over from a previous run.
  The script backs up first, but restoring is a hassle — ask, wait for a clear yes,
  then run. It refuses to do anything without the `--yes` flag for this reason.
- **Version control — commit AND ship every milestone, same turn.** The moment a change
  works (tests pass / verified live), commit it with a detailed message (subject = what,
  body = WHY + what it survived) and get it onto GitHub before moving on. Never end a
  turn or session with working changes left uncommitted — the repo must always be traceable
  from GitHub, and uncommitted work has been lost to branch switches before. Use the `/ship`
  skill. If several milestones land in one session, commit each as its own logical commit.
- **`main` is protected — `git push origin main` is REJECTED, for everyone.** Since
  2026-07-26 a commit only lands once `skills-and-evals` is green, and `enforce_admins`
  is on, so the rule binds Sean and Claude identically. Ship via
  `git checkout -b <topic>` → `gh pr create --fill` → `gh pr checks --watch` (~30s) →
  `gh pr merge --squash --delete-branch`. `GH006: Protected branch update failed` is the
  guard working; never route around it. Merging a COMMUNITY PR still needs Sean's
  explicit per-PR yes (see `.claude/skills/review-pr/SKILL.md`).
- **Gate before push**: `make gate` (deterministic must pass; judge runs with a key).
  When a live bug is found, fix it AND add a regression case to `evals/deterministic/`.
- **No emojis** in any UI surface (dashboard, CLI output, README prose).
- **No new dependencies without discussion** — the core is stdlib + anthropic/openai.
  Optional features go behind extras (`[voice]`, `[telegram]`, ...).
- **Footprint ladder — where new capability goes.** Every registered tool ships in
  every applicable prompt, so the core stays narrow and capability lives at the
  edges. Follow the contribution ladder in `CONTRIBUTING.md`.
- **Scope**: scheduling is the flagship teaching task, but the project is growing toward a
  full assistant. New capabilities (providers, tools, gateways, integrations) are welcome
  when they're self-contained, tested, and keep the core legible. Reject only complexity
  that muddies how the system works or bloats the default path — prefer opt-in extras.
- Providers are framed neutrally in docs (Anthropic, OpenAI, Gemini, DeepSeek, Kimi, GLM,
  OpenRouter) — no ranking, no "open-source vs closed" framing.

## Commands

`make run` · `make voice` · `make dashboard` (7777) · `make trace` (6006) ·
`make eval` · `make gate` · `make lint` · tests live under `evals/`, not `tests/`

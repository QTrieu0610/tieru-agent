# Tieru product identity

## Product definition

- **Name:** Tieru
- **Category:** Local-first Personal AI Runtime
- **Core line:** One memory. Any model. Your rules.
- **One-sentence description:** Tieru is an extensible personal AI runtime that
  keeps memory, skills, model choice, tools, permissions, and operational
  evidence under the user's control.

Tieru remembers you, learns how you work, chooses how to think, and never acts
beyond your rules.

## Core philosophy

Tieru treats personal AI as a runtime rather than a single assistant persona or
model. The runtime assembles context, routes model roles, exposes approved
capabilities, records outcomes, and keeps each boundary understandable. Users
should be able to inspect what Tieru remembers, change how it works, swap the
models it uses, and constrain what it may do.

## Principles

### Local-first

Local state is the default: SQLite memory, skills, traces, configuration, and
generated artifacts live under the user's Tieru home. Local-first does not mean
that every optional integration is offline. Hosted models, cloud stores, MCP
servers, and external tools cross the local boundary only when configured and
should remain visible and permission-scoped.

### User-controlled

The user controls memory writes and deletion, provider/model roles, optional
integrations, tool policy, and approval behavior. Defaults favor inspectability,
bounded execution, and explicit long-term memory writes.

### Model-independent

Tieru is not a wrapper for one model. Models belong to configurable `main`,
`small`, and `judge` roles behind provider adapters. Ollama with Gemma 4 E2B is
the verified local target; Gemma is a backend profile, not Tieru's identity.

## Current product pillars

| Product idea | Current subsystem | Current status |
|---|---|---|
| Remember | **Tieru Memory** — semantic, episodic, procedural, graph, and working context | Shipped |
| Learn | **Tieru Skills** — matched reusable `SKILL.md` procedures | Shipped |
| Think | **Tieru Model Layer** and **Tieru Model Fabric v2** — provider/role resolution, execution-mode routing, and policy-first configured model selection | Shipped through M12 |
| Act | **Tieru Tools** — built-ins, MCP, and restricted browser capabilities | Shipped, with optional features gated |
| Verify | **Tieru Trust Kernel**, **Tieru Replay**, **Tieru Shadow**, tracing, and evaluation | Shipped through M10 |
| Move | **Tieru Capsule** — selective local snapshots and conservative import | Shipped in M13 |

The canonical current subsystem names are **Tieru Runtime**, **Tieru Memory**,
**Tieru Skills**, **Tieru Skill Forge**, **Tieru Model Layer**, **Tieru Model Fabric v2**, **Tieru Trust Kernel**,
**Tieru Replay**, **Tieru Shadow**, **Tieru Capsule**, and **Tieru Tools**.
**Tieru Replay** is the shipped local, read-only inspection subsystem over
observable run events. Tracing and evaluation remain distinct supporting systems.

## Milestone product vocabulary

The **Tieru Memory Graph** shipped in M6, **Tieru Trust Kernel** shipped in M7,
**Tieru Replay** shipped in M8, **Tieru Skill Forge** shipped in M9, and
**Tieru Shadow** shipped in M10, Model Fabric execution modes shipped in M11,
**Tieru Model Fabric v2** shipped in M12, and **Tieru Capsule** shipped in M13.

## Status language

- **Shipped** means the capability exists in source, is user-accessible under
  its documented configuration, and has relevant verification.
- **Foundation exists** means current code supports part of a future direction,
  but the named future product is not implemented.
- **Planned** means roadmap intent only. Planned work must never be written in
  present tense or included in current-capability lists.

## Terminology rules

- Describe Tieru as a **local-first personal AI runtime**.
- Use **runtime** for the product and lifecycle; use **agent loop** for the model
  and tool-call cycle inside it.
- Use **Tieru Memory** for the complete subsystem and **Tieru Memory Graph** for
  its shipped typed-relationship capability.
- Use **Tieru Model Layer** for provider/client construction and **Tieru Model
  Fabric v2** for M11 execution modes plus M12 policy-first selection. Do not
  describe it as autonomous cloud purchasing or permission routing.
- Use **Tieru Trust Kernel** for the shipped authorization boundary. Do not call
  authorization itself Replay; Replay only inspects its safe decisions.
- Use **Tieru Replay** for the shipped stable-run, normalized-event inspection
  surface. JSONL traces and evaluation remain separate capabilities.
- Treat Gemma 4 E2B as a verified local model target, never as the brand.
- Keep compatibility identifiers exact in code and migration documentation.

## License and compatibility rules

The MIT license, copyright notices, repository history, historical artifacts,
and compatibility identifiers must remain intact. Tieru-specific development
and product direction must not misrepresent the authorship of inherited work.

## What Tieru does not claim after M13

Tieru does not claim cloud synchronization, account identity, remote Capsule
backup, or Capsule encryption/authenticity. Replay does
not expose chain-of-thought or execute/fork/retry past
actions. Trust Kernel is not an OS sandbox, antivirus, autonomous
policy generator, or general shell-security parser. Tieru does not claim that
all optional integrations are offline or that one model is required. Fabric v2
does not train a router, scrape live prices, auto-enable cloud, or escalate for
subjective answer quality.
Shadow
observes only successful structured Replay metadata; it does not monitor wider
activity, replay side effects, grant permission, or automatically create or
install anything.

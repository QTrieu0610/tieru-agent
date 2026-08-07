# Security

Tieru runs on your own machine, with your own API keys, and reads your own
calendar, notes and messages. That makes a few things worth stating plainly.

## Reporting a vulnerability

Please **don't** open a public issue for a security problem. To report a vulnerability, use GitHub's private vulnerability reporting:

https://github.com/QTrieu0610/tieru-agent/security/advisories/new

If private vulnerability reporting is not available, contact the maintainer directly via GitHub profile:

https://github.com/QTrieu0610

## What we consider in scope

- Anything that exfiltrates keys, `.env`, memory (`state.db`), traces, or
  message contents off the machine.
- Code executing at install time, or a dependency doing so.
- A gateway or webhook accepting instructions it shouldn't — an unsigned or
  unauthenticated inbound request that can drive the agent.
- Prompt injection that leads to a real side effect (a tool call, a file write,
  a message sent) rather than just a strange reply.

## What isn't a vulnerability

- **The agent can run tools that touch your stuff.** That's the product. Tools
  are listed in the dashboard; default tools can update local state, while optional
  integrations are gated behind their extras and flags.
- **`TIERU_EXPERIMENTAL=1`** enables sub-agent delegation, which runs another
  coding agent locally. It's off by default and documented as experimental.
- **Your own API keys in your own `.env`.** Tieru sends each key only to the
  provider or integration service that key configures.

## Running it safely

- Keep `.env` out of git — it's gitignored, and so are `credentials.json` and
  `*token*.json`.
- Inbound gateways (WhatsApp-style webhooks) must verify request signatures
  before acting. Outbound ones (Telegram, Discord) dial out and aren't exposed.
- Review a community skill or extension before installing it. A `SKILL.md` is
  instructions to a model that can call tools — read it like code.

## Release-candidate controls

- The dashboard binds to loopback. Every mutation endpoint requires an HttpOnly,
  SameSite session cookie, an exact loopback `Origin`, and a per-session CSRF token.
- Dashboard approvals are only a transport for the centralized tool-permission gate.
  They show redacted arguments and are bound to session, tool, and an opaque keyed
  argument fingerprint. They are single-use, expire quickly, and deny on timeout,
  malformed resolution, or chat-stream disconnect.
- Browser page text is marked untrusted before it reaches the model. The optional
  browser rejects credentials in URLs, non-HTTP schemes, destinations outside an
  explicit domain allowlist, and non-public resolved addresses. Redirect requests
  cross the same check. Downloads, service workers, logins, arbitrary JavaScript,
  and unrestricted actions are not exposed.
- Memory rejects credential-shaped content and treats browser-derived records as
  untrusted. Traces and tool results pass through secret redaction.
- Subprocess calls use explicit argument vectors. Coding verification does not use
  a shell; extensionless Python scripts are launched with the active interpreter.

These controls reduce the local attack surface; they do not turn the dashboard into
a remote multi-user service or make model output trustworthy. Keep it loopback-only,
review configured tools and skills, and use the default deny/confirm policies.

## Dependency review

The base package does not require Playwright. The `browser` extra installs the
Python Playwright client, and browser binaries remain a separate explicit install.
PyYAML is a base dependency used with `safe_load` for versioned non-secret config.
At M4 review time, installed package metadata reported Apache-2.0 for Playwright and
MIT for PyYAML. Those are third-party licenses; Tieru's existing MIT LICENSE and
upstream attribution are unchanged.

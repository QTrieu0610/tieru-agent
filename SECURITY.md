# Security Policy

Tieru handles local personal state, model credentials, tools, and optional
external integrations. Trust and privacy boundary failures are treated as
security issues.

## Supported versions

Tieru is pre-1.0. Security work targets the current `main` branch and the newest
published public-beta or prerelease line. Older 0.x and upstream-era tags are
not routinely supported; reporters should still disclose a credible issue if
it may remain present in current code.

## Reporting a vulnerability

Do not open a public issue containing exploit details, real secrets, or private
user data.

If the repository Security page offers **Report a vulnerability**, use GitHub's
private reporting form:

<https://github.com/QTrieu0610/tieru-agent/security/advisories/new>

Private reporting availability is controlled by the repository's GitHub
settings. Confirm that GitHub presents a private form before entering details.
If it is unavailable, open a
[minimal public issue](https://github.com/QTrieu0610/tieru-agent/issues/new)
asking the maintainer to establish a private contact path. Do not include the
vulnerability, logs, proof of concept, or affected data in that public issue.

Include a minimal synthetic reproduction, affected version/commit and platform,
impact, and suggested mitigation when known. Allow time for acknowledgement and
coordination before public disclosure.

## Security report scope

Examples include:

- Trust bypass, scope expansion, or approval confusion that enables an action;
- credential, Memory, Replay, trace, message, or local-file disclosure;
- Capsule traversal, integrity, collision, or unsafe import behavior;
- unauthenticated or incorrectly authorized gateway/dashboard actions;
- browser or MCP boundary bypass, SSRF, or unsafe subprocess execution;
- install-time execution or dependency behavior that compromises users;
- prompt injection that crosses Trust and causes a real side effect.

Unexpected model output without a boundary violation is generally a product bug,
not a vulnerability. An explicitly approved tool performing its documented
action is not itself a vulnerability, though misleading approval or excess
scope may be.

## Privacy-safe reporting

Never attach or paste:

- `.tieru/` or `state.db`;
- complete private Memory or personal chat content;
- raw Replay payloads or trace files containing personal data;
- Capsule archives;
- `.env`, OAuth files, cookies, credentials, API keys, or gateway tokens.

Prefer `tieru doctor --json`, redacted bounded Replay metadata, synthetic
records, and the smallest reproduction that demonstrates the boundary. Doctor
JSON is designed to be share-safe, but review any diagnostic before posting it.

## Running Tieru safely

- Keep the dashboard loopback-only and do not expose it as a remote multi-user service.
- Keep secrets outside YAML and version control.
- Do not disable Trust or use an allow-all policy to debug a report.
- Treat `.tieru/mcp.json`, community skills, and extensions like executable code.
- Keep gateway sender allowlists explicit; an empty allowlist fails closed.
- Review optional provider, browser, and tool configuration before enabling it.

Tieru rejects unknown actions by default, redacts bounded runtime records, and
keeps browser, gateway, MCP, Capsule, and tool behavior behind explicit policy
boundaries. These controls reduce risk; they do not make model output trusted or
guarantee absolute security.

## Dependencies and attribution

Optional integrations retain their own security and licensing responsibilities.
The base package does not install browser binaries, start external services, or
configure credentials. Tieru remains MIT-licensed with its upstream attribution
preserved in [LICENSE](LICENSE).

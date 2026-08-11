# GitHub Community Recommendations

These are maintainer recommendations for GitHub server-side settings. They do
not claim that repository About text, topics, labels, Discussions, or badges
have already been configured.

## About description

> Local-first personal AI runtime with memory, safe tools, replay, reusable
> skills, model routing, and portable identity.

## Topics

`ai-agent`, `local-ai`, `personal-ai`, `ollama`, `llm`, `agent-memory`,
`model-routing`, `mcp`, `python`, `privacy`

## Labels

- General: `bug`, `enhancement`, `documentation`, `question`
- Community: `good first issue`, `help wanted`
- Subsystems: `memory`, `trust`, `replay`, `forge`, `shadow`, `fabric`,
  `capsule`, `tools`, `provider`, `onboarding`, `ci`
- Risk: `security`, `privacy`, `breaking-change`

Keep labels orthogonal and add them only when there is real issue volume. Do not
create milestone-specific labels for completed internal work.

## Support routing

Use GitHub Issues for non-sensitive bugs, documentation problems, and feature
requests. Security reports follow [SECURITY.md](../SECURITY.md). Do not promise
commercial support or advertise Discussions unless it is actually enabled and
maintained.

## Badge decision

Defer the CI badge until the workflow has a committed remote path and at least
one hosted run. At that point, a small set of CI, Python, and MIT License badges
is sufficient. Do not add test-count or synthetic readiness badges.

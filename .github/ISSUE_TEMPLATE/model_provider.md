---
name: Model or provider integration
about: Propose verified Model Fabric metadata or an optional provider adapter
title: "[Provider] "
labels: enhancement, provider
assignees: ""
---

## Provider and model

- Provider:
- Exact model identifier:
- Local or cloud:
- Documentation/source for capability metadata:

## Protocol compatibility

Describe the API/wire protocol and why Tieru's existing generic adapters are
insufficient.

## Verified capabilities

- Tool calling:
- Structured output:
- Context window and metadata source:
- Other relevant limitations:

Do not infer capability solely from marketing text. Include a reproducible,
non-secret verification or authoritative technical documentation where relevant.

## Credential mechanism

Name the environment-variable mechanism without posting a credential. Do not
include live API keys, tokens, request headers, or account data.

## Local-first behavior

Explain discovery, availability, failure, fallback, and whether existing local
operation remains usable.

## Security, Trust, and privacy impact

Describe data sent externally, new actions or permissions, and any retention
considerations. Model selection must not grant tool authority.

## Alternatives considered

See [Model Fabric](../../docs/MODEL_FABRIC.md) for the current policy-first
selection contract.

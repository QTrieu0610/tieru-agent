# Tieru Model Fabric v2

Tieru Model Fabric is the local-first routing layer shipped across M11 and M12.
M11 answers **how should this turn execute?** with QUICK, STANDARD, AGENT, or
DEEP. M12 then answers **which explicitly configured, eligible model should
execute it?** These are separate decisions and remain sticky for the turn.

```text
request -> TaskAnalyzer -> TaskProfile -> execution mode
        -> candidate registry -> hard policy filters -> deterministic scoring
        -> ModelSelection -> RouteDecision -> ModelRouter -> Tieru loop
```

The governing rule is **policy before intelligence**. Availability, explicit
privacy, role compatibility, required capabilities, and context/output limits
are hard filters. A high score can never bring an excluded candidate back.
Trust remains the sole authorization boundary for every actual tool action.

## v1 and v2

M11's deterministic analyzer, immutable execution profiles, optional bounded
classifier signal, and QUICK/STANDARD/AGENT/DEEP policies are unchanged. M12
extends `RouteDecision` with a structured `ModelSelection`: selected alias,
provider/model/role, total score, component breakdown, all candidate outcomes,
reasons, fallback chain, initial alias, and fallback count.

If no `fabric.models` registry is supplied, Tieru derives compatibility
candidates from the already active `main` and `small` roles. This preserves M11
behavior; it does not opt a new cloud provider in. Fabric itself remains
disabled by default on upgrade.

## Candidate registry

Candidates are configuration, not a hard-coded market ranking. Each has an
alias, provider/model/protocol, compatible roles, local/cloud flag, enabled
state, verified capability metadata, optional context/output limits, relative
API-cost and latency tiers, privacy class, and preference from 0 to 1. Unknown
capabilities stay unknown and fail conservatively when required.

```yaml
fabric:
  enabled: true
  routing_policy: local_first
  availability_ttl_seconds: 60
  min_history_samples: 5
  max_fallbacks: 1
  weights:
    capability: 0.35
    preference: 0.20
    performance: 0.20
    latency: 0.15
    cost: 0.10
  models:
    local-main:
      provider: ollama
      model: gemma4:e2b
      local: true
      capabilities: {text: true, tool_calling: true, long_context: true}
      cost_tier: free
      latency_tier: medium
      preference: 1.0
```

`free` means no provider API charge; it does not claim hardware or electricity
has zero cost. Tieru never scrapes prices or assumes a cloud model is newest,
best, cheapest, or a particular context size. Users provide relative metadata.
Configuration validation performs no network requests and rejects invalid
aliases, providers, policies, ranges, roles, limits, tiers, and capabilities.

## Availability

Availability distinguishes configured, credential, endpoint, and model state.
Checks are lazy, bounded to a short timeout, and cached in memory with a TTL.
Cloud candidates are locally checked for credentials and otherwise defer health
to the real request; Tieru does not issue probe completions. Ollama uses its
bounded installed-model catalog when needed. A missing tag is reported as
`model_not_installed`; Tieru never runs `ollama pull` or downloads weights.

Missing optional cloud keys produce `unavailable_credentials`, not a startup
failure. Availability records contain no key values. `tieru fabric refresh`
explicitly refreshes the cache.

## Privacy routing

- `local_only` excludes every cloud candidate, including fallback.
- `local_first` selects among suitable local candidates before considering an
  explicitly enabled cloud candidate.
- `balanced` scores local and cloud survivors together.
- `quality_first` changes soft weighting only; hard privacy still wins.

An explicit local-only/offline request and deterministic credential/secret
signals force local routing. Tieru does not ask an unrestricted LLM to classify
privacy. A per-turn `--local`, preferred alias, or forced execution mode can
narrow routing, but cannot override availability, capability, privacy, or Trust.
An impossible preference returns candidate-specific exclusions.

Cloud participation requires an explicit candidate entry. Merely setting an API
key never grants Fabric permission to send tasks to that provider.

## Capability filtering

Tieru routes only capabilities its current text/tool loop uses: `text`,
`tool_calling`, `structured_output`, and `long_context`. QUICK and STANDARD
require verified text support. AGENT (or any tool-requiring task) also requires
verified tool calling. DEEP tasks requiring deep context need `long_context: true`
or a configured context limit sufficient for the execution profile. A model's
ability to emit tool calls is not permission to execute them; Trust evaluates
every action separately.

## Scoring and history

Eligible candidates receive deterministic normalized components for capability
fit, user preference, Replay-backed performance, observed/configured latency,
relative API-cost tier, and optional local preference. The weighted breakdown
sums exactly to the total and stable alias ordering breaks ties.

`ModelPerformanceService` reads safe Replay metadata: provider, model, mode,
task type, completed/failed status, latency, and tool outcomes. It never needs
prompt content or duplicates run bodies. Below `min_history_samples` history is
neutral. Above it, a simple success prior, bounded latency normalization, and
optional tool completion ratio contribute. One result cannot dominate, no
routing model is trained, and history never rewrites configuration, aliases,
privacy, providers, or Trust.

## Selection, sticky routing, and fallback

Selection occurs once before the agent loop. The chosen model handles all main
loop iterations for that turn. `ModelRouter.client_for(candidate)` remains the
only adapter/client construction path and caches by non-secret connection
identity; Fabric never mutates global `Settings`.

Fallback is separate from selection and is limited to connection/endpoint
failure, timeout, rate limit, model-not-found, or runtime discovery of unsupported
tool calling. It is finite (`max_fallbacks`, default 1), does not retry a failed
candidate, and re-runs the same privacy/availability/capability filters. It never
switches merely because an answer appears weak.

Tieru restarts on a fallback only before tool activity and only for non-streamed
turns. After any tool activity or streamed output it stops honestly instead of
risking duplicate writes, messages, pushes, or other actions.

## Local-only and optional cloud operation

The Ollama/Gemma profile remains a complete deployment with no API keys and no
internet. Multiple installed Ollama tags can be configured as ordinary
candidates with different verified capabilities and preferences. Cloud
providers are optional accelerators; missing keys do not affect unrelated local
work, and no provider is purchased, configured, installed, or enabled
automatically.

## Replay, CLI, dashboard, and explainability

Replay records bounded, redacted `fabric_candidates`, `fabric_filter`,
`fabric_score`, `fabric_selection`, and `fabric_fallback` events in addition to
M11 events. It preserves initial/final provider/model aliases and fallback count
without credentials, prompts, or hidden reasoning. Pre-M12 runs retain valid M11
metrics; missing candidate details are never fabricated. Shadow workflow
signatures remain structural and Forge provenance is unchanged.

```bash
tieru fabric status
tieru fabric modes
tieru fabric models
tieru fabric models --available
tieru fabric refresh
tieru fabric stats
tieru fabric explain "edit this repository" --local
tieru fabric score "edit this repository" --model local-main --mode agent
```

Explain and score analyze only; they never execute the supplied task. The
no-build dashboard shows candidate models, routing policy/weights, recent
selections and fallback, and bounded performance aggregates with insufficient
sample sizes labeled.

## Failure isolation and limitations

Unexpected non-policy scoring failures use the existing M11 role behavior only
where doing so cannot violate `local_only`; an empty eligible set fails honestly.
Replay, verification, Shadow, and performance aggregation are advisory and
cannot change the completed answer or authorization policy.

M12 does not provide live price catalogs, automatic model downloads, automatic
cloud opt-in, subjective mid-turn escalation, multimodal routing, provider
purchasing, reinforcement learning, fine-tuning, self-modifying Trust, or Tieru
Capsule.

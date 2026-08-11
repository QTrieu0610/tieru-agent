# Tieru Trust Kernel

Tieru Trust Kernel is the authorization boundary between a model requesting an
action and Tieru executing it. The model can propose a tool call; it cannot grant
itself permission. Classification, policy, approval, and the final decision run
in deterministic application code outside the prompt.

## Why it exists

Tieru already had a deny-by-default registry, declared tool capabilities,
action-specific policies, redaction, and CLI/dashboard approval. M7 preserves
those controls and gives them one structured model that can answer:

- What operation was requested, and against which safe target?
- Which canonical capabilities are involved?
- What deterministic risk does the action carry?
- Which policy matched, and was its scope satisfied?
- Was exact, single-use approval required and granted?
- Why was the final result allowed or denied?

Every model-invoked built-in, optional browser action, experimental tool, and MCP
tool crosses `ToolRegistry` and then `TrustKernel.authorize()` immediately before
its function can run. A denied function is never called.

## ActionRequest

`ActionRequest` is a normalized, secret-safe description of an attempted action.
It includes the tool, canonical capabilities, operation, target, scope, resource
type, local/network/external/process/browser/destructive flags, reversibility,
read-only state, and bounded metadata.

Tool declarations may retain compatible capability names such as
`filesystem.write`, `network.read`, or `memory.write`. The registry maps them to
the canonical set:

- `local_read`
- `local_write`
- `network_read`
- `external_write`
- `process_execution`
- `browser_automation`
- `destructive`

An unknown legacy capability makes the complete action unclassified. It is
denied rather than partially authorizing the known portion.

## Risk classification

`RiskClassifier` uses capability, operation, target/scope, destructiveness,
external effects, reversibility, and locally declared risk. It never calls a
model.

| Level | Initial meaning |
|---|---|
| LOW | Bounded local or public-network read; read-only browser extraction |
| MEDIUM | Local persistent write or a tool with a medium declared floor |
| HIGH | External write, process execution, browser side effect, ordinary destructive action, or irreversible local mutation |
| CRITICAL | Credential handling, unclassified action, broad destructive scope, or a fail-closed classification/policy failure |

Declared tool risk is a floor, not the only signal. A broad/default allow cannot
authorize CRITICAL risk; it needs an explicit tool or capability allow. Existing
tool-specific safety code remains an independent second boundary.

## TrustDecision and explanation

Authorization returns `TrustDecision`, not a boolean. It records:

- `allowed`
- `risk`
- `approval_required`
- deterministic `reason_codes`
- `explanation`
- `matched_policy`
- `action_fingerprint`

`TrustKernel.explain()` returns the already-computed deterministic explanation,
for example `DENIED — HIGH risk: external write, approval unavailable`. It does
not ask a model to explain security policy.

## Policy and precedence

The exact precedence is:

1. explicit deny;
2. matching scoped explicit allow;
3. exact human approval;
4. capability/default or compatible tool-declared policy;
5. deny by default.

A deny found at any explicit tool or capability level wins. A scoped rule whose
target does not match denies rather than falling through to a broader allow.
Supported readable scopes are paths, hosts, repositories, commands, recipients,
and exact targets.

Paths are expanded and resolved before comparison. A target is accepted only
when equal to or below an allowed root, so alternate relative spellings and
`../` traversal cannot bypass a root. Resolution also follows existing symlink
parents where the platform exposes them. Host matching uses parsed hostnames and
allows exact domains or their subdomains. Command scope compares only the
declared executable token; Trust Kernel deliberately does not parse a shell.

Example:

```yaml
trust:
  default: deny
  capabilities:
    local_read: allow
    local_write: confirm
    network_read: allow
    external_write: confirm
    process_execution:
      mode: allow
      commands: [git]
    destructive: deny
```

Path scopes can be attached to a capability when every action in that class has
a filesystem target, or more narrowly to a tool rule such as
`tools.workspace_write: {mode: allow, paths: [workspace]}`.

This syntax is optional. Existing `tool_permissions.defaults`, `.capabilities`,
and `.tools` remain supported. Both policy sources are evaluated together, and
an old deny cannot be normalized into an allow.

## Approval

`confirm` creates a redacted `ApprovalRequest` describing the operation, safe
target, risk, capabilities, and deterministic reason. CLI and dashboard handlers
offer `Allow once` or `Deny`. The dashboard binds approval to its authenticated
session, tool, expiry, and exact fingerprint; requests are single-use.

An approval never modifies configuration or creates a permanent trust rule.
Approving the same action again requires a new approval. A denied fingerprint is
remembered within the registry session so an identical retry is denied without
approval spam. Missing, disconnected, expired, declined, malformed, or failing
approval handlers deny.

## Action fingerprints and secrets

The fingerprint is a stable SHA-256 digest of only the tool, canonical
capabilities, operation, normalized safe target/scope, and resource type. It does
not include arbitrary arguments. Credential-shaped targets are replaced before
hashing, so different raw tokens identify the same safe action rather than
embedding secret-dependent material.

Approval arguments still use Tieru's existing key-aware and content-aware
redaction. Trust decisions, denial results, observer events, dashboard rows, and
explanations never contain full action arguments or secrets.

## MCP

MCP server descriptions are untrusted capability descriptions, not permission.
An MCP tool is unclassified and denied unless the user gives that exact tool a
local `tool_policies` entry in `mcp.json` with capabilities and related metadata.
Classified MCP calls then pass through the same Trust Kernel as built-ins. Merely
being supplied by a configured server never grants execution permission.

Example server fragment:

```json
{
  "name": "docs",
  "command": "docs-mcp",
  "tool_policies": {
    "search": {
      "capabilities": ["network_read"],
      "read_only": true,
      "risk": "low",
      "policy": "allow",
      "operation": "search"
    }
  }
}
```

MCP configuration is discovery, not process permission. Before a configured
stdio command starts, Tieru submits `start_mcp_server` with the
`process_execution` capability to the Trust Kernel. Explicit deny wins; without
an allow policy or available approval handler, no process starts. Command
arguments and environment values are not copied into Trust events. Model-facing
MCP results are secret-redacted and byte-bounded before entering context.

## Browser

Browser operations are classified separately as navigation, extraction, click,
fill, screenshot, and close. Navigation/extraction remain read-oriented; click
and fill are state-changing and require their configured policy/approval.
Uploads, downloads, arbitrary JavaScript, and silent credential reuse remain
unavailable.

The Trust Kernel checks navigation against `browser_allowed_domains` before the
tool runs. `RestrictedBrowser` independently retains URL scheme, hostname,
private-address, redirect, timeout, action-count, isolated-context, and local
fixture protections. M7 adds authorization; it does not weaken the sandbox.

## Filesystem and process safety

Scoped filesystem policy uses resolved paths, but M7 is not an OS sandbox.
Tool-owned roots, safe filenames, and workspace restrictions still apply inside
individual tools. Model-accessible process execution is HIGH risk and denied or
confirmed unless explicitly scoped. Command rules compare a known executable;
arguments and working directories remain bounded by the tool. Tieru does not
attempt to make arbitrary shell strings safe.

## Memory and Memory Graph

Action-specific `manage_memory` metadata maps searches, listing, reads, and graph
inspection to `local_read`; fact updates, explicit relation saves, relation
archives, and exports to write capabilities. Delete operations are additionally
classified destructive. This preserves M6's explicit-write philosophy and the
existing memory approval settings without redesigning Memory Graph.

## Trust events and dashboard

The existing observer/tracer receives:

- `trust_request`
- `trust_approval`
- `trust_decision`

Events contain safe tool/capability/operation/risk/decision/reason/fingerprint
metadata, never raw arguments. The dashboard Trust tab shows current policy,
canonical capabilities, baseline risks, scopes, and a bounded list of recent
safe decisions. This is operational authorization inspection only. It does not
implement Replay navigation, historical reconstruction, or execution forking.

## Fail-closed behavior

Unknown tools, missing classifications, invalid targets for scoped rules,
invalid policy modes, classifier exceptions, policy exceptions, approval
exceptions, and unavailable interactive approval all deny. Trust authorization
does not inherit the fail-open posture used by optional convenience systems such
as retrieval or graph-workflow triage.

## Limitations and non-goals

Trust Kernel is an application authorization layer, not a VM, container, OS
sandbox, antivirus, credential broker, shell-security parser, or autonomous
policy generator. It does not infer permanent trust, and it cannot make a
misbehaving dependency harmless after an explicitly authorized call. Narrow
tool implementation, independent browser/path/process restrictions, and normal
operating-system security remain necessary defense layers.

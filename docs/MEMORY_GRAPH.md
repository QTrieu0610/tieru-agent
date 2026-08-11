# Tieru Memory Graph

Tieru Memory Graph is the structured fourth capability of Tieru Memory. It adds
typed entities and relationships without replacing semantic facts, episodic
records, procedural skills, or bounded working context. The complete graph works
offline, needs no API key, and uses the existing `.tieru/state.db` as its only
authoritative store.

## Purpose and boundaries

Graph memory represents compact personal knowledge whose relationships matter:
preferences, current defaults, projects, technologies, decisions, and their
change over time. It is intentionally not a general-purpose graph database, an
LLM reasoning engine, a vector store, or an automatic extractor for every chat
sentence. Existing text memories are not backfilled.

Graph writes occur only through explicit graph-aware operations or another
approved memory flow. The `manage_memory` tool supports `remember_relation`,
`inspect_relation`, and `archive_relation`; writes and archives use the existing
confirmation policy, while inspection is read-only.

## SQLite architecture

Two additive tables live beside Tieru's existing memory tables:

- `graph_entities` stores type, canonical and normalized names, small JSON
  metadata, and timestamps. `(entity_type, normalized_name)` is unique.
- `graph_relations` stores subject and object entity IDs, a normalized predicate,
  confidence, importance, provenance, validity, lifecycle state, supersession
  linkage, and timestamps.

Indexes cover normalized entity name, entity type, relation subject, object,
predicate, and status. Schema creation uses idempotent `CREATE ... IF NOT EXISTS`
statements. No migration deletes, rewrites, or automatically converts existing
memory.

All graph SQL is contained by `GraphStore`; `GraphService` is the names-in,
typed-records-out facade exposed as `Memory.graph`.

## Entity model

An entity has:

- stable integer `id`;
- open-ended, normalized `entity_type`;
- human-readable `canonical_name`;
- case-folded, punctuation-normalized `normalized_name`;
- optional `metadata_json` object;
- `created_at` and `updated_at` timestamps.

Types such as `person`, `project`, `organization`, `technology`, `model`,
`preference`, and `decision` are conventions rather than a closed ontology.
Custom machine-readable types remain valid. Upsert treats the normalized name
and type as identity, preventing duplicate spelling/case variants.

## Relation model

A relation is a directed edge:

```text
subject --PREDICATE--> object
```

Predicates are upper-case machine-readable identifiers but are not restricted to
a fixed vocabulary. Each relation records `confidence` and `importance` in the
inclusive range `0.0` to `1.0`. M6 uses simple documented defaults rather than
LLM-estimated precision: explicit user saves default to `0.9`, semantic memory
and system sources to `0.8`, episodic memory and imports to `0.7`, and
conversation or unknown sources to `0.6`. Importance defaults to `0.5`.

Lifecycle status is one of `active`, `superseded`, `contradicted`, or `archived`.
Archiving preserves the row and closes an open validity interval. Explicit
contradiction support marks a relationship without attempting unrestricted
natural-language contradiction reasoning.

## Provenance

Every relation has `source_type` and `source_ref`. A source may be
`explicit_user_save`, `semantic_memory`, `episodic_memory`, `conversation`,
`import`, or `system`; custom machine-readable source types are also possible.
A reference such as `semantic:42` points to an originating record without
copying its full private content into the graph row.

`explain_relation()` returns the relation, its subject and object, its provenance,
and the newer relation when superseded. This makes origin, creation time,
confidence, current status, and replacement inspectable.

## Temporal behavior

`valid_from` and `valid_to` accept ISO 8601 dates or timestamps. Both are
optional. When both exist, they are parsed and compared chronologically,
including timezone offsets. Current-only retrieval includes an active relation
only when its start is not in the future and its end is still open or in the
future. Historical rows remain available for inspection.

## Supersession policy

`GraphPolicy` separates single-current-value predicates from multi-value
predicates. The default single-value set is `DEFAULT_MODEL`,
`USES_DEFAULT_MODEL`, `CURRENT_ROLE`, and `PRIMARY_PROJECT`.

Adding a different active object for the same subject and a configured
single-value predicate marks each older active relation `superseded`, links its
`superseded_by` to the new row, and closes an empty `valid_to`. Predicates such as
`USES_TECHNOLOGY`, `KNOWS`, `LIKES`, and `WORKS_ON` are multi-value by default and
do not supersede one another. The policy is explicit and extensible; M6 does not
guess contradictions from prose.

## Retrieval and context bounds

Graph retrieval is deterministic. It normalizes the user message, finds exact
whole-name entity mentions, and traverses their current neighborhood before the
existing semantic/episodic retrieval path. It does not call an LLM for matching
or traversal.

Prompt context defaults to at most 4 entities, 8 relations, and depth 1. M6 caps
callers at 20 entities, 50 relations, and depth 2. The whole graph is never
injected. If no complete graph edge matches, the existing retrieval gate and
text-memory search continue unchanged.

## Inspection and export

Generated `MEMORY.md` retains the existing fact and episode sections and appends
`## Memory Graph`. Each edge shows its status, confidence, importance, source,
and optional validity. The local dashboard Memory tab provides lightweight
entity and relation tables with types, predicates, scores, status, and source.
These are views; SQLite remains authoritative.

## Privacy

Entity names, entity metadata, and provenance references pass through Tieru's
existing credential-shape detector. API keys, tokens, passwords, private keys,
and similar secret material are refused. Exported content is redacted again at
the display boundary. Graph writes do not occur from ordinary chat logging, and
graph contents are not emitted into unrelated traces or sent to a hosted model
automatically. As with other memory context, selected graph context reaches the
configured main model only when used to answer a matching request; users who
require a fully local path should configure local model roles.

## Example

```python
relation, created = memory.graph.remember_relation(
    subject="Tieru",
    subject_type="project",
    predicate="USES_DEFAULT_MODEL",
    object="gemma4:e2b",
    object_type="model",
    source_type="explicit_user_save",
    source_ref="tool:manage_memory",
)

details = memory.graph.inspect_relation(relation.id)
context = memory.graph.retrieve_context("What model does Tieru use locally?")
```

The operation creates or reuses both typed entities, stores one inspectable
relation, and supersedes an older active default-model edge if present.

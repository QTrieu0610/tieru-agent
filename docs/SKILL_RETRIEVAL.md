# Hybrid Skill Retrieval

M20 selects potentially relevant procedural skills with a deterministic, explainable pipeline:

```text
normalize query → explicit/canonical reference → exact alias → BM25-like lexical score
                → optional semantic score → weighted fusion → threshold → top-k
                → Context Firewall authority
```

Skill retrieval relevance does not imply authorization.

The Trust Kernel still authorizes every actual action. Model Fabric still selects models, and the
Context Firewall still decides whether retrieved content is REVIEWED instruction material or DATA.

## Metadata and normalization

Existing `name` and `description` frontmatter remains valid. Skills may additionally declare
bounded YAML lists named `aliases`, `keywords`, and `domains`. Metadata is limited to 8 KiB of
frontmatter, 1 KiB descriptions, 16 items per list, and 128 bytes per item. Malformed optional
lists are ignored without dropping an otherwise valid legacy skill.

Queries and metadata use Unicode NFKC normalization, case-folding, whitespace normalization, and
Unicode-aware tokenization. Tieru does not apply English stemming to Vietnamese text and does not
use a domain translation table as its primary cross-language mechanism.

## Alias and lexical scoring

An exact canonical name or explicit `... skill` reference ranks first. A bounded alias phrase has
a deterministic near-maximum score and is not diluted by semantic fusion.

Other candidates use an in-memory BM25-like term score with centralized field weights:
canonical name (4.0), alias (3.0), keyword (2.0), domain (1.1), and description (1.0). Only
retrieval metadata is indexed; instruction bodies are excluded to avoid noisy or adversarial
matching. Terms absent from the corpus do not penalize longer natural-language requests.

## Optional semantic retrieval and hybrid fusion

Semantic retrieval is optional; Tieru remains functional offline with lexical retrieval.

No provider SDK is instantiated by `SkillLoader`. When Tieru is explicitly configured with its
existing Supabase/OpenAI semantic-memory backend, that production embedding boundary may also
embed bounded, redacted skill retrieval metadata and the query. Default SQLite/local operation
does not make network calls. Tests inject a deterministic fake embedding backend.

Validated cosine similarity rejects empty, zero, non-finite, oversized, or dimension-incompatible
vectors. For non-exact candidates with a valid semantic score, fusion is:

```text
final = 0.60 × lexical + 0.40 × semantic
```

If semantic retrieval fails for any reason, the candidate or request falls back to its lexical
score. Tieru does not use an LLM router as the primary skill-selection mechanism.

## Threshold, top-k, and explanations

The default minimum final score is 0.30. Below-threshold queries return no skill rather than
forcing a candidate. The default top-k is 2 and the hard maximum is 4. Each `SkillMatch` exposes
the final, lexical, and optional semantic scores, any matched alias, review status, authority, and
one reason: `explicit_reference`, `exact_name`, `exact_alias`, `lexical`, `semantic`, or `hybrid`.

## Embedding cache and live index lifecycle

The `skill_embeddings` SQLite table stores only JSON vectors for bounded retrieval metadata. Its
identity combines a stable skill ID, SHA-256 metadata content hash, and configured embedding model.
Changing metadata or models recomputes the vector; unchanged skills reuse it. Live file signatures
include path, nanosecond modification time, and size, so additions, edits, and deletions are visible
without restarting. Stale cache entries are pruned and instruction bodies/user queries are not
stored in the embedding cache.

## Reviewed and unreviewed authority

Unreviewed skills do not become trusted instructions because they score highly.

Packaged skills and explicitly approved installed Forge artifacts retain REVIEWED authority.
Arbitrary home/generated skills remain DATA. Explicitly naming an unreviewed skill changes its
relevance score, not its review status. Session assembly always passes retrieved matches through
the M19 Context Builder and never concatenates arbitrary skill text directly into CONTROL.

## Deterministic evaluation

`evals/fixtures/skill_retrieval_cases.json` covers exact, alias, lexical, synonym,
cross-language, multiple-skill, and irrelevant cases. Run:

```text
python -m tieru.memory.procedural.eval
```

It reports Recall@1 and Recall@2 for the legacy overlap baseline, M20 lexical-only, and hybrid
retrieval, plus hybrid no-match accuracy. The fixture backend is model-free and makes no network
calls.

## Limitations

The implementation performs O(N) scoring, appropriate for tens or hundreds of local skills rather
than large public catalogs. Semantic quality depends on the explicitly configured embedding model;
it does not guarantee universal multilingual retrieval or zero false positives. Metadata authors
still need useful descriptions, aliases, and keywords, while thresholds may require future tuning
for substantially different corpora.

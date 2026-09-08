"""Deterministic lexical, alias, operation-aware, and optional semantic capability retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

from tieru.capabilities.models import Capability
from tieru.memory.personal import redact_secrets

FIELD_WEIGHTS = {
    "name": 4.0,
    "aliases": 3.0,
    "keywords": 2.0,
    "domains": 1.1,
    "description": 1.0,
}

MAX_EMBEDDING_DIMENSIONS = 16_384
MAX_EMBEDDING_TEXT_BYTES = 4096

_DESTRUCTIVE_KEYWORDS = frozenset({
    "delete", "remove", "cancel", "drop", "destroy", "clear", "wipe", "forget", "purge",
})

_STOPWORDS = frozenset({
    "and", "the", "a", "an", "or", "to", "in", "of", "for", "with", "on", "at",
    "by", "from", "is", "it", "as", "be", "this", "that", "are", "was", "were",
    "will", "would", "should", "could", "can", "do", "does", "did", "have", "has",
    "had", "how", "what", "which", "who", "whom", "why", "where", "when", "about",
})

_OPERATION_KEYWORDS: dict[str, frozenset[str]] = {
    "read": frozenset({
        "what", "show", "list", "get", "inspect", "check", "find", "status",
        "diff", "symbols", "view", "read", "describe", "explain", "search",
        "fetch", "tell", "display", "see", "look", "review",
    }),
    "create": frozenset({
        "create", "add", "make", "schedule", "new", "write", "post", "send",
        "draft", "compose", "insert",
    }),
    "update": frozenset({
        "update", "modify", "edit", "change", "patch", "correct", "revise",
    }),
    "delete": _DESTRUCTIVE_KEYWORDS,
    "execute": frozenset({
        "run", "execute", "test", "tests", "build", "compile", "eval", "delegate",
        "start", "launch", "benchmark", "pytest", "bash",
    }),
}


def normalize_text(value: object) -> str:
    """Normalize text without English stemming or accent destruction."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def tokenize(value: object) -> tuple[str, ...]:
    return tuple(
        token
        for token in normalize_text(value).split()
        if len(token) >= 2 or token.isdigit()
    )


def classify_operations(query: str) -> set[str]:
    """Extract intended operations from the user request or task instruction."""
    tokens = set(tokenize(query))
    ops: set[str] = set()
    for op, keywords in _OPERATION_KEYWORDS.items():
        if tokens & keywords:
            ops.add(op)
    if not ops:
        # Default conservatively to read
        ops.add("read")
    return ops


def has_destructive_intent(query: str) -> bool:
    """Check if the query specifically requests a destructive operation."""
    tokens = set(tokenize(query))
    return bool(tokens & _DESTRUCTIVE_KEYWORDS)


def canonical_capability_text(cap: Capability) -> str:
    lines = [
        f"Capability: {cap.name}",
        f"ID: {cap.capability_id}",
        f"Aliases: {', '.join(cap.aliases)}",
        f"Description: {cap.description}",
        f"Keywords: {', '.join(cap.keywords)}",
        f"Domains: {', '.join(cap.domains)}",
        f"Tools: {', '.join(cap.tool_names)}",
    ]
    safe = redact_secrets("\n".join(lines))
    encoded = safe.encode("utf-8")
    if len(encoded) <= MAX_EMBEDDING_TEXT_BYTES:
        return safe
    return encoded[:MAX_EMBEDDING_TEXT_BYTES].decode("utf-8", errors="ignore")


def metadata_hash(cap: Capability) -> str:
    return hashlib.sha256(canonical_capability_text(cap).encode("utf-8")).hexdigest()


class EmbeddingBackend(Protocol):
    model: str

    def embed(self, text: str) -> Sequence[float]: ...


def _valid_vector(value: object) -> tuple[float, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if not 1 <= len(value) <= MAX_EMBEDDING_DIMENSIONS:
        return None
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    if math.sqrt(sum(item * item for item in vector)) == 0.0:
        return None
    return vector


def cosine_similarity(left: object, right: object) -> float | None:
    a = _valid_vector(left)
    b = _valid_vector(right)
    if a is None or b is None or len(a) != len(b):
        return None
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if denom == 0.0:
        return None
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True)) / denom))


class CapabilityEmbeddingCache:
    """Cache for capability metadata embeddings."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn
        self._memory: dict[tuple[str, str], tuple[str, tuple[float, ...]]] = {}
        if conn is not None:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS capability_embeddings (
                       capability_id TEXT NOT NULL,
                       content_hash TEXT NOT NULL,
                       model TEXT NOT NULL,
                       vector_json TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       PRIMARY KEY(capability_id, model)
                   )"""
            )
            conn.commit()

    def get(self, capability_id: str, content_hash: str, model: str) -> tuple[float, ...] | None:
        if self.conn is None:
            cached = self._memory.get((capability_id, model))
            return cached[1] if cached and cached[0] == content_hash else None
        row = self.conn.execute(
            "SELECT content_hash, vector_json FROM capability_embeddings WHERE capability_id=? AND model=?",
            (capability_id, model),
        ).fetchone()
        if row is None or str(row[0]) != content_hash:
            return None
        try:
            return _valid_vector(json.loads(str(row[1])))
        except (TypeError, ValueError):
            return None

    def put(self, capability_id: str, content_hash: str, model: str, vector: object) -> bool:
        valid = _valid_vector(vector)
        if valid is None:
            return False
        if self.conn is None:
            self._memory[(capability_id, model)] = (content_hash, valid)
            return True
        self.conn.execute(
            """INSERT OR REPLACE INTO capability_embeddings
               (capability_id, content_hash, model, vector_json, created_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (capability_id, content_hash, model, json.dumps(list(valid))),
        )
        self.conn.commit()
        return True


def _fields(cap: Capability) -> dict[str, tuple[str, ...]]:
    return {
        "name": tokenize(cap.name),
        "aliases": tuple(token for a in cap.aliases for token in tokenize(a)),
        "keywords": tuple(token for k in cap.keywords for token in tokenize(k)),
        "domains": tuple(token for d in cap.domains for token in tokenize(d)),
        "description": tokenize(cap.description),
    }


def compute_lexical_scores(
    capabilities: Sequence[Capability],
    query_tokens: tuple[str, ...],
    bm25_k1: float = 1.2,
) -> dict[str, float]:
    """BM25-like lexical scoring for capabilities."""
    if not capabilities or not query_tokens:
        return {c.capability_id: 0.0 for c in capabilities}

    fields = {c.capability_id: _fields(c) for c in capabilities}
    documents = {
        cap_id: {token for tokens in val.values() for token in tokens}
        for cap_id, val in fields.items()
    }
    vocabulary = set().union(*documents.values()) if documents else set()
    query_terms = tuple(
        term for term in dict.fromkeys(query_tokens)
        if term in vocabulary and term not in _STOPWORDS
    )
    if not query_terms:
        return {c.capability_id: 0.0 for c in capabilities}

    scores: dict[str, float] = {}
    for c in capabilities:
        cap_id = c.capability_id
        matching_terms = [t for t in query_terms if t in documents.get(cap_id, set())]
        if not matching_terms:
            scores[cap_id] = 0.0
            continue

        raw_score = 0.0
        for term in matching_terms:
            strongest = 0.0
            for field_name, tokens in fields[cap_id].items():
                tf = Counter(tokens).get(term, 0)
                if tf <= 0:
                    continue
                saturated = (tf * (bm25_k1 + 1.0)) / (tf + bm25_k1)
                weighted = saturated * FIELD_WEIGHTS.get(field_name, 1.0)
                strongest = max(strongest, weighted)
            raw_score += strongest / FIELD_WEIGHTS["name"]

        # Score with diminishing returns: keyword match gives ~0.45; additional matches increase score
        normalized = (raw_score / math.sqrt(len(matching_terms))) * 0.70 + 0.10 * min(3, len(matching_terms))
        scores[cap_id] = round(max(0.0, min(1.0, normalized)), 4)

    return scores

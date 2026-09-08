"""Deterministic alias, BM25-like lexical, and optional semantic skill retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from tieru.memory.personal import redact_secrets

if TYPE_CHECKING:
    from tieru.memory.procedural.loader import Skill

MAX_TOP_K = 4
MAX_EMBEDDING_DIMENSIONS = 16_384
MAX_EMBEDDING_TEXT_BYTES = 4096
FIELD_WEIGHTS = {
    "name": 4.0,
    "aliases": 3.0,
    "keywords": 2.0,
    "domains": 1.1,
    "description": 1.0,
}


@dataclass(frozen=True)
class SkillRetrievalConfig:
    top_k: int = 2
    min_score: float = 0.30
    lexical_weight: float = 0.60
    semantic_weight: float = 0.40
    semantic_enabled: bool = True
    bm25_k1: float = 1.2

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= MAX_TOP_K:
            raise ValueError(f"skill top_k must be between 1 and {MAX_TOP_K}")
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError("skill minimum score must be between 0 and 1")
        if self.lexical_weight < 0 or self.semantic_weight < 0:
            raise ValueError("skill retrieval weights cannot be negative")
        if not math.isclose(self.lexical_weight + self.semantic_weight, 1.0):
            raise ValueError("skill retrieval weights must sum to 1")
        if self.bm25_k1 <= 0:
            raise ValueError("skill BM25 saturation must be positive")


class EmbeddingBackend(Protocol):
    """Small injection boundary; chat/completion clients are deliberately absent."""

    model: str

    def embed(self, text: str) -> Sequence[float]: ...


@dataclass(frozen=True)
class SkillMatch:
    skill: Skill
    final_score: float
    lexical_score: float
    semantic_score: float | None
    matched_alias: str | None
    retrieval_reason: str

    @property
    def review_status(self) -> str:
        return "reviewed" if self.skill.reviewed else "unreviewed"

    @property
    def authority(self):
        from tieru.context import ContextTrust

        return ContextTrust.REVIEWED if self.skill.reviewed else ContextTrust.DATA


def normalize_text(value: object) -> str:
    """Normalize without English stemming or accent destruction."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def tokenize(value: object) -> tuple[str, ...]:
    return tuple(
        token
        for token in normalize_text(value).split()
        if len(token) >= 2 or token.isdigit()
    )


def canonical_embedding_text(skill: Skill) -> str:
    lines = [
        f"Skill: {skill.name}",
        f"Aliases: {', '.join(skill.aliases)}",
        f"Description: {skill.description}",
        f"Keywords: {', '.join(skill.keywords)}",
        f"Domains: {', '.join(skill.domains)}",
    ]
    safe = redact_secrets("\n".join(lines))
    encoded = safe.encode("utf-8")
    if len(encoded) <= MAX_EMBEDDING_TEXT_BYTES:
        return safe
    return encoded[:MAX_EMBEDDING_TEXT_BYTES].decode("utf-8", errors="ignore")


def metadata_hash(skill: Skill) -> str:
    return hashlib.sha256(canonical_embedding_text(skill).encode("utf-8")).hexdigest()


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
    denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if denominator == 0.0:
        return None
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True)) / denominator))


class SkillEmbeddingCache:
    """SQLite-backed when available; bounded metadata vectors only."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn
        self._memory: dict[tuple[str, str], tuple[str, tuple[float, ...]]] = {}
        if conn is not None:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_embeddings (
                       skill_id TEXT NOT NULL,
                       content_hash TEXT NOT NULL,
                       model TEXT NOT NULL,
                       vector_json TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       PRIMARY KEY(skill_id, model)
                   )"""
            )
            conn.commit()

    def get(self, skill_id: str, content_hash: str, model: str) -> tuple[float, ...] | None:
        if self.conn is None:
            cached = self._memory.get((skill_id, model))
            return cached[1] if cached and cached[0] == content_hash else None
        row = self.conn.execute(
            "SELECT content_hash, vector_json FROM skill_embeddings WHERE skill_id=? AND model=?",
            (skill_id, model),
        ).fetchone()
        if row is None or str(row[0]) != content_hash:
            return None
        try:
            return _valid_vector(json.loads(str(row[1])))
        except (TypeError, ValueError):
            return None

    def put(self, skill_id: str, content_hash: str, model: str, vector: object) -> bool:
        valid = _valid_vector(vector)
        if valid is None:
            return False
        if self.conn is None:
            self._memory[(skill_id, model)] = (content_hash, valid)
            return True
        self.conn.execute(
            """INSERT INTO skill_embeddings
                   (skill_id, content_hash, model, vector_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(skill_id, model) DO UPDATE SET
                   content_hash=excluded.content_hash,
                   vector_json=excluded.vector_json,
                   created_at=excluded.created_at""",
            (
                skill_id,
                content_hash,
                model,
                json.dumps(valid, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()
        return True

    def prune(self, active_skill_ids: set[str]) -> None:
        for key in list(self._memory):
            if key[0] not in active_skill_ids:
                self._memory.pop(key, None)
        if self.conn is None:
            return
        rows = self.conn.execute("SELECT DISTINCT skill_id FROM skill_embeddings").fetchall()
        stale = [str(row[0]) for row in rows if str(row[0]) not in active_skill_ids]
        if stale:
            self.conn.executemany(
                "DELETE FROM skill_embeddings WHERE skill_id=?", ((item,) for item in stale)
            )
            self.conn.commit()


def _phrase_present(phrase: str, query: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {query} "


class SkillRetriever:
    def __init__(
        self,
        skills: Callable[[], Sequence[Skill]],
        *,
        config: SkillRetrievalConfig | None = None,
        embedding_backend: EmbeddingBackend | None = None,
        cache: SkillEmbeddingCache | None = None,
    ) -> None:
        self._skills = skills
        self.config = config or SkillRetrievalConfig()
        self.embedding_backend = embedding_backend
        self.cache = cache or SkillEmbeddingCache()

    @staticmethod
    def _fields(skill: Skill) -> dict[str, tuple[str, ...]]:
        return {
            "name": tokenize(skill.name),
            "aliases": tuple(token for value in skill.aliases for token in tokenize(value)),
            "keywords": tuple(token for value in skill.keywords for token in tokenize(value)),
            "domains": tuple(token for value in skill.domains for token in tokenize(value)),
            "description": tokenize(skill.description),
        }

    def _lexical_scores(
        self, skills: Sequence[Skill], query_tokens: tuple[str, ...]
    ) -> dict[str, float]:
        fields = {skill.skill_id: self._fields(skill) for skill in skills}
        documents = {
            skill_id: {token for tokens in value.values() for token in tokens}
            for skill_id, value in fields.items()
        }
        vocabulary = set().union(*documents.values()) if documents else set()
        # Terms absent from the skill corpus carry no retrieval evidence and
        # must not dilute a longer natural-language request.
        query_terms = tuple(
            term for term in dict.fromkeys(query_tokens) if term in vocabulary
        )
        size = max(1, len(skills))
        idf = {
            term: math.log(
                1.0
                + (size - sum(term in document for document in documents.values()) + 0.5)
                / (sum(term in document for document in documents.values()) + 0.5)
            )
            for term in query_terms
        }
        denominator = sum(idf.values()) * FIELD_WEIGHTS["name"]
        if denominator <= 0.0:
            return {skill.skill_id: 0.0 for skill in skills}
        output: dict[str, float] = {}
        for skill in skills:
            score = 0.0
            for term in query_terms:
                strongest = 0.0
                for field, tokens in fields[skill.skill_id].items():
                    tf = Counter(tokens).get(term, 0)
                    if tf:
                        saturated = tf * (self.config.bm25_k1 + 1.0) / (
                            tf + self.config.bm25_k1
                        )
                        strongest = max(strongest, FIELD_WEIGHTS[field] * saturated)
                score += idf[term] * min(FIELD_WEIGHTS["name"], strongest)
            output[skill.skill_id] = max(0.0, min(1.0, score / denominator))
        return output

    def _semantic_scores(
        self, skills: Sequence[Skill], query: str
    ) -> dict[str, float | None]:
        backend = self.embedding_backend
        if not self.config.semantic_enabled or backend is None:
            return {skill.skill_id: None for skill in skills}
        model = str(getattr(backend, "model", "")).strip()
        if not model:
            return {skill.skill_id: None for skill in skills}
        try:
            query_vector = _valid_vector(backend.embed(redact_secrets(query)))
        except Exception:
            query_vector = None
        if query_vector is None:
            return {skill.skill_id: None for skill in skills}
        output: dict[str, float | None] = {}
        for skill in skills:
            content_hash = metadata_hash(skill)
            vector = self.cache.get(skill.skill_id, content_hash, model)
            if vector is None:
                try:
                    candidate = backend.embed(canonical_embedding_text(skill))
                except Exception:
                    candidate = None
                if self.cache.put(skill.skill_id, content_hash, model, candidate):
                    vector = self.cache.get(skill.skill_id, content_hash, model)
            output[skill.skill_id] = cosine_similarity(query_vector, vector)
        return output

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[SkillMatch]:
        skills = tuple(self._skills())
        self.cache.prune({skill.skill_id for skill in skills})
        if not skills:
            return []
        bounded_k = min(MAX_TOP_K, max(1, int(top_k or self.config.top_k)))
        normalized_query = normalize_text(query)
        query_tokens = tokenize(query)
        lexical = self._lexical_scores(skills, query_tokens)
        semantic = self._semantic_scores(skills, query)
        matches: list[SkillMatch] = []
        for skill in skills:
            name = normalize_text(skill.name)
            matched_alias = next(
                (
                    alias
                    for alias in skill.aliases
                    if _phrase_present(normalize_text(alias), normalized_query)
                ),
                None,
            )
            if normalized_query == name:
                final, reason = 1.0, "exact_name"
            elif _phrase_present(name, normalized_query) and "skill" in query_tokens:
                final, reason = 1.0, "explicit_reference"
            elif matched_alias is not None:
                final, reason = 0.98, "exact_alias"
            else:
                lexical_score = lexical[skill.skill_id]
                semantic_score = semantic[skill.skill_id]
                if semantic_score is None:
                    final = lexical_score
                    reason = "lexical"
                else:
                    final = (
                        self.config.lexical_weight * lexical_score
                        + self.config.semantic_weight * semantic_score
                    )
                    reason = (
                        "hybrid"
                        if lexical_score > 0 and semantic_score > 0
                        else "semantic" if semantic_score > 0 else "lexical"
                    )
            final = round(max(0.0, min(1.0, final)), 6)
            lexical_score = round(lexical[skill.skill_id], 6)
            semantic_score = semantic[skill.skill_id]
            semantic_score = round(semantic_score, 6) if semantic_score is not None else None
            if final >= self.config.min_score:
                matches.append(
                    SkillMatch(
                        skill,
                        final,
                        lexical_score,
                        semantic_score,
                        matched_alias,
                        reason,
                    )
                )
        priority = {
            "explicit_reference": 0,
            "exact_name": 1,
            "exact_alias": 2,
            "hybrid": 3,
            "semantic": 4,
            "lexical": 5,
        }
        matches.sort(
            key=lambda match: (
                -match.final_score,
                priority[match.retrieval_reason],
                normalize_text(match.skill.name),
                str(match.skill.path),
            )
        )
        return matches[:bounded_k]

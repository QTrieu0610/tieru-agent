"""Model-free M20 retrieval benchmark and lightweight metrics."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tieru.memory.procedural.loader import Skill
from tieru.memory.procedural.retrieval import (
    SkillEmbeddingCache,
    SkillRetrievalConfig,
    SkillRetriever,
    normalize_text,
)


class FixtureEmbeddingBackend:
    model = "deterministic-fixture-v1"

    def __init__(self, query_vectors: dict[str, list[float]], skill_vectors: dict[str, list[float]]):
        self.query_vectors = query_vectors
        self.skill_vectors = skill_vectors

    def embed(self, text: str):
        if text.startswith("Skill: "):
            name = text.splitlines()[0].partition(":")[2].strip()
            return self.skill_vectors[name]
        return self.query_vectors[normalize_text(text)]


def evaluate_fixture(path: Path) -> dict[str, float]:
    value = json.loads(path.read_text(encoding="utf-8"))
    skills = tuple(
        Skill(
            item["name"],
            item["description"],
            "benchmark body",
            Path("fixture") / item["name"] / "SKILL.md",
            aliases=tuple(item.get("aliases", ())),
            keywords=tuple(item.get("keywords", ())),
            domains=tuple(item.get("domains", ())),
        )
        for item in value["skills"]
    )
    query_vectors = {
        normalize_text(item["query"]): item["embedding"] for item in value["cases"]
    }
    skill_vectors = {item["name"]: item["embedding"] for item in value["skills"]}
    lexical = SkillRetriever(
        lambda: skills,
        config=SkillRetrievalConfig(semantic_enabled=False),
    )
    hybrid = SkillRetriever(
        lambda: skills,
        embedding_backend=FixtureEmbeddingBackend(query_vectors, skill_vectors),
        cache=SkillEmbeddingCache(),
    )
    positives = [item for item in value["cases"] if item["expected_skill"]]
    negatives = [item for item in value["cases"] if not item["expected_skill"]]
    counts = {
        "legacy_r1": 0,
        "legacy_r2": 0,
        "lexical_r1": 0,
        "lexical_r2": 0,
        "hybrid_r1": 0,
        "hybrid_r2": 0,
    }
    no_match = 0
    for item in value["cases"]:
        query_words = set(re.findall(r"[a-z0-9]{3,}", item["query"].lower()))
        legacy_ranked = []
        for position, skill in enumerate(skills):
            skill_words = set(
                re.findall(r"[a-z0-9]{3,}", f"{skill.name} {skill.description}".lower())
            )
            overlap = len(query_words & skill_words)
            if overlap >= 2:
                legacy_ranked.append((-overlap, position, skill.name))
        legacy_names = [name for _score, _position, name in sorted(legacy_ranked)[:2]]
        lexical_names = [match.skill.name for match in lexical.retrieve(item["query"], top_k=2)]
        hybrid_names = [match.skill.name for match in hybrid.retrieve(item["query"], top_k=2)]
        expected = item["expected_skill"]
        if expected:
            counts["legacy_r1"] += expected in legacy_names[:1]
            counts["legacy_r2"] += expected in legacy_names[:2]
            counts["lexical_r1"] += expected in lexical_names[:1]
            counts["lexical_r2"] += expected in lexical_names[:2]
            counts["hybrid_r1"] += expected in hybrid_names[:1]
            counts["hybrid_r2"] += expected in hybrid_names[:2]
        else:
            no_match += not hybrid_names
    total = max(1, len(positives))
    negative_total = max(1, len(negatives))
    return {
        "legacy_recall_at_1": counts["legacy_r1"] / total,
        "legacy_recall_at_2": counts["legacy_r2"] / total,
        "lexical_recall_at_1": counts["lexical_r1"] / total,
        "hybrid_recall_at_1": counts["hybrid_r1"] / total,
        "lexical_recall_at_2": counts["lexical_r2"] / total,
        "hybrid_recall_at_2": counts["hybrid_r2"] / total,
        "no_match_accuracy": no_match / negative_total,
    }


def main() -> None:
    default = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "skill_retrieval_cases.json"
    print(json.dumps(evaluate_fixture(default), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

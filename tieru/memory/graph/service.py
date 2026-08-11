"""High-level, model-free operations for Tieru Memory Graph."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tieru.memory.graph.models import GraphContextLimits, Relation
from tieru.memory.graph.store import GraphStore

DEFAULT_CONFIDENCE = {
    "explicit_user_save": 0.9,
    "semantic_memory": 0.8,
    "episodic_memory": 0.7,
    "import": 0.7,
    "system": 0.8,
    "conversation": 0.6,
}


class GraphService:
    """Names-in, typed-records-out facade used by Memory and its tools."""

    def __init__(self, store: GraphStore):
        self.store = store

    def remember_relation(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        subject_type: str = "concept",
        object_type: str = "concept",
        confidence: float | None = None,
        importance: float = 0.5,
        source_type: str = "explicit_user_save",
        source_ref: str = "",
        valid_from: str = "",
        valid_to: str = "",
        subject_metadata: dict[str, Any] | None = None,
        object_metadata: dict[str, Any] | None = None,
    ) -> tuple[Relation, bool]:
        source_key = (source_type or "explicit_user_save").strip().lower()
        resolved_confidence = (
            DEFAULT_CONFIDENCE.get(source_key, 0.6) if confidence is None else confidence
        )
        # Reject malformed or secret-bearing input before either entity upsert
        # commits, so a refused relation cannot leave partial graph state.
        self.store.validate_entity_input(subject, subject_type, subject_metadata)
        self.store.validate_entity_input(object, object_type, object_metadata)
        self.store.validate_relation_input(
            predicate,
            confidence=resolved_confidence,
            importance=importance,
            source_type=source_key,
            source_ref=source_ref,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        subject_entity, _ = self.store.upsert_entity(subject, subject_type, subject_metadata)
        object_entity, _ = self.store.upsert_entity(object, object_type, object_metadata)
        return self.store.add_relation(
            subject_entity.id,
            predicate,
            object_entity.id,
            confidence=resolved_confidence,
            importance=importance,
            source_type=source_key,
            source_ref=source_ref,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def inspect_relation(self, relation_id: int) -> dict[str, Any]:
        return self.store.explain_relation(relation_id)

    def archive_relation(self, relation_id: int) -> Relation:
        return self.store.archive_relation(relation_id)

    def retrieve_context(
        self, message: str, *, limits: GraphContextLimits | None = None
    ) -> str:
        limits = limits or GraphContextLimits()
        # Keep room for at least one adjacent entity. Otherwise a question that
        # names several roots could consume the whole entity budget before any
        # complete edge can be represented.
        root_limit = max(1, limits.max_entities // 2)
        mentions = self.store.find_entity_mentions(message, limit=root_limit)
        if not mentions:
            return ""
        relation_by_id: dict[int, Relation] = {}
        entity_by_id = {entity.id: entity for entity in mentions}
        for entity in mentions:
            neighborhood = self.store.get_neighbors(
                entity.id,
                depth=limits.max_depth,
                max_entities=limits.max_entities,
                max_relations=limits.max_relations,
                current_only=True,
            )
            for neighbor in neighborhood["entities"]:
                if len(entity_by_id) < limits.max_entities or neighbor.id in entity_by_id:
                    entity_by_id[neighbor.id] = neighbor
            for relation in neighborhood["relations"]:
                if len(relation_by_id) >= limits.max_relations:
                    break
                if relation.subject_id in entity_by_id and relation.object_id in entity_by_id:
                    relation_by_id[relation.id] = relation
        if not relation_by_id:
            return ""
        lines = ["## Memory Graph"]
        for relation in list(relation_by_id.values())[: limits.max_relations]:
            subject = entity_by_id[relation.subject_id]
            obj = entity_by_id[relation.object_id]
            lines.append(
                f"- {subject.canonical_name} --{relation.predicate}--> {obj.canonical_name}"
            )
        return "\n".join(lines)

    def snapshot(self, *, max_entities: int = 200, max_relations: int = 200) -> dict[str, Any]:
        return {
            "entities": [asdict(entity) for entity in self.store.list_entities(limit=max_entities)],
            "relations": self.store.relation_views(limit=max_relations),
        }

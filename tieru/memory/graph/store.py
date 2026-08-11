"""SQLite repository for the additive Tieru Memory Graph."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from tieru.memory.graph.models import Entity, Relation
from tieru.memory.graph.policy import GraphPolicy, normalize_predicate
from tieru.memory.personal import UnsafeMemoryError, contains_secret

RELATION_STATUSES = frozenset({"active", "superseded", "contradicted", "archived"})


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", (value or "").strip()).casefold()
    return " ".join(part for part in re.split(r"[^\w]+", value) if part)


def normalize_entity_type(value: str) -> str:
    value = "_".join((value or "concept").strip().casefold().replace("-", " ").split())
    if not value or not all(ch.isalnum() or ch == "_" for ch in value):
        raise ValueError("entity_type must be a machine-readable identifier")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bounded_score(value: float, label: str) -> float:
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"{label} must be between 0.0 and 1.0")
    return score


def _valid_time(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 date or timestamp") from exc
    return value


def _time_key(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_text(value: str, label: str) -> str:
    value = (value or "").strip()
    if contains_secret(value):
        raise UnsafeMemoryError(f"refusing to store {label} that looks like a secret")
    return value


class GraphStore:
    """All graph SQL stays behind this repository boundary."""

    def __init__(self, conn: sqlite3.Connection, policy: GraphPolicy | None = None):
        self.conn = conn
        self.policy = policy or GraphPolicy()

    @staticmethod
    def validate_entity_input(
        canonical_name: str,
        entity_type: str = "concept",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        canonical_name = _safe_text(canonical_name, "entity name")
        if not canonical_name:
            raise ValueError("canonical_name must not be empty")
        normalize_entity_type(entity_type)
        if not normalize_name(canonical_name):
            raise ValueError("canonical_name must contain searchable characters")
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            _safe_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                "entity metadata",
            )

    @staticmethod
    def validate_relation_input(
        predicate: str,
        *,
        confidence: float,
        importance: float,
        source_type: str,
        source_ref: str,
        valid_from: str,
        valid_to: str,
        status: str = "active",
    ) -> None:
        normalize_predicate(predicate)
        _bounded_score(confidence, "confidence")
        _bounded_score(importance, "importance")
        normalize_entity_type(source_type)
        _safe_text(source_ref, "provenance reference")
        valid_from = _valid_time(valid_from, "valid_from")
        valid_to = _valid_time(valid_to, "valid_to")
        if valid_from and valid_to and _time_key(valid_to) < _time_key(valid_from):
            raise ValueError("valid_to must not be earlier than valid_from")
        if (status or "active").strip().lower() not in RELATION_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(RELATION_STATUSES))}")

    @staticmethod
    def _entity(row: sqlite3.Row) -> Entity:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return Entity(
            id=int(row["id"]),
            entity_type=row["entity_type"],
            canonical_name=row["canonical_name"],
            normalized_name=row["normalized_name"],
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or row["created_at"] or "",
        )

    @staticmethod
    def _relation(row: sqlite3.Row) -> Relation:
        return Relation(
            id=int(row["id"]),
            subject_id=int(row["subject_id"]),
            predicate=row["predicate"],
            object_id=int(row["object_id"]),
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            source_type=row["source_type"],
            source_ref=row["source_ref"] or "",
            valid_from=row["valid_from"] or "",
            valid_to=row["valid_to"] or "",
            status=row["status"],
            superseded_by=(int(row["superseded_by"]) if row["superseded_by"] else None),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or row["created_at"] or "",
        )

    def upsert_entity(
        self,
        canonical_name: str,
        entity_type: str = "concept",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Entity, bool]:
        self.validate_entity_input(canonical_name, entity_type, metadata)
        canonical_name = _safe_text(canonical_name, "entity name")
        entity_type = normalize_entity_type(entity_type)
        normalized_name = normalize_name(canonical_name)
        metadata_json = None
        if metadata is not None:
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        existing = self.conn.execute(
            "SELECT * FROM graph_entities WHERE entity_type=? AND normalized_name=?",
            (entity_type, normalized_name),
        ).fetchone()
        now = _now()
        if existing is not None:
            self.conn.execute(
                """UPDATE graph_entities
                   SET canonical_name=?, metadata_json=COALESCE(?, metadata_json), updated_at=?
                   WHERE id=?""",
                (canonical_name, metadata_json, now, existing["id"]),
            )
            self.conn.commit()
            return self.get_entity(int(existing["id"])), False
        cur = self.conn.execute(
            """INSERT INTO graph_entities
               (entity_type, canonical_name, normalized_name, metadata_json, updated_at)
               VALUES (?,?,?,?,?)""",
            (entity_type, canonical_name, normalized_name, metadata_json or "{}", now),
        )
        self.conn.commit()
        return self.get_entity(int(cur.lastrowid)), True

    def get_entity(self, entity_id: int) -> Entity:
        row = self.conn.execute(
            "SELECT * FROM graph_entities WHERE id=?", (int(entity_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"entity:{entity_id}")
        return self._entity(row)

    def find_entities(
        self, query: str, *, entity_type: str | None = None, limit: int = 20
    ) -> list[Entity]:
        normalized = normalize_name(query)
        if not normalized:
            return []
        clauses = ["normalized_name LIKE ?"]
        params: list[Any] = [f"%{normalized}%"]
        if entity_type:
            clauses.append("entity_type=?")
            params.append(normalize_entity_type(entity_type))
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(
            f"""SELECT * FROM graph_entities WHERE {' AND '.join(clauses)}
                ORDER BY CASE WHEN normalized_name=? THEN 0 ELSE 1 END,
                         length(normalized_name), id LIMIT ?""",
            (*params[:-1], normalized, params[-1]),
        ).fetchall()
        return [self._entity(row) for row in rows]

    def find_entity_mentions(self, text: str, *, limit: int = 4) -> list[Entity]:
        normalized = normalize_name(text)
        if not normalized:
            return []
        rows = self.conn.execute(
            """SELECT * FROM graph_entities
               WHERE instr(' ' || ? || ' ', ' ' || normalized_name || ' ') > 0
               ORDER BY length(normalized_name) DESC, id LIMIT ?""",
            (normalized, max(1, min(int(limit), 20))),
        ).fetchall()
        return [self._entity(row) for row in rows]

    def list_entities(self, *, limit: int = 200) -> list[Entity]:
        rows = self.conn.execute(
            "SELECT * FROM graph_entities ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [self._entity(row) for row in rows]

    def add_relation(
        self,
        subject_id: int,
        predicate: str,
        object_id: int,
        *,
        confidence: float = 0.8,
        importance: float = 0.5,
        source_type: str = "explicit_user_save",
        source_ref: str = "",
        valid_from: str = "",
        valid_to: str = "",
        status: str = "active",
    ) -> tuple[Relation, bool]:
        subject_id, object_id = int(subject_id), int(object_id)
        self.get_entity(subject_id)
        self.get_entity(object_id)
        self.validate_relation_input(
            predicate,
            confidence=confidence,
            importance=importance,
            source_type=source_type,
            source_ref=source_ref,
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
        )
        predicate = normalize_predicate(predicate)
        confidence = _bounded_score(confidence, "confidence")
        importance = _bounded_score(importance, "importance")
        source_type = normalize_entity_type(source_type)
        source_ref = _safe_text(source_ref, "provenance reference")
        valid_from = _valid_time(valid_from, "valid_from")
        valid_to = _valid_time(valid_to, "valid_to")
        status = (status or "active").strip().lower()
        existing = self.conn.execute(
            """SELECT * FROM graph_relations
               WHERE subject_id=? AND predicate=? AND object_id=? AND status='active'
               ORDER BY id DESC LIMIT 1""",
            (subject_id, predicate, object_id),
        ).fetchone()
        if existing is not None and status == "active":
            return self._relation(existing), False
        now = _now()
        try:
            cur = self.conn.execute(
                """INSERT INTO graph_relations
                   (subject_id, predicate, object_id, confidence, importance,
                    source_type, source_ref, valid_from, valid_to, status, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    subject_id,
                    predicate,
                    object_id,
                    confidence,
                    importance,
                    source_type,
                    source_ref,
                    valid_from,
                    valid_to,
                    status,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)
            if status == "active" and self.policy.is_single_value(predicate):
                closes_at = valid_from or now
                self.conn.execute(
                    """UPDATE graph_relations
                       SET status='superseded', superseded_by=?,
                           valid_to=CASE WHEN valid_to='' THEN ? ELSE valid_to END,
                           updated_at=?
                       WHERE subject_id=? AND predicate=? AND status='active'
                         AND object_id<>? AND id<>?""",
                    (new_id, closes_at, now, subject_id, predicate, object_id, new_id),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_relation(new_id), True

    def get_relation(self, relation_id: int) -> Relation:
        row = self.conn.execute(
            "SELECT * FROM graph_relations WHERE id=?", (int(relation_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"relation:{relation_id}")
        return self._relation(row)

    def find_relations(
        self,
        *,
        subject_id: int | None = None,
        object_id: int | None = None,
        predicate: str | None = None,
        status: str | None = None,
        current_only: bool = False,
        limit: int = 100,
    ) -> list[Relation]:
        clauses, params = [], []
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(int(subject_id))
        if object_id is not None:
            clauses.append("object_id=?")
            params.append(int(object_id))
        if predicate is not None:
            clauses.append("predicate=?")
            params.append(normalize_predicate(predicate))
        if status is not None:
            status = status.strip().lower()
            if status not in RELATION_STATUSES:
                raise ValueError("invalid relation status")
            clauses.append("status=?")
            params.append(status)
        if current_only:
            now = _now()
            clauses.extend(
                [
                    "status='active'",
                    "(valid_from='' OR datetime(valid_from)<=datetime(?))",
                    "(valid_to='' OR datetime(valid_to)>datetime(?))",
                ]
            )
            params.extend([now, now])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = self.conn.execute(
            f"SELECT * FROM graph_relations {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._relation(row) for row in rows]

    def relations_from(self, entity_id: int, *, limit: int = 100) -> list[Relation]:
        return self.find_relations(subject_id=entity_id, limit=limit)

    def relations_to(self, entity_id: int, *, limit: int = 100) -> list[Relation]:
        return self.find_relations(object_id=entity_id, limit=limit)

    def supersede_relation(self, relation_id: int, superseded_by: int) -> Relation:
        old = self.get_relation(relation_id)
        new = self.get_relation(superseded_by)
        if old.id == new.id:
            raise ValueError("a relation cannot supersede itself")
        closes_at = new.valid_from or _now()
        self.conn.execute(
            """UPDATE graph_relations SET status='superseded', superseded_by=?,
               valid_to=CASE WHEN valid_to='' THEN ? ELSE valid_to END, updated_at=? WHERE id=?""",
            (new.id, closes_at, _now(), old.id),
        )
        self.conn.commit()
        return self.get_relation(old.id)

    def archive_relation(self, relation_id: int) -> Relation:
        self.get_relation(relation_id)
        now = _now()
        self.conn.execute(
            """UPDATE graph_relations SET status='archived',
               valid_to=CASE WHEN valid_to='' THEN ? ELSE valid_to END, updated_at=? WHERE id=?""",
            (now, now, int(relation_id)),
        )
        self.conn.commit()
        return self.get_relation(relation_id)

    def contradict_relation(self, relation_id: int) -> Relation:
        self.get_relation(relation_id)
        now = _now()
        self.conn.execute(
            "UPDATE graph_relations SET status='contradicted', updated_at=? WHERE id=?",
            (now, int(relation_id)),
        )
        self.conn.commit()
        return self.get_relation(relation_id)

    def get_neighbors(
        self,
        entity_id: int,
        *,
        depth: int = 1,
        max_entities: int = 20,
        max_relations: int = 50,
        current_only: bool = True,
    ) -> dict[str, list[Entity] | list[Relation]]:
        if depth not in (1, 2):
            raise ValueError("depth must be 1 or 2")
        max_entities = max(1, min(int(max_entities), 20))
        max_relations = max(1, min(int(max_relations), 50))
        seen_entities = {int(entity_id)}
        seen_relations: set[int] = set()
        frontier = {int(entity_id)}
        relations: list[Relation] = []
        for _ in range(depth):
            next_frontier: set[int] = set()
            for current in sorted(frontier):
                candidates = self.find_relations(
                    subject_id=current, current_only=current_only, limit=max_relations
                )
                candidates += self.find_relations(
                    object_id=current, current_only=current_only, limit=max_relations
                )
                for relation in candidates:
                    if relation.id in seen_relations or len(relations) >= max_relations:
                        continue
                    seen_relations.add(relation.id)
                    relations.append(relation)
                    for neighbor in (relation.subject_id, relation.object_id):
                        if neighbor not in seen_entities and len(seen_entities) < max_entities:
                            seen_entities.add(neighbor)
                            next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier or len(relations) >= max_relations:
                break
        entities = [self.get_entity(item) for item in sorted(seen_entities)]
        return {"entities": entities, "relations": relations}

    def explain_relation(self, relation_id: int) -> dict[str, Any]:
        relation = self.get_relation(relation_id)
        payload: dict[str, Any] = {
            "relation": asdict(relation),
            "subject": asdict(self.get_entity(relation.subject_id)),
            "object": asdict(self.get_entity(relation.object_id)),
            "provenance": {
                "source_type": relation.source_type,
                "source_ref": relation.source_ref,
            },
        }
        if relation.superseded_by is not None:
            payload["superseded_by"] = asdict(self.get_relation(relation.superseded_by))
        return payload

    def relation_views(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return [self.explain_relation(row.id) for row in self.find_relations(limit=limit)]

    def relation_lines(self, relations: Iterable[Relation]) -> list[str]:
        lines = []
        for relation in relations:
            subject = self.get_entity(relation.subject_id)
            obj = self.get_entity(relation.object_id)
            lines.append(f"{subject.canonical_name} --{relation.predicate}--> {obj.canonical_name}")
        return lines

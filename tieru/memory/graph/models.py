"""Typed records for Tieru Memory Graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Entity:
    id: int
    entity_type: str
    canonical_name: str
    normalized_name: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Relation:
    id: int
    subject_id: int
    predicate: str
    object_id: int
    confidence: float
    importance: float
    source_type: str
    source_ref: str
    valid_from: str
    valid_to: str
    status: str
    superseded_by: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GraphContextLimits:
    """Hard prompt bounds; depth two is the maximum supported by M6."""

    max_entities: int = 4
    max_relations: int = 8
    max_depth: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.max_entities <= 20:
            raise ValueError("max_entities must be between 1 and 20")
        if not 1 <= self.max_relations <= 50:
            raise ValueError("max_relations must be between 1 and 50")
        if not 1 <= self.max_depth <= 2:
            raise ValueError("max_depth must be 1 or 2")

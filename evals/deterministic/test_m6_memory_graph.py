"""Deterministic contracts for M6 — Tieru Memory Graph.

The graph is an additive, model-free SQLite capability. These tests deliberately
exercise the public store/service/facade boundaries instead of reaching around
them with graph SQL, except where migration and schema indexes are the subject.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tieru.config import Settings
from tieru.db import connect
from tieru.memory import Memory
from tieru.memory.graph import GraphContextLimits, GraphService, GraphStore
from tieru.memory.personal import UnsafeMemoryError
from tieru.tools.memory_admin import make_manage_memory_tool
from tieru.tools.registry import ToolRegistry


def graph(tmp_path) -> GraphService:
    return GraphService(GraphStore(connect(tmp_path)))


def test_entity_create_read_normalize_upsert_and_custom_type(tmp_path):
    store = graph(tmp_path).store
    first, created = store.upsert_entity(
        "  Local-AI  ", "Technology", {"scope": "personal"}
    )
    duplicate, created_again = store.upsert_entity(
        "local ai", "technology", {"scope": "local"}
    )

    assert created and not created_again
    assert duplicate.id == first.id
    assert duplicate.normalized_name == "local ai"
    assert duplicate.canonical_name == "local ai"
    assert duplicate.metadata == {"scope": "local"}
    assert store.get_entity(first.id) == duplicate
    assert store.find_entities("LOCAL AI") == [duplicate]

    custom, _ = store.upsert_entity("Blue Team", "research_cohort")
    assert custom.entity_type == "research_cohort"


def test_relation_crud_queries_scores_provenance_and_temporal_fields(tmp_path):
    service = graph(tmp_path)
    relation, created = service.remember_relation(
        subject="Fipilot",
        subject_type="project",
        predicate="uses-technology",
        object="FastAPI",
        object_type="technology",
        confidence=0.85,
        importance=0.75,
        source_type="semantic_memory",
        source_ref="semantic:42",
        valid_from="2026-08-01",
        valid_to="2027-08-01T00:00:00Z",
    )

    assert created
    assert relation.predicate == "USES_TECHNOLOGY"
    assert relation.confidence == 0.85
    assert relation.importance == 0.75
    assert relation.source_type == "semantic_memory"
    assert relation.source_ref == "semantic:42"
    assert relation.valid_from == "2026-08-01"
    assert relation.valid_to == "2027-08-01T00:00:00Z"
    assert service.store.get_relation(relation.id) == relation
    assert service.store.relations_from(relation.subject_id) == [relation]
    assert service.store.relations_to(relation.object_id) == [relation]

    explained = service.inspect_relation(relation.id)
    assert explained["subject"]["canonical_name"] == "Fipilot"
    assert explained["object"]["canonical_name"] == "FastAPI"
    assert explained["provenance"] == {
        "source_type": "semantic_memory",
        "source_ref": "semantic:42",
    }


def test_relation_validation_duplicate_handling_and_status_operations(tmp_path):
    service = graph(tmp_path)
    first, created = service.remember_relation(
        subject="User", predicate="LIKES", object="SQLite"
    )
    duplicate, created_again = service.remember_relation(
        subject="User", predicate="likes", object="SQLite", confidence=0.1
    )
    assert created and not created_again and duplicate == first

    with pytest.raises(ValueError, match="confidence"):
        service.remember_relation(
            subject="User", predicate="LIKES", object="Postgres", confidence=1.01
        )
    with pytest.raises(ValueError, match="valid_to"):
        service.remember_relation(
            subject="User",
            predicate="LIKES",
            object="DuckDB",
            valid_from="2026-09-01T00:00:00+07:00",
            valid_to="2026-08-31T16:00:00Z",
        )

    assert service.store.contradict_relation(first.id).status == "contradicted"
    assert service.archive_relation(first.id).status == "archived"


def test_single_value_predicate_supersedes_and_preserves_history(tmp_path):
    service = graph(tmp_path)
    old, _ = service.remember_relation(
        subject="Tieru",
        predicate="USES_DEFAULT_MODEL",
        object="Gemini",
        object_type="model",
        valid_from="2026-07-01",
    )
    new, _ = service.remember_relation(
        subject="Tieru",
        predicate="uses default model",
        object="gemma4:e2b",
        object_type="model",
        valid_from="2026-08-02",
    )

    historical = service.store.get_relation(old.id)
    assert historical.status == "superseded"
    assert historical.superseded_by == new.id
    assert historical.valid_to == "2026-08-02"
    assert service.inspect_relation(old.id)["superseded_by"]["id"] == new.id
    assert service.store.find_relations(subject_id=old.subject_id, status="active") == [new]


def test_multi_value_predicate_keeps_multiple_active_relations(tmp_path):
    service = graph(tmp_path)
    fastapi, _ = service.remember_relation(
        subject="Fipilot", predicate="USES_TECHNOLOGY", object="FastAPI"
    )
    sqlite, _ = service.remember_relation(
        subject="Fipilot", predicate="USES_TECHNOLOGY", object="SQLite"
    )

    active = service.store.find_relations(
        subject_id=fastapi.subject_id, predicate="USES_TECHNOLOGY", status="active"
    )
    assert {row.id for row in active} == {fastapi.id, sqlite.id}


def test_current_relation_query_honors_temporal_windows(tmp_path):
    service = graph(tmp_path)
    current, _ = service.remember_relation(
        subject="User",
        predicate="WORKS_ON",
        object="Current",
        valid_from="2000-08-10T00:00:00+07:00",
        valid_to="2999-01-01T00:00:00Z",
    )
    service.remember_relation(
        subject="User",
        predicate="WORKS_ON",
        object="Expired",
        valid_to="2020-01-01",
    )
    service.remember_relation(
        subject="User",
        predicate="WORKS_ON",
        object="Future",
        valid_from="2999-01-01T00:00:00+07:00",
    )

    assert service.store.find_relations(
        subject_id=current.subject_id, current_only=True
    ) == [current]


def test_graph_neighbors_and_prompt_context_are_strictly_bounded(tmp_path):
    service = graph(tmp_path)
    for name in ("A", "B", "C", "D", "E"):
        service.remember_relation(
            subject="Tieru", predicate="USES_TECHNOLOGY", object=name
        )

    neighborhood = service.store.get_neighbors(
        service.store.find_entities("Tieru")[0].id,
        depth=1,
        max_entities=3,
        max_relations=2,
    )
    assert len(neighborhood["entities"]) <= 3
    assert len(neighborhood["relations"]) <= 2

    context = service.retrieve_context(
        "What technology does Tieru use?",
        limits=GraphContextLimits(max_entities=3, max_relations=2, max_depth=1),
    )
    assert context.startswith("## Memory Graph")
    assert context.count("\n- ") <= 2


def test_memory_facade_resolves_graph_before_any_model_call(tmp_path):
    class NoModelCalls:
        @property
        def messages(self):
            raise AssertionError("exact graph retrieval must not call an LLM")

    settings = Settings(home=tmp_path, api_key="")
    settings.ensure_home()
    memory = Memory(connect(tmp_path), settings, NoModelCalls())
    memory.graph.remember_relation(
        subject="Tieru",
        predicate="USES_DEFAULT_MODEL",
        object="gemma4:e2b",
        object_type="model",
    )
    events = []

    context = memory.gated_retrieve(
        "What model does Tieru use locally?", lambda kind, event: events.append((kind, event))
    )

    assert "Tieru --USES_DEFAULT_MODEL--> gemma4:e2b" in context
    assert events == [
        ("gate", {"decision": "retrieve", "reason": "exact Memory Graph match"})
    ]


def test_manage_memory_graph_writes_obey_confirmation_policy(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    memory = Memory(connect(tmp_path), settings, client=None)
    tool = make_manage_memory_tool(memory)
    args = {
        "action": "remember_relation",
        "subject": "User",
        "predicate": "PREFERS",
        "object": "Local AI",
        "object_type": "preference",
    }

    denied = ToolRegistry()
    denied.register(tool)
    assert json.loads(denied.execute("manage_memory", args))["error"]["policy"] == "confirm"
    assert memory.graph.store.find_relations() == []

    approved = ToolRegistry(approval_handler=lambda _request: True)
    approved.register(tool)
    saved = approved.execute("manage_memory", args)
    assert saved.startswith("Saved as relation:")
    relation_id = int(saved.removeprefix("Saved as relation:").removesuffix("."))
    inspected = json.loads(
        approved.execute(
            "manage_memory", {"action": "inspect_relation", "id": relation_id}
        )
    )
    assert inspected["relation"]["source_type"] == "explicit_user_save"
    assert tool.action_policies["inspect_relation"] == "allow"
    assert tool.action_policies["archive_relation"] == "confirm"


def test_graph_rejects_secrets_in_entities_metadata_and_provenance(tmp_path):
    service = graph(tmp_path)
    with pytest.raises(UnsafeMemoryError):
        service.remember_relation(
            subject="User", predicate="USES", object="api_key=sk-super-secret-value"
        )
    with pytest.raises(UnsafeMemoryError):
        service.store.upsert_entity(
            "Service", metadata={"credential": "token=abc123456789"}
        )
    with pytest.raises(UnsafeMemoryError):
        service.remember_relation(
            subject="User",
            predicate="USES",
            object="Service",
            source_ref="token=abc123456789",
        )
    assert service.snapshot() == {"entities": [], "relations": []}


def test_existing_database_migrates_additively_and_idempotently(tmp_path):
    legacy = sqlite3.connect(tmp_path / "state.db")
    legacy.execute(
        """CREATE TABLE facts (
               id INTEGER PRIMARY KEY, subject TEXT NOT NULL, content TEXT NOT NULL,
               source TEXT DEFAULT 'user', created_at TEXT DEFAULT (datetime('now'))
           )"""
    )
    legacy.execute("INSERT INTO facts (subject, content) VALUES ('user', 'kept')")
    legacy.commit()
    legacy.close()

    first = connect(tmp_path)
    first.close()
    second = connect(tmp_path)
    assert second.execute("SELECT content FROM facts WHERE id=1").fetchone()[0] == "kept"
    assert {
        "graph_entities",
        "graph_relations",
    }.issubset(
        {
            row[0]
            for row in second.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    )
    indexes = {
        row[0]
        for row in second.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {
        "graph_entities_normalized_name_idx",
        "graph_entities_type_idx",
        "graph_relations_subject_idx",
        "graph_relations_object_idx",
        "graph_relations_predicate_idx",
        "graph_relations_status_idx",
    }.issubset(indexes)


def test_memory_export_and_dashboard_surface_are_inspectable(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    memory = Memory(connect(tmp_path), settings, client=None)
    relation, _ = memory.graph.remember_relation(
        subject="User",
        subject_type="person",
        predicate="PREFERS",
        object="Local AI",
        object_type="preference",
        confidence=0.9,
        importance=0.8,
        source_ref="semantic:7",
    )
    memory.export_markdown()

    exported = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Memory Graph" in exported
    for value in ("User", "PREFERS", "Local AI", "confidence: 0.9", "semantic:7"):
        assert value in exported

    snapshot = memory.graph.snapshot()
    assert snapshot["relations"][0]["relation"]["id"] == relation.id
    view = Path("tieru/ops/static/js/views.js").read_text(encoding="utf-8")
    for field in ("Entities", "Relations", "predicate", "confidence", "status", "source"):
        assert field in view


def test_existing_text_memory_remains_independent_of_graph_population(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    memory = Memory(connect(tmp_path), settings, client=None)
    memory.facts.add("user", "User prefers local inference")
    memory.episodes.add("Discussed local inference", "2026-08-10")

    assert memory.facts.search("local inference")
    assert memory.episodes.search("local inference")
    assert memory.graph.snapshot() == {"entities": [], "relations": []}

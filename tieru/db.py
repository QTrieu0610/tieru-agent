"""One SQLite file (state.db) holds everything Tieru remembers and does.

SQLite + FTS5 keeps the default memory source inspectable and requires no server.
Open it yourself anytime:  sqlite3 .tieru/state.db '.tables'
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tieru.execution import initialize_execution_schema
from tieru.recovery import initialize_recovery_schema
from tieru.scheduler import initialize_scheduler_schema
from tieru.tasks import initialize_task_schema

SCHEMA = """
-- Flagship-task artifact: events the calendar tool creates. The deterministic
-- eval asserts directly on rows in this table ("did the meeting trigger?").
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,           -- ISO 8601
    "end" TEXT,
    attendees TEXT DEFAULT '',     -- comma-separated
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Semantic memory: durable facts about you, your people, your projects.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,         -- who/what the fact is about, e.g. 'alex'
    content TEXT NOT NULL,         -- the fact itself
    source TEXT DEFAULT 'user',    -- 'user' (told directly) or 'consolidation'
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    provenance TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    trusted INTEGER DEFAULT 1,
    fingerprint TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, content, content=facts, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;

-- Episodic memory: dated things that happened (past chats, distilled).
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    happened_at TEXT NOT NULL,     -- ISO 8601 date of the episode
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    source TEXT DEFAULT 'user',
    provenance TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    trusted INTEGER DEFAULT 1,
    fingerprint TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary, content=episodes, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
END;

-- Raw chat log ("save the messages" box). Consolidation reads from here.
-- session_id tags each row with which conversation it belongs to, so the
-- dashboard can offer "New chat" and switch between past sessions (like a
-- chat app). Everything shares this one table — sessions are just a label.
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,            -- 'user' | 'assistant'
    content TEXT NOT NULL,
    consolidated INTEGER DEFAULT 0,
    session_id TEXT DEFAULT 'default',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Tieru Memory Graph: an additive typed relationship layer. Existing facts,
-- episodes, skills, and chat rows remain independent and authoritative in
-- their current stores; graph population begins only through explicit writes.
CREATE TABLE IF NOT EXISTS graph_entities (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(entity_type, normalized_name)
);

CREATE TABLE IF NOT EXISTS graph_relations (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES graph_entities(id),
    predicate TEXT NOT NULL,
    object_id INTEGER NOT NULL REFERENCES graph_entities(id),
    confidence REAL NOT NULL DEFAULT 0.8 CHECK(confidence BETWEEN 0.0 AND 1.0),
    importance REAL NOT NULL DEFAULT 0.5 CHECK(importance BETWEEN 0.0 AND 1.0),
    source_type TEXT NOT NULL DEFAULT 'explicit_user_save',
    source_ref TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'superseded', 'contradicted', 'archived')),
    superseded_by INTEGER REFERENCES graph_relations(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS graph_entities_normalized_name_idx
    ON graph_entities(normalized_name);
CREATE INDEX IF NOT EXISTS graph_entities_type_idx
    ON graph_entities(entity_type);
CREATE INDEX IF NOT EXISTS graph_relations_subject_idx
    ON graph_relations(subject_id);
CREATE INDEX IF NOT EXISTS graph_relations_object_idx
    ON graph_relations(object_id);
CREATE INDEX IF NOT EXISTS graph_relations_predicate_idx
    ON graph_relations(predicate);
CREATE INDEX IF NOT EXISTS graph_relations_status_idx
    ON graph_relations(status);

-- M20 Hybrid Skill Retrieval: bounded retrieval-metadata vectors only. The
-- instruction body and user query are never stored in this cache.
CREATE TABLE IF NOT EXISTS skill_embeddings (
    skill_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(skill_id, model)
);

-- Tieru Replay: bounded, normalized observability for one user turn. Replay is
-- deliberately separate from chat_log and memory so retention can remove
-- telemetry without deleting conversations or anything Tieru remembers.
CREATE TABLE IF NOT EXISTS replay_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL DEFAULT 'cli',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    role TEXT NOT NULL DEFAULT 'main',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    iterations INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    event_count INTEGER NOT NULL DEFAULT 0,
    tool_count INTEGER NOT NULL DEFAULT 0,
    trust_decision_count INTEGER NOT NULL DEFAULT 0,
    input_preview TEXT NOT NULL DEFAULT '',
    output_preview TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS replay_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES replay_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    node TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, sequence)
);

CREATE INDEX IF NOT EXISTS replay_events_run_sequence_idx
    ON replay_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS replay_runs_session_idx
    ON replay_runs(session_id);
CREATE INDEX IF NOT EXISTS replay_runs_started_idx
    ON replay_runs(started_at);
CREATE INDEX IF NOT EXISTS replay_runs_status_idx
    ON replay_runs(status);

-- Tieru Shadow: passive, local aggregation over Forge-compatible Replay
-- structure. These rows are operational suggestions, never Memory or policy.
CREATE TABLE IF NOT EXISTS shadow_patterns (
    id TEXT PRIMARY KEY,
    workflow_signature TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'observing',
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    successful_count INTEGER NOT NULL DEFAULT 0,
    verification_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    tools_json TEXT NOT NULL DEFAULT '[]',
    operations_json TEXT NOT NULL DEFAULT '[]',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'low',
    suppress_until_count INTEGER NOT NULL DEFAULT 0,
    snoozed_until TEXT NOT NULL DEFAULT '',
    forge_draft_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS shadow_suggestions (
    id TEXT PRIMARY KEY,
    pattern_id TEXT NOT NULL UNIQUE REFERENCES shadow_patterns(id) ON DELETE CASCADE,
    workflow_signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    occurrence_count INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    representative_runs_json TEXT NOT NULL DEFAULT '[]',
    suggested_name TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    tools_json TEXT NOT NULL DEFAULT '[]',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    explanation_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    snoozed_until TEXT NOT NULL DEFAULT '',
    forge_draft_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shadow_observations (
    run_id TEXT PRIMARY KEY,
    pattern_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS shadow_patterns_status_idx ON shadow_patterns(status);
CREATE INDEX IF NOT EXISTS shadow_patterns_last_seen_idx ON shadow_patterns(last_seen_at);
CREATE INDEX IF NOT EXISTS shadow_suggestions_status_idx ON shadow_suggestions(status);

-- Tieru Capsule: safe operational provenance only. Capsule contents are never
-- duplicated here, and deleting an archive does not affect imported state.
CREATE TABLE IF NOT EXISTS capsule_audits (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK(operation IN ('export', 'import')),
    capsule_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    target_name TEXT NOT NULL DEFAULT '',
    create_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS capsule_audits_capsule_idx ON capsule_audits(capsule_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column upgrades for databases created before a
    column existed. SQLite has no 'ADD COLUMN IF NOT EXISTS', so we check."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_log)").fetchall()}
    if "session_id" not in cols:
        conn.execute("ALTER TABLE chat_log ADD COLUMN session_id TEXT DEFAULT 'default'")
        conn.commit()
    if "source" not in cols:
        # which gateway a message came in through (cli / voice / telegram / dashboard)
        conn.execute("ALTER TABLE chat_log ADD COLUMN source TEXT DEFAULT 'cli'")
        conn.commit()
    if "meta" not in cols:
        # per-turn telemetry as JSON on the assistant row (gate decision,
        # latency, iterations, tools) — so reopening a thread still shows how
        # each answer was produced, not just the plain text.
        conn.execute("ALTER TABLE chat_log ADD COLUMN meta TEXT")
        conn.commit()
    additive = {
        "facts": {
            "updated_at": "TEXT DEFAULT ''",
            "provenance": "TEXT DEFAULT ''",
            "importance": "REAL DEFAULT 0.5",
            "trusted": "INTEGER DEFAULT 1",
            "fingerprint": "TEXT DEFAULT ''",
        },
        "episodes": {
            "updated_at": "TEXT DEFAULT ''",
            "source": "TEXT DEFAULT 'user'",
            "provenance": "TEXT DEFAULT ''",
            "importance": "REAL DEFAULT 0.5",
            "trusted": "INTEGER DEFAULT 1",
            "fingerprint": "TEXT DEFAULT ''",
        },
    }
    for table, columns in additive.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.commit()


def connect(home: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    # check_same_thread=False lets the dashboard's threaded HTTP server reuse
    # one agent connection across worker threads (guarded by a lock). busy_timeout
    # avoids "database is locked" when the dashboard reads while a chat writes.
    conn = sqlite3.connect(home / "state.db", check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(SCHEMA)
    initialize_execution_schema(conn)
    initialize_recovery_schema(conn)
    initialize_task_schema(conn)
    initialize_scheduler_schema(conn)
    _migrate(conn)
    return conn

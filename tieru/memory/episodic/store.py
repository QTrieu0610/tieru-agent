"""Episodic memory — dated events: what happened, and when.

Semantic memory answers "what do I know?"; episodic answers "what happened
last Tuesday?". Same SQLite file, but every row carries a date and retrieval
blends relevance (FTS rank) with recency — the whiteboard's "RAG for
relevance + SQL for recency".
"""

from __future__ import annotations

import sqlite3

from tieru.memory.semantic.store import _fts_query


class SqliteEpisodeStore:
    def __init__(self, conn: sqlite3.Connection, unified=None):
        self.conn = conn
        self.unified = unified

    def add(self, summary: str, happened_at: str) -> None:
        if self.unified is not None:
            self.unified.add(
                content=summary,
                kind="episode",
                source="consolidation",
                provenance="model consolidation",
                happened_at=happened_at,
                trusted=True,
            )
            return
        self.conn.execute(
            "INSERT INTO episodes (happened_at, summary) VALUES (?,?)",
            (happened_at, summary),
        )
        self.conn.commit()

    def search(self, query: str, top_k: int = 3) -> list[str]:
        """Relevance first (FTS), most recent first among matches."""
        fts = _fts_query(query)
        if not fts:
            return self.recent(top_k)
        rows = self.conn.execute(
            "SELECT e.happened_at, e.summary FROM episodes_fts JOIN episodes e "
            "ON e.id = episodes_fts.rowid WHERE episodes_fts MATCH ? AND e.trusted = 1 "
            "ORDER BY rank, e.happened_at DESC LIMIT ?",
            (fts, top_k),
        ).fetchall()
        return [f"({r['happened_at']}) {r['summary']}" for r in rows]

    def recent(self, top_k: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC LIMIT ?",
            (top_k,),
        ).fetchall()
        return [f"({r['happened_at']}) {r['summary']}" for r in rows]

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, happened_at, summary, created_at FROM episodes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, episode_id: int) -> bool:
        if self.unified is not None:
            return self.unified.delete(f"episode:{episode_id}")
        cur = self.conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        self.conn.commit()
        return cur.rowcount > 0

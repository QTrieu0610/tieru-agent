"""Semantic memory — durable facts, keyword-searched with SQLite FTS5.

The Hermes insight from the whiteboard: "keyword top-k, no embedding". For a
single user's facts, ranked keyword search (BM25) is fast, fully local, and —
crucially for teaching — you can read the whole index with sqlite3.
Want vectors? Set TIERU_SEMANTIC_STORE=supabase (see supabase_store.py).
"""

from __future__ import annotations

import re
import sqlite3


def _fts_query(text: str) -> str:
    """User text isn't a valid FTS5 query (quotes/punctuation break MATCH).
    Reduce it to `word OR word OR ...` over alphanumeric tokens."""
    words = re.findall(r"[a-zA-Z0-9]{2,}", text.lower())
    return " OR ".join(dict.fromkeys(words)) if words else ""


class SqliteFactStore:
    def __init__(self, conn: sqlite3.Connection, unified=None):
        self.conn = conn
        self.unified = unified
        self.last_created = True

    def add(self, subject: str, content: str, source: str = "user") -> int | None:
        if self.unified is not None:
            record, created = self.unified.add(
                content=content,
                kind="fact",
                source=source,
                provenance=("model consolidation" if source == "consolidation"
                            else "explicit user memory"),
                subject=subject,
                trusted=source != "web",
            )
            self.last_created = created
            return int(record.id.split(":", 1)[1])
        self.conn.execute(
            "INSERT INTO facts (subject, content, source) VALUES (?,?,?)",
            (subject.lower().strip(), content, source),
        )
        self.conn.commit()
        self.last_created = True
        return None

    def search(self, query: str, top_k: int = 4) -> list[str]:
        fts = _fts_query(query)
        if not fts:
            return []
        rows = self.conn.execute(
            "SELECT f.subject, f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND f.trusted = 1 ORDER BY rank LIMIT ?",
            (fts, top_k),
        ).fetchall()
        return [f"[{r['subject']}] {r['content']}" for r in rows]

    # --- CRUD: humans (dashboard) and the agent (manage_memory tool) edit memory.
    # The facts_au / facts_ad triggers keep the FTS index in sync automatically.
    def list(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, subject, content, source, created_at FROM facts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        fts = _fts_query(query)
        if not fts:
            return self.list(top_k)
        rows = self.conn.execute(
            "SELECT f.id, f.subject, f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts, top_k),
        ).fetchall()
        return [dict(r) for r in rows]

    def update(self, fact_id: int, content: str, subject: str | None = None) -> bool:
        if self.unified is not None:
            changes = {"content": content}
            if subject is not None:
                changes["subject"] = subject
            try:
                self.unified.update(f"fact:{fact_id}", **changes)
            except KeyError:
                return False
            return True
        if subject is None:
            cur = self.conn.execute("UPDATE facts SET content=? WHERE id=?", (content, fact_id))
        else:
            cur = self.conn.execute(
                "UPDATE facts SET content=?, subject=? WHERE id=?",
                (content, subject.lower().strip(), fact_id),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, fact_id: int) -> bool:
        if self.unified is not None:
            return self.unified.delete(f"fact:{fact_id}")
        cur = self.conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        self.conn.commit()
        return cur.rowcount > 0

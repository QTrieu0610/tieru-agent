"""Tieru Memory facade — four additive capabilities behind one interface.

    procedural  SKILL.md files      how to act
    semantic    facts table (FTS5)  what is durably true
    episodic    episodes table      what happened, when
    graph       typed relationships how entities connect and change

Plus the two agents that manage them:
    retrieval_gate   decides IF a turn needs memory   (hero moment #1)
    consolidation    optionally distills chats after N exchanges when explicitly enabled
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import anthropic

from tieru.config import Settings
from tieru.memory import consolidation, retrieval_gate
from tieru.memory.episodic.store import SqliteEpisodeStore
from tieru.memory.graph import GraphService, GraphStore
from tieru.memory.personal import PersonalMemoryStore, redact_secrets
from tieru.memory.procedural.loader import SkillLoader
from tieru.memory.procedural.retrieval import SkillEmbeddingCache
from tieru.memory.semantic.store import SqliteFactStore


def bundled_skill_dirs() -> list[Path]:
    """Where the skills that SHIP with Tieru live — and why there are two answers.

    Contributors add skills to `skills/` at the repo root: that is what
    CONTRIBUTING.md documents, what CI validates, and what a checkout has. But
    the wheel only packages the `tieru/` directory, so a `pip install tieru-agent`
    would have found nothing there and silently started with zero skills —
    procedural memory, one of the four pillars, quietly missing. (It did, until
    2026-07-31.) pyproject force-includes the same folder into the wheel at
    `tieru/skills`, so an installed Tieru finds it next to the code.

    Exactly one of these exists at a time — the package copy only in a built
    wheel, the repo copy only in a checkout — so returning both is not a
    double-load, it is "wherever you installed from, the skills came too".
    """
    here = Path(__file__).resolve()
    return [p for p in (here.parents[1] / "skills", here.parents[2] / "skills") if p.is_dir()]


class Memory:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, client: anthropic.Anthropic,
                 episode_store=None):
        # episode_store: inject an already-built store (the dashboard caches ONE
        # NotionEpisodeStore process-wide — its constructor hits the network,
        # so building one per Memory would re-query Notion on every poll).
        self.conn = conn
        self.settings = settings
        self.client = client
        small = settings.role("small")
        self.model = small.model
        self.provider = small.provider
        self.store = PersonalMemoryStore(conn, settings.memory_max_records)
        self.facts = self._make_fact_store(conn, settings, self.store)
        self.episodes = episode_store if episode_store is not None else self._make_episode_store(
            conn, settings, self.store
        )
        bundled = bundled_skill_dirs()
        embedding_backend = getattr(self.facts, "semantic", None)
        if not callable(getattr(embedding_backend, "embed", None)):
            embedding_backend = None
        self.skills = SkillLoader(
            [*bundled, settings.home / "skills"],
            reviewed_dirs=bundled,
            embedding_backend=embedding_backend,
            embedding_cache=SkillEmbeddingCache(conn),
        )
        self.graph = GraphService(GraphStore(conn))

    def set_model(self, client, model: str, provider: str) -> None:
        """Use the turn's sticky Fabric target for memory-side model calls."""
        self.client = client
        self.model = model
        self.provider = provider

    @staticmethod
    def _make_fact_store(conn, settings, unified=None):
        if settings.semantic_store == "supabase":
            from tieru.memory.semantic.supabase_store import SupabaseFactStore

            return HybridFactStore(SqliteFactStore(conn, unified), SupabaseFactStore(settings))
        return SqliteFactStore(conn, unified)

    @staticmethod
    def _make_episode_store(conn, settings, unified=None):
        if settings.episodic_store == "notion":
            from tieru.memory.episodic.notion_store import NotionEpisodeStore

            return NotionEpisodeStore()
        return SqliteEpisodeStore(conn, unified)

    # ---- retrieval (gated — see retrieval_gate.py for why)
    def gated_retrieve(self, message: str, notify=None) -> str:
        graph_context = self.graph.retrieve_context(message)
        if graph_context:
            self.last_context_source = "memory_graph"
            if notify:
                notify("gate", {"decision": "retrieve", "reason": "exact Memory Graph match"})
            return graph_context
        started = time.perf_counter()
        if notify:
            notify("model_call_started", {"role": "small", "model": self.model,
                                          "provider": self.provider, "purpose": "memory_gate"})
        retrieve, query, reason = retrieval_gate.should_retrieve(
            self.client, self.model, message
        )
        if notify:
            notify("model_call_completed", {"role": "small", "model": self.model,
                                            "provider": self.provider,
                                            "purpose": "memory_gate",
                                            "duration_ms": int((time.perf_counter() - started) * 1000),
                                            "stop_reason": "decision"})
            notify("gate", {"decision": "retrieve" if retrieve else "skip", "reason": reason})
        if not retrieve:
            self.last_context_source = "memory"
            return ""
        found = self.facts.search(query, self.settings.retrieval_top_k)
        found += self.episodes.search(query, top_k=3)
        if notify:
            notify("memory_retrieval", {"count": len(found),
                                        "source": ["facts", "episodes"]})
        self.last_context_source = "memory"
        return "\n".join(found)

    # ---- procedural
    def matching_skills(self, message: str) -> str:
        matched = self.skills.match(message)
        return "\n\n".join(f"### {s.name}\n{s.body}" for s in matched)

    def matching_skill_records(self, message: str):
        """Return matched skills with explicit loader-owned review classification."""
        return self.skills.match(message)

    def matching_skill_matches(self, message: str):
        """Return explained M20 matches; relevance never changes review authority."""
        return self.skills.retrieve(message)

    # ---- write paths
    def log_chat(self, user_message: str, reply: str, session_id: str = "default",
                 source: str = "cli", meta: dict | None = None) -> None:
        import json as _json

        from tieru.memory.personal import redact_secrets

        user_message = redact_secrets(user_message)
        reply = redact_secrets(reply)
        self.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source) VALUES ('user', ?, ?, ?)",
            (user_message, session_id, source),
        )
        # meta (gate/latency/iterations/tools) rides on the assistant row so a
        # reopened thread can render the full turn card, not just the text.
        self.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source, meta) VALUES ('assistant', ?, ?, ?, ?)",
            (reply, session_id, source, _json.dumps(meta) if meta else None),
        )
        self.conn.commit()

    # ---- sessions (for the dashboard's chat history + "New chat")
    def session_history(self, session_id: str) -> list[tuple[str, str]]:
        """The (user, assistant) exchanges of one past session, in order — used
        to reload working memory when the user switches back to a conversation."""
        rows = self.conn.execute(
            "SELECT role, content FROM chat_log WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        pairs, pending = [], None
        for r in rows:
            if r["role"] == "user":
                pending = r["content"]
            elif pending is not None:
                pairs.append((pending, r["content"]))
                pending = None
        return pairs

    def list_sessions(self) -> list[dict]:
        """One row per conversation: id, first user message (the title), message
        count, and when it started — newest first."""
        rows = self.conn.execute(
            """SELECT session_id,
                      COUNT(*) AS messages,
                      MIN(created_at) AS started_at,
                      MAX(created_at) AS last_at
               FROM chat_log GROUP BY session_id ORDER BY last_at DESC"""
        ).fetchall()
        out = []
        for r in rows:
            first = self.conn.execute(
                "SELECT content FROM chat_log WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
                (r["session_id"],),
            ).fetchone()
            out.append({
                "id": r["session_id"],
                "title": (first["content"][:60] if first else "(empty)"),
                "messages": r["messages"],
                "started_at": r["started_at"],
                "last_at": r["last_at"],
            })
        return out

    def export_markdown(self) -> None:
        """Mirror memory to a human-readable MEMORY.md next to state.db — so the
        whiteboard's `~/.tieru/MEMORY.md` box is literally real, and "your memory
        is a file you can open" is true. state.db stays the queryable source of
        truth; this file is a generated view, refreshed after each turn."""
        facts = self.conn.execute(
            "SELECT subject, content FROM facts ORDER BY subject, id"
        ).fetchall()
        eps = self.conn.execute(
            "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC, id DESC"
        ).fetchall()
        graph_relations = self.graph.store.find_relations(limit=1000)
        lines = [
            "# Tieru memory",
            "",
            ("_A human-readable mirror of what Tieru remembers. The source of truth is "
            "`state.db` (facts, episodes, and Memory Graph tables; text memory remains "
            "keyword-searchable via FTS5); "
            "this file is regenerated after every turn._"),
            "",
            f"## Facts — semantic memory ({len(facts)})",
            "",
        ]
        lines += [
            f"- **{redact_secrets(f['subject'])}** — {redact_secrets(f['content'])}"
            for f in facts
        ] or ["_none yet_"]
        lines += ["", f"## Episodes — episodic memory ({len(eps)})", ""]
        lines += [
            f"- **{e['happened_at']}** — {redact_secrets(e['summary'])}" for e in eps
        ] or ["_none yet_"]
        lines += ["", f"## Memory Graph ({len(graph_relations)} relations)", ""]
        if graph_relations:
            for relation in graph_relations:
                subject = self.graph.store.get_entity(relation.subject_id)
                obj = self.graph.store.get_entity(relation.object_id)
                validity = ""
                if relation.valid_from or relation.valid_to:
                    validity = f"; valid {relation.valid_from or '…'} to {relation.valid_to or '…'}"
                lines.append(
                    f"- **{redact_secrets(subject.canonical_name)}** —{relation.predicate}→ "
                    f"**{redact_secrets(obj.canonical_name)}** "
                    f"(status: {relation.status}; confidence: {relation.confidence:g}; "
                    f"importance: {relation.importance:g}; source: "
                    f"{redact_secrets(relation.source_type)}:{redact_secrets(relation.source_ref)}"
                    f"{validity})"
                )
        else:
            lines.append("_none yet_")
        (self.settings.home / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def maybe_consolidate(self, notify=None, recalled: str = "") -> None:
        if self.settings.memory_write_policy != "consolidate":
            return
        new_facts = consolidation.consolidate_if_due(
            self.conn,
            self.client,
            self.model,
            self.settings.consolidate_every,
            self.facts,
            self.episodes,
            recalled=recalled,
        )
        if new_facts and notify:
            notify("consolidation", {"new_facts": new_facts})


class HybridFactStore:
    """Optional semantic retrieval with deterministic local FTS fallback."""

    def __init__(self, lexical, semantic):
        self.lexical = lexical
        self.semantic = semantic

    def add(self, subject, content, source="user"):
        row_id = self.lexical.add(subject, content, source)
        try:
            self.semantic.add(subject, content, source)
        except Exception:
            pass
        return row_id

    def search(self, query, top_k=4):
        lexical = self.lexical.search(query, top_k)
        try:
            semantic = self.semantic.search(query, top_k)
        except Exception:
            semantic = []
        return list(dict.fromkeys(semantic + lexical))[:top_k]

    def __getattr__(self, name):
        return getattr(self.lexical, name)

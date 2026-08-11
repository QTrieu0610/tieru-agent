"""Tools that let the agent manage its OWN memory — so it feels like a personal
assistant that learns, not a black box. Three tools:

  manage_memory  — inspect/write/archive facts, episodes, and graph relations
  update_soul    — append a durable behaviour rule to SOUL.md (its persona)
  create_skill   — write a new SKILL.md, so the agent builds its own procedures

Everything writes to the same local files the dashboard shows; nothing leaves
the machine. update_soul is append-only (the agent can't delete its own honesty
rules); a human does full rewrites in the dashboard.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from tieru.memory import bundled_skill_dirs
from tieru.memory.personal import UnsafeMemoryError, contains_secret
from tieru.memory.procedural.loader import _parse_text
from tieru.tools.registry import Tool

SOUL_MAX = 8000
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def make_manage_memory_tool(memory) -> Tool:
    facts = memory.facts
    episodes = memory.episodes

    def manage_memory(
        action: str,
        kind: str = "fact",
        id: int | str = 0,
        query: str = "",
        content: str = "",
        subject: str = "",
        predicate: str = "",
        object: str = "",
        subject_type: str = "concept",
        object_type: str = "concept",
        confidence: float | None = None,
        importance: float = 0.5,
        source_ref: str = "",
        valid_from: str = "",
        valid_to: str = "",
    ) -> str:
        action = (action or "").lower()
        if action == "remember_relation":
            try:
                relation, created = memory.graph.remember_relation(
                    subject=subject,
                    predicate=predicate,
                    object=object,
                    subject_type=subject_type,
                    object_type=object_type,
                    confidence=confidence,
                    importance=importance,
                    source_type="explicit_user_save",
                    source_ref=source_ref or "tool:manage_memory",
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            except (UnsafeMemoryError, ValueError) as exc:
                return f"Refused: {exc}"
            verb = "Saved" if created else "Already remembered"
            return f"{verb} as relation:{relation.id}."
        if action == "inspect_relation":
            try:
                return json.dumps(
                    memory.graph.inspect_relation(int(id)), ensure_ascii=False, sort_keys=True
                )
            except (KeyError, TypeError, ValueError):
                return f"No relation with id relation:{id}."
        if action == "archive_relation":
            try:
                relation = memory.graph.archive_relation(int(id))
            except (KeyError, TypeError, ValueError):
                return f"No relation with id relation:{id}."
            return f"Archived relation:{relation.id}."
        store = getattr(memory, "store", None)
        external_episode = (
            kind == "episode"
            and getattr(getattr(memory, "settings", None), "episodic_store", "sqlite") != "sqlite"
        )
        if store is not None and not external_episode:
            memory_id = str(id) if ":" in str(id) else f"{kind}:{id}"
            if action == "search":
                rows = store.search(query, top_k=8)
                return "\n".join(
                    f"{row.id} [{row.kind}] {row.content}" for row in rows
                ) or "no matching memories"
            if action == "list":
                rows = store.list(kind=kind, limit=20)
                return "\n".join(
                    f"{row.id} [{row.kind}] {row.content}" for row in rows
                ) or "no memories"
            if action == "get":
                try:
                    return json.dumps(asdict(store.get(memory_id)), ensure_ascii=False)
                except KeyError:
                    return f"No memory with id {memory_id}."
            if action == "update":
                changes = {"content": content}
                if subject:
                    changes["subject"] = subject
                try:
                    record = store.update(memory_id, **changes)
                except KeyError:
                    return f"No memory with id {memory_id}."
                return f"Updated {record.id}."
            if action == "delete":
                return (
                    f"Deleted {memory_id}." if store.delete(memory_id)
                    else f"No memory with id {memory_id}."
                )
            if action == "export":
                path = memory.settings.home / "memory-export.json"
                store.export(path)
                return f"Exported memory to {path}."
            return (
                "action must be one of: search, list, get, update, delete, export, "
                "remember_relation, inspect_relation, archive_relation"
            )
        if action == "search":
            if kind == "episode":
                rows = episodes.list(20)
                if query:
                    rows = [r for r in rows if query.lower() in r["summary"].lower()]
                return "\n".join(f"#{r['id']} ({r['happened_at']}) {r['summary']}" for r in rows[:8]) or "no episodes"
            rows = facts.search_with_ids(query, 8) if hasattr(facts, "search_with_ids") else []
            return "\n".join(f"#{r['id']} [{r['subject']}] {r['content']}" for r in rows) or "no matching facts"
        if action == "update":
            if kind != "fact":
                return "Only facts can be updated (episodes are historical)."
            ok = facts.update(int(id), content, subject or None)
            return f"Updated fact #{id}." if ok else f"No fact with id {id}."
        if action == "delete":
            if kind == "episode":
                # sqlite ids are ints; notion page ids are UUID strings — coerce by shape.
                rid = int(id) if str(id).isdigit() else str(id)
                return f"Deleted episode #{id}." if episodes.delete(rid) else f"No episode with id {id}."
            return f"Deleted fact #{id}." if facts.delete(int(id)) else f"No fact with id {id}."
        return (
            "action must be one of: search, update, delete, remember_relation, "
            "inspect_relation, archive_relation"
        )

    return Tool(
        name="manage_memory",
        description=(
            "Search, correct, or delete long-term facts and episodes, and explicitly "
            "remember, inspect, or archive typed Memory Graph relations. "
            "ALWAYS search first to get the id, then update or delete that id. "
            "Only remember a relation when the user explicitly asks to store it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search", "list", "get", "update", "delete", "export",
                        "remember_relation", "inspect_relation", "archive_relation",
                    ],
                },
                "kind": {"type": "string", "enum": ["fact", "episode"], "description": "default fact"},
                "id": {"type": ["integer", "string"],
                       "description": "row id (from a prior search); a number for sqlite, a page id string when the notion backend is active"},
                "query": {"type": "string", "description": "keywords for search"},
                "content": {"type": "string", "description": "new text for update"},
                "subject": {"type": "string", "description": "optional new subject for a fact update"},
                "predicate": {"type": "string", "description": "graph predicate, e.g. WORKS_ON"},
                "object": {"type": "string", "description": "graph relation object"},
                "subject_type": {"type": "string", "description": "graph subject entity type"},
                "object_type": {"type": "string", "description": "graph object entity type"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "source_ref": {"type": "string", "description": "optional source record reference"},
                "valid_from": {"type": "string", "description": "optional ISO 8601 start"},
                "valid_to": {"type": "string", "description": "optional ISO 8601 end"},
            },
            "required": ["action"],
        },
        fn=manage_memory,
        risk="medium",
        read_only=False,
        capabilities=("memory.read", "memory.write"),
        default_policy="confirm",
        action_field="action",
        action_policies={
            "search": "allow", "list": "allow", "get": "allow",
            "inspect_relation": "allow", "update": "confirm", "delete": "confirm",
            "export": "confirm", "remember_relation": "confirm", "archive_relation": "confirm",
        },
        action_read_only={
            "search": True, "list": True, "get": True, "inspect_relation": True,
        },
        action_capabilities={
            "search": ("memory.read",), "list": ("memory.read",), "get": ("memory.read",),
            "inspect_relation": ("memory.read",),
            "update": ("memory.write",), "delete": ("memory.write",),
            "remember_relation": ("memory.write",), "archive_relation": ("memory.write",),
            "export": ("memory.read", "filesystem.write"),
        },
        sensitive_args=("content", "subject", "object", "source_ref"),
        resource_type="memory",
        target_arg="id",
    )


def make_update_soul_tool(settings) -> Tool:
    from tieru.runtime.session import load_soul

    def update_soul(rule: str) -> str:
        rule = rule.strip().lstrip("-").strip()
        if not rule:
            return "Nothing to add."
        if contains_secret(rule):
            return "Refused: standing instructions must not contain secrets or credentials."
        path = settings.home / "SOUL.md"
        text = load_soul(settings)  # ensures the file exists
        if len(text) > SOUL_MAX:
            return "SOUL.md is at its size limit — edit it in the dashboard instead."
        if "## Learned rules" not in text:
            text = text.rstrip() + "\n\n## Learned rules\n"
        text = text.rstrip() + f"\n- {rule}\n"
        path.write_text(text, encoding="utf-8")
        return f"Noted, I'll remember to: {rule}"

    return Tool(
        name="update_soul",
        description=(
            "Save a durable rule about how you should behave for this user (their "
            "preferences and standing instructions). Appends to your persona; takes "
            "effect next turn. Use when the user tells you how they want you to act."
        ),
        input_schema={
            "type": "object",
            "properties": {"rule": {"type": "string", "description": "one behaviour rule, imperative"}},
            "required": ["rule"],
        },
        fn=update_soul,
        risk="medium",
        read_only=False,
        capabilities=("persona.write", "filesystem.write"),
        default_policy="confirm",
        operation="append_rule",
        fixed_target="SOUL.md",
        resource_type="memory",
    )


def make_create_skill_tool(settings, memory) -> Tool:
    def create_skill(name: str, description: str, body: str) -> str:
        name = (name or "").strip().lower().replace(" ", "-")
        if not _SLUG.match(name):
            return "Skill name must be a short slug like 'weekly-review' (lowercase, hyphens)."
        if contains_secret(description) or contains_secret(body):
            return "Refused: skill instructions must not contain secrets or credentials."
        dest = settings.home / "skills" / name / "SKILL.md"
        # never silently overwrite an existing skill (built-in or user)
        if dest.exists() or any((d / name / "SKILL.md").exists() for d in bundled_skill_dirs()):
            return f"A skill named '{name}' already exists — pick another name."
        text = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{body.strip()}\n"
        if _parse_text(text, dest) is None:
            return "That didn't validate — description must be present and non-trivial."
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        memory.skills.refresh()  # live this session
        return f"Created skill '{name}'. It will trigger on: {description.strip()}"

    return Tool(
        name="create_skill",
        description=(
            "Write a new reusable skill (a SKILL.md the agent loads when relevant) so you "
            "can repeat a workflow the user taught you. Only call this after the user agrees. "
            "body = step-by-step instructions; description = when to use it (include trigger words)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short slug, e.g. weekly-review"},
                "description": {"type": "string", "description": "one line: what it does and when to use it"},
                "body": {"type": "string", "description": "the step-by-step instructions (markdown)"},
            },
            "required": ["name", "description", "body"],
        },
        fn=create_skill,
        risk="high",
        read_only=False,
        capabilities=("instructions.write", "filesystem.write"),
        default_policy="confirm",
        operation="create_skill",
        target_arg="name",
        resource_type="skill",
    )

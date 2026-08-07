"""save_note — writes a durable fact into semantic memory, on request.

This is the default explicit memory path ("remember that Alex prefers mornings").
Optional consolidation is available only when ``memory_write_policy`` is set to
``consolidate``; the default never distills every conversation automatically.
"""

from __future__ import annotations

import sqlite3

from tieru.tools.registry import Tool


def make_tool(store: sqlite3.Connection | object) -> Tool:
    def save_note(subject: str, content: str) -> str:
        if hasattr(store, "add"):
            record, created = store.add(
                content=content,
                kind="fact",
                source="user",
                provenance="explicit user request via save_note",
                subject=subject,
                trusted=True,
            )
            verb = "Saved" if created else "Already remembered"
            return f"{verb} as {record.id} under '{subject}': {content}"
        store.execute(
            "INSERT INTO facts (subject, content, source) VALUES (?,?,'user')",  # type: ignore[attr-defined]
            (subject.lower().strip(), content),
        )
        store.commit()  # type: ignore[attr-defined]
        return f"Saved to memory under '{subject}': {content}"

    return Tool(
        name="save_note",
        description=(
            "Save a durable fact to long-term memory. Use when the user tells you something "
            "worth remembering about themselves, a person, or a project — especially if they "
            "say 'remember' or share a preference."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Who/what this is about, e.g. 'alex' or 'acme-project'"},
                "content": {"type": "string", "description": "The fact, one sentence"},
            },
            "required": ["subject", "content"],
        },
        fn=save_note,
        risk="medium",
        read_only=False,
        capabilities=("memory.write",),
        default_policy="confirm",
    )

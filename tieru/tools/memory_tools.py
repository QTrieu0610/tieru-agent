"""Structured direct-agent tools over Tieru's existing personal memory store."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict
from typing import Any

from tieru.memory.personal import MemoryCapacityError, UnsafeMemoryError, contains_secret
from tieru.tools.registry import Tool

_MAX_TOP_K = 8
_MAX_OUTPUT_BYTES = 12_000
_MAX_CONTENT_CHARS = 2_000


def _plain(text: str) -> str:
    folded = (text or "").casefold().replace("'", "").replace("’", "")
    normalized = unicodedata.normalize("NFKD", folded)
    return " ".join(
        re.findall(r"[a-z0-9]+", normalized.encode("ascii", "ignore").decode("ascii"))
    )


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, _plain(text)) is not None


def _negated(request: str, verbs: str) -> bool:
    return _has(
        rf"\b(?:do not|dont|never|not|no|without|khong|dung)(?:\s+[a-z0-9]+){{0,2}}\s+(?:{verbs})\b",
        request,
    )


def _remember_intent(request: str) -> bool:
    text = _plain(request)
    verbs = r"remember|memorize|save|store|ghi nho|luu|hay nho|nho giup"
    if _negated(request, verbs):
        return False
    if re.search(r"\b(?:do|did) you remember\b|\bi (?:remember|recall)\b|\btoi nho\b", text):
        return False
    return re.search(rf"\b(?:{verbs})\b", text) is not None


def _update_intent(request: str) -> bool:
    verbs = r"update|change|correct|replace|edit|revise|cap nhat|thay doi|dinh chinh|sua"
    return not _negated(request, verbs) and _has(rf"\b(?:{verbs})\b", request)


def _forget_intent(request: str) -> bool:
    verbs = r"forget|delete|remove|erase|xoa|quen|bo nho"
    return not _negated(request, verbs) and _has(rf"\b(?:{verbs})\b", request)


def _search_intent(request: str) -> bool:
    text = _plain(request)
    if re.search(
        r"\b(?:memory|remember|recall|previously|earlier|told you|you know|"
        r"da noi|truoc day|ghi nho|bo nho)\b",
        text,
    ):
        return True
    if _update_intent(request) or _forget_intent(request) or _remember_intent(request):
        return True
    personal = re.search(r"\b(?:my|mine|our|user|project|cua toi|cua chung ta)\b", text)
    question = "?" in request or re.search(
        r"\b(?:what|which|who|where|when|how|do i|did i|la gi|nao|ai|o dau|khi nao)\b",
        text,
    )
    return bool(personal and question)


def memory_intent_check(action: str):
    checks = {
        "search": _search_intent,
        "remember": _remember_intent,
        "update": _update_intent,
        "forget": _forget_intent,
    }

    def check(request: str, _args: dict[str, Any]) -> tuple[str, str] | None:
        if checks[action](request):
            return None
        if action == "search":
            return (
                "memory_query_not_relevant",
                "memory_search is allowed only when the request clearly needs known user or project information",
            )
        return (
            "memory_intent_required",
            f"memory_{action} requires an explicit user request to {action} memory",
        )

    return check


def _error(code: str, message: str, *, memory_id: str = "") -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": False},
    }
    if memory_id:
        payload["memory_id"] = memory_id
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _record(record) -> dict[str, Any]:
    payload = asdict(record)
    payload["content"] = payload["content"][:_MAX_CONTENT_CHARS]
    return payload


def _bounded(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return text
    memories = payload.get("memories")
    if isinstance(memories, list):
        bounded = dict(payload)
        bounded_rows = list(memories)
        bounded["truncated"] = True
        while bounded_rows:
            bounded["memories"] = bounded_rows
            bounded["returned_count"] = len(bounded_rows)
            text = json.dumps(bounded, ensure_ascii=False, sort_keys=True)
            if len(text.encode("utf-8")) <= _MAX_OUTPUT_BYTES:
                return text
            bounded_rows.pop()
    return _error("memory_output_too_large", "memory output exceeded its safety bound")


def make_tools(memory) -> list[Tool]:
    """Return four bounded tools backed by ``PersonalMemoryStore``."""
    store = memory.store

    def memory_search(query: str, top_k: int = 4) -> str:
        query = query.strip()
        if not query:
            return _error("invalid_memory_query", "memory query must not be empty")
        rows = store.search(
            query,
            top_k=max(1, min(int(top_k), _MAX_TOP_K)),
            trusted_only=True,
        )
        return _bounded(
            {
                "ok": True,
                "query": query,
                "count": len(rows),
                "memories": [_record(row) for row in rows],
            }
        )

    def memory_remember(
        content: str,
        subject: str = "user",
        kind: str = "fact",
        importance: float = 0.5,
        happened_at: str = "",
    ) -> str:
        try:
            if contains_secret(subject):
                raise UnsafeMemoryError("refusing to store a subject that looks like a secret")
            record, created = store.add(
                content=content,
                kind=kind,
                source="user",
                provenance="explicit user request via memory_remember",
                importance=importance,
                trusted=True,
                subject=subject,
                happened_at=happened_at,
            )
        except (MemoryCapacityError, UnsafeMemoryError, ValueError) as exc:
            return _error("memory_write_refused", str(exc))
        return _bounded(
            {
                "ok": True,
                "status": "saved" if created else "already_exists",
                "created": created,
                "memory": _record(record),
            }
        )

    def memory_update(memory_id: str, content: str, subject: str = "") -> str:
        try:
            current = store.get(memory_id)
            if contains_secret(subject):
                raise UnsafeMemoryError("refusing to store a subject that looks like a secret")
            provenance = current.provenance.strip()
            audit = "explicit user update via memory_update"
            if audit not in provenance:
                provenance = f"{provenance}; {audit}" if provenance else audit
            changes: dict[str, Any] = {"content": content, "provenance": provenance}
            if subject:
                changes["subject"] = subject
            record = store.update(memory_id, **changes)
        except KeyError:
            return _error("memory_not_found", "memory id was not found", memory_id=memory_id)
        except (UnsafeMemoryError, ValueError) as exc:
            return _error("memory_update_refused", str(exc), memory_id=memory_id)
        return _bounded({"ok": True, "status": "updated", "memory": _record(record)})

    def memory_forget(memory_id: str) -> str:
        try:
            store.get(memory_id)
        except (KeyError, ValueError):
            return _error("memory_not_found", "memory id was not found", memory_id=memory_id)
        deleted = store.delete(memory_id)
        return _bounded(
            {"ok": deleted, "status": "forgotten" if deleted else "not_found", "memory_id": memory_id}
        )

    return [
        Tool(
            name="memory_search",
            description=(
                "Search Tieru's existing long-term memory only when the request clearly asks "
                "for known user, preference, prior-conversation, or project information. "
                "Never use for unrelated general knowledge. Returns bounded records with exact ids and provenance."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": _MAX_TOP_K},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            fn=memory_search,
            risk="low",
            read_only=True,
            capabilities=("memory.read",),
            default_policy="allow",
            operation="search",
            fixed_target="personal memory",
            resource_type="memory",
            sensitive_args=("query",),
            intent_check=memory_intent_check("search"),
        ),
        Tool(
            name="memory_remember",
            description=(
                "Save one fact or episode only after an explicit user save request such as "
                "remember/save/store/nhớ/lưu. Never infer save intent from a normal statement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "subject": {"type": "string", "minLength": 1, "maxLength": 200},
                    "kind": {"type": "string", "enum": ["fact", "episode"]},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                    "happened_at": {"type": "string", "maxLength": 64},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            fn=memory_remember,
            risk="medium",
            read_only=False,
            capabilities=("memory.write",),
            default_policy="confirm",
            operation="save",
            target_arg="subject",
            resource_type="memory",
            sensitive_args=("content", "subject"),
            intent_check=memory_intent_check("remember"),
            # PersonalMemoryStore already performs transactional semantic
            # deduplication and its response distinguishes saved/already_exists.
            idempotency_guard=False,
        ),
        Tool(
            name="memory_update",
            description=(
                "Update the exact memory_id from memory_search only when the user explicitly "
                "asks to update/change/correct it. Keeps the same id and provenance audit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 80,
                        "description": "complete typed id from memory_search, e.g. fact:12; never use 12 alone",
                    },
                    "content": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "subject": {"type": "string", "maxLength": 200},
                },
                "required": ["memory_id", "content"],
                "additionalProperties": False,
            },
            fn=memory_update,
            risk="medium",
            read_only=False,
            capabilities=("memory.write",),
            default_policy="confirm",
            operation="update",
            target_arg="memory_id",
            resource_type="memory",
            sensitive_args=("content", "subject"),
            intent_check=memory_intent_check("update"),
        ),
        Tool(
            name="memory_forget",
            description=(
                "Destructively delete the exact memory_id from memory_search only when the "
                "user explicitly asks to forget/delete it. Never delete memory autonomously."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 80,
                        "description": "complete typed id from memory_search, e.g. fact:12; never use 12 alone",
                    }
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
            fn=memory_forget,
            risk="high",
            read_only=False,
            capabilities=("memory.write", "destructive"),
            default_policy="confirm",
            operation="delete",
            target_arg="memory_id",
            resource_type="memory",
            reversible=False,
            intent_check=memory_intent_check("forget"),
        ),
    ]

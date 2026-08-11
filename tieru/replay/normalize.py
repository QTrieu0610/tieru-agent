"""Normalize existing observer dictionaries without coupling emitters to Replay."""

from __future__ import annotations

import json
import re
from typing import Any

from tieru.memory.personal import redact_secrets
from tieru.replay.models import NormalizedEvent

_SENSITIVE_KEY = re.compile(
    r"(^|[-_])(api[-_]?key|authorization|cookie|credential|password|secret|token)($|[-_])",
    re.IGNORECASE,
)
_AUTH_VALUE = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\s,;]+(?:\s+[^\s,;]+)?",
    re.IGNORECASE,
)


def _redact_text(value: str) -> str:
    return _AUTH_VALUE.sub(lambda match: f"{match.group(1)}: [REDACTED]", redact_secrets(value))


def _safe(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def bounded_text(value: Any, limit_bytes: int) -> tuple[str, int, bool]:
    text = _redact_text(str(value or ""))
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text, len(encoded), False
    preview = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    return preview, len(encoded), True


def _bounded_payload(payload: dict[str, Any], limit_bytes: int) -> dict[str, Any]:
    safe = _safe(payload)
    raw = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":"))
    size = len(raw.encode("utf-8"))
    if size <= limit_bytes:
        return safe
    preview, _, _ = bounded_text(raw, max(32, limit_bytes - 96))
    return {"preview": preview, "payload_size": size, "truncated": True}


class ReplayNormalizer:
    """Translate meaningful raw event shapes into a stable Replay vocabulary."""

    def __init__(self, max_payload_bytes: int = 8192, max_tool_output_bytes: int = 2048):
        self.max_payload_bytes = max_payload_bytes
        self.max_tool_output_bytes = max_tool_output_bytes

    def normalize(self, kind: str, event: dict[str, Any]) -> NormalizedEvent | None:
        raw = dict(event or {})
        common = {
            "node": str(raw.get("node") or ""),
            "tool": str(raw.get("tool") or ""),
            "role": str(raw.get("role") or ""),
            "model": str(raw.get("model") or ""),
            "provider": str(raw.get("provider") or ""),
            "duration_ms": self._duration(raw),
        }

        category, event_type = self._kind(kind, raw)
        if not category:
            return None

        excluded = {
            "run_id", "node", "tool", "role", "model", "provider", "duration_ms", "ms"
        }
        payload = {key: value for key, value in raw.items() if key not in excluded}
        if category == "memory":
            omitted = any(
                payload.pop(key, None) is not None
                for key in ("content", "text", "context", "memory", "query")
            )
            if omitted:
                payload["content_omitted"] = True
        if category == "model":
            for key in ("reasoning", "thinking", "scratchpad", "content", "messages", "system"):
                payload.pop(key, None)
        if category == "trust":
            payload.pop("args", None)
        if category == "tool" and "output" in payload:
            preview, size, truncated = bounded_text(
                payload.pop("output"), self.max_tool_output_bytes
            )
            payload.update(
                {"output_preview": preview, "output_size": size, "truncated": truncated}
            )
        elif category == "tool" and "output_preview" in payload:
            preview, preview_size, shortened = bounded_text(
                payload["output_preview"], self.max_tool_output_bytes
            )
            payload["output_preview"] = preview
            payload["output_size"] = max(int(payload.get("output_size") or 0), preview_size)
            payload["truncated"] = bool(payload.get("truncated")) or shortened
        if category == "output" and "output" in payload:
            _preview, size, truncated = bounded_text(
                payload.pop("output"), self.max_tool_output_bytes
            )
            payload.update({"output_size": size, "truncated": truncated})
        payload = _bounded_payload(payload, self.max_payload_bytes)
        return NormalizedEvent(category, event_type, payload, **common)

    @staticmethod
    def _duration(raw: dict[str, Any]) -> int | None:
        value = raw.get("duration_ms", raw.get("ms"))
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _kind(kind: str, raw: dict[str, Any]) -> tuple[str, str]:
        if kind in {"run_started", "run_completed", "run_failed"}:
            return "lifecycle", kind
        if kind in {"gate", "memory_gate"}:
            return "memory", "memory_gate"
        if kind in {"memory_retrieval", "graph_lookup", "consolidation"}:
            return "memory", kind
        if kind == "model_call_started":
            return "model", kind
        if kind == "llm":
            return "model", "model_call_completed"
        if kind == "model_call_completed":
            return "model", kind
        if kind in {"route", "triage"}:
            return "routing", "graph_route"
        if kind in {
            "fabric_analysis", "fabric_route", "fabric_candidates", "fabric_filter",
            "fabric_score", "fabric_selection", "fabric_fallback", "fabric_verification"
        }:
            return "fabric", kind
        if kind == "graph_end" and raw.get("error"):
            return "error", "graph_failed"
        if kind == "node_end" and raw.get("error"):
            return "error", "graph_node_failed"
        if kind in {"graph_start", "graph_end", "node_start", "node_end"}:
            return "graph", kind
        if kind.startswith("trust_"):
            return "trust", kind
        if kind in {
            "tool_requested",
            "tool_started",
            "tool_completed",
            "tool_failed",
            "tool_denied",
        }:
            return "tool", kind
        if kind == "tool":
            output = str(raw.get("output") or "").lower()
            if "tool_permission_denied" in output:
                return "tool", "tool_denied"
            if output.startswith("error") or "timed out" in output:
                return "tool", "tool_failed"
            return "tool", "tool_completed"
        if kind in {"error", "exception", "timeout"}:
            return "error", kind
        if kind == "final_output":
            return "output", kind
        return "", ""

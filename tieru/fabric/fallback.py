"""Conservative classification of hard failures eligible for fallback."""

from __future__ import annotations


def fallback_reason(exc: Exception) -> str | None:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "rate" in name and "limit" in name or "rate limit" in text or "429" in text:
        return "rate_limited"
    if "timeout" in name or "timed out" in text:
        return "provider_timeout"
    if "connection" in name or "connect" in text or "endpoint" in text:
        return "connection_failure"
    if "notfound" in name or "model not found" in text or "model_not_found" in text:
        return "model_not_found"
    if "unsupported" in text and ("tool" in text or "function" in text):
        return "tool_calling_unsupported"
    return None

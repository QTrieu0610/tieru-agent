"""Bounds, redaction, and metadata filtering for context records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

_SENSITIVE = re.compile(
    r"(^|[-_])(api[-_]?key|authorization|cookie|credential|env|password|secret|token)($|[-_])",
    re.IGNORECASE,
)
_SOURCE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+", re.IGNORECASE
    ),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def redact_context_secrets(text: str) -> str:
    """Redact credential-shaped values without importing a runtime subsystem."""
    if not text:
        return text
    if _SECRET_PATTERNS[0].search(text):
        return "[REDACTED SECRET]"
    for pattern in _SECRET_PATTERNS[1:]:
        text = pattern.sub("[REDACTED SECRET]", text)
    return text


def bounded_text(value: object, limit_bytes: int) -> tuple[str, int, bool]:
    safe = redact_context_secrets(str(value or ""))
    encoded = safe.encode("utf-8")
    original_size = len(encoded)
    if original_size <= limit_bytes:
        return safe, original_size, False
    marker = "\n[CONTEXT DATA TRUNCATED]"
    budget = max(0, int(limit_bytes) - len(marker.encode("utf-8")))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker, original_size, True


def safe_source(value: str) -> str:
    source = str(value or "data").strip().lower()
    return source if _SOURCE.fullmatch(source) else "data"


def safe_metadata(value: Mapping[str, object] | None) -> MappingProxyType:
    output: dict[str, str] = {}
    for raw_key, raw_value in list((value or {}).items())[:16]:
        key = str(raw_key).strip().lower()
        if not _SOURCE.fullmatch(key) or _SENSITIVE.search(key):
            continue
        safe, _size, _truncated = bounded_text(raw_value, 256)
        output[key] = safe
    return MappingProxyType(output)

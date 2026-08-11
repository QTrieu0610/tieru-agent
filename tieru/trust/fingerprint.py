"""Stable action identity without secret material."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from tieru.memory.personal import contains_secret, redact_secrets
from tieru.trust.models import ActionRequest

_SENSITIVE_KEY = re.compile(
    r"(^|_)(api_?key|authorization|cookie|credential|password|secret|token)($|_)",
    re.IGNORECASE,
)


def safe_value(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item) for item in value]
    if isinstance(value, str):
        if contains_secret(value):
            return "[REDACTED]"
        return redact_secrets(value)
    return value


def action_fingerprint(action: ActionRequest) -> str:
    """Hash only normalized, redacted identity fields—not arbitrary arguments."""
    material = {
        "tool": action.tool_name,
        "capabilities": sorted(item.value for item in action.capabilities),
        "operation": action.operation,
        "target": safe_value(action.target, "target"),
        "scope": safe_value(action.scope, "scope"),
        "resource_type": action.resource_type,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

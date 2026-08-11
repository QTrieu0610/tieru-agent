"""Conservative parameter generalization for observable tool arguments."""

from __future__ import annotations

import copy
import re
from typing import Any

from tieru.forge.models import WorkflowCandidate
from tieru.memory.personal import redact_secrets

_VARIABLE_KEYS = {
    "path": "file_path", "file": "file_path", "filename": "file_path",
    "cwd": "workspace_path", "directory": "workspace_path", "workspace": "workspace_path",
    "repo": "repository", "repository": "repository", "branch": "branch_name",
    "query": "query", "url": "target_url", "target": "target",
}


def _name(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    for token, name in _VARIABLE_KEYS.items():
        if normalized == token or normalized.endswith("_" + token):
            return name
    return None


def generalize(candidate: WorkflowCandidate) -> WorkflowCandidate:
    """Generalize only known parameter fields; tools, operations, commands and policy stay fixed."""
    result = copy.deepcopy(candidate)
    inputs: dict[str, dict[str, Any]] = {}
    for step in result.steps:
        safe_args: dict[str, Any] = {}
        for key, value in step.arguments.items():
            variable = _name(str(key))
            if variable and isinstance(value, str) and value.strip():
                inputs.setdefault(variable, {
                    "name": variable,
                    "type": "string",
                    "required": True,
                    "source_fields": [],
                })
                source = f"{step.step_id}.{key}"
                if source not in inputs[variable]["source_fields"]:
                    inputs[variable]["source_fields"].append(source)
                safe_args[str(key)] = "{{" + variable + "}}"
            else:
                safe_args[str(key)] = _redact(value)
        step.arguments = safe_args
    result.inputs = list(inputs.values())
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)[:1000]
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:50]]
    return value

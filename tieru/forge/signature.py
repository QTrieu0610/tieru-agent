"""Secret-safe structural workflow signatures."""

from __future__ import annotations

import hashlib
import json

from tieru.forge.models import WorkflowCandidate


def workflow_signature(candidate: WorkflowCandidate) -> str:
    """Hash structure only; argument values and private text never participate."""
    shape = [
        {
            "operation": step.operation,
            "tool": step.tool,
            "status": step.status,
            "capabilities": sorted(step.capabilities),
            "verification": step.verification,
        }
        for step in candidate.steps
    ]
    raw = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compatible(left: WorkflowCandidate, right: WorkflowCandidate) -> bool:
    return workflow_signature(left) == workflow_signature(right)

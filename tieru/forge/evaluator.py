"""Side-effect-free behavioral consistency evaluation for Forge drafts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tieru.forge.models import SkillDraft


class DraftEvaluator:
    def __init__(self, judge: Callable[[SkillDraft], dict[str, Any]] | None = None):
        self.judge = judge

    def evaluate(self, draft: SkillDraft) -> dict:
        errors: list[str] = []
        low = draft.content.lower()
        for step in draft.workflow.steps:
            if step.tool and f"`{step.tool.lower()}`" not in low and step.tool.lower() not in low:
                errors.append(f"source tool is not represented: {step.tool}")
            if (step.status == "denied"
                    and ("denied" not in low
                         or not any(word in low for word in ("do not", "never", "stop")))):
                errors.append(f"denied operation is not preserved as denied: {step.tool}")
            if step.verification and step.status == "completed" and "## verification" not in low:
                errors.append(f"verification step is missing: {step.tool}")
        for capability in draft.workflow.capabilities:
            if capability.lower() not in low:
                errors.append(f"required capability is not represented: {capability}")
        if "trust kernel" not in low or "not granted permissions" not in low:
            errors.append("Trust boundary is not retained")

        judge_result: dict[str, Any] = {"status": "skipped", "reason": "judge not configured"}
        if self.judge is not None:
            try:
                judge_result = dict(self.judge(draft))
                judge_result.setdefault("status", "completed")
            except Exception as exc:
                judge_result = {"status": "skipped", "reason": f"judge unavailable: {type(exc).__name__}"}
        return {
            "deterministic_pass": not errors,
            "errors": list(dict.fromkeys(errors)),
            "judge": judge_result,
            "side_effect_execution": False,
        }

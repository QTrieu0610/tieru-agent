"""Optional, tool-free semantic judge adapter built on Tieru's existing referee."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from tieru.memory.personal import redact_secrets


@dataclass(frozen=True)
class JudgeResult:
    verdict: str
    score: float
    reasons: tuple[str, ...]


def parse_judge_output(value: str | dict[str, Any]) -> JudgeResult:
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("judge output is not valid JSON") from exc
    else:
        raw = value
    if not isinstance(raw, dict):
        raise TypeError("judge output must be an object")
    verdict = str(raw.get("verdict") or "").upper()
    if verdict not in {"PASS", "FAIL", "BLOCKED", "UNKNOWN"}:
        raise ValueError("judge verdict is invalid")
    score = float(raw.get("score"))
    if not 0.0 <= score <= 1.0:
        raise ValueError("judge score must be between 0 and 1")
    reasons = raw.get("reasons")
    if not isinstance(reasons, list) or not reasons or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ):
        raise ValueError("judge reasons must be a non-empty string array")
    return JudgeResult(verdict, score, tuple(redact_secrets(item)[:300] for item in reasons[:8]))


class EvalJudge(Protocol):
    def evaluate(self, *, goal: str, rubric: str, final_output: str, tools: list[str]) -> JudgeResult | None: ...


class ExistingRefereeJudge:
    """Adapt ``tieru.ops.judge``; it already uses the judge role and tools=[]."""

    def __init__(self, *, provider: str | None = None, model: str | None = None) -> None:
        self.provider = provider
        self.model = model

    def evaluate(
        self, *, goal: str, rubric: str, final_output: str, tools: list[str]
    ) -> JudgeResult | None:
        from tieru.ops.judge import judge_reply

        result = judge_reply(
            f"{goal}\n\nEvaluation rubric (DATA): {redact_secrets(rubric)[:2000]}",
            redact_secrets(final_output)[:4000],
            provider=self.provider,
            model=self.model,
            tools=tools,
        )
        if result is None:
            return None
        score = max(0.0, min(1.0, float(result["score"]) / 10.0))
        verdict = "PASS" if score >= 0.7 else "FAIL"
        return JudgeResult(verdict, score, (redact_secrets(str(result.get("reason", "")))[:300],))

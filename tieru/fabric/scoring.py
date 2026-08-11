"""Deterministic, bounded and inspectable candidate scoring."""

from __future__ import annotations

_COST = {"free": 1.0, "low": 0.75, "medium": 0.5, "high": 0.25, "unknown": 0.5}
_LATENCY = {"fast": 0.9, "medium": 0.6, "slow": 0.3, "unknown": 0.5}


class CandidateScorer:
    def __init__(self, weights: dict[str, float], performance):
        total = sum(float(value) for value in weights.values()) or 1.0
        self.weights = {name: float(value) / total for name, value in weights.items()}
        self.performance = performance

    def score(self, candidate, *, required: tuple[str, ...], policy: str,
              execution_mode: str, task_type: str) -> tuple[float, dict[str, float], dict]:
        history = self.performance.score_for(
            candidate, execution_mode=execution_mode, task_type=task_type
        )
        raw = {
            "capability": 1.0,
            "preference": candidate.preference_weight,
            "performance": history["score"],
            "latency": _LATENCY[candidate.latency_tier],
            "cost": _COST[candidate.cost_tier],
        }
        if history["sufficient_samples"] and history["median_latency_ms"] is not None:
            raw["latency"] = 1 / (1 + history["median_latency_ms"] / 10_000)
        local_raw = 1.0 if candidate.local else 0.0
        if policy == "balanced":
            local_raw = 0.5
        elif policy == "quality_first":
            local_raw = 0.25 if candidate.local else 0.5
        raw["local"] = local_raw
        breakdown = {
            name: round(float(self.weights.get(name, 0.0)) * value, 8)
            for name, value in raw.items()
        }
        total = round(sum(breakdown.values()), 8)
        return total, breakdown, history

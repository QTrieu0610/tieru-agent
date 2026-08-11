"""Privacy-preserving performance aggregates backed by Replay metadata."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, median
from typing import Any


class ModelPerformanceService:
    def __init__(self, replay_service=None, *, min_samples: int = 5):
        self.replay = replay_service
        self.min_samples = max(0, int(min_samples))

    def score_for(self, candidate, *, execution_mode: str, task_type: str) -> dict[str, Any]:
        rows = self._observations()
        matched = [row for row in rows if row["provider"] == candidate.provider
                   and row["model"] == candidate.model
                   and (not row["execution_mode"] or row["execution_mode"] == execution_mode)
                   and (not row["task_type"] or row["task_type"] == task_type)]
        return self._aggregate(matched)

    def stats(self) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
        for row in self._observations():
            key = (row["provider"], row["model"], row["execution_mode"], row["task_type"])
            groups[key].append(row)
        result = []
        for key in sorted(groups):
            aggregate = self._aggregate(groups[key])
            result.append({
                "provider": key[0], "model": key[1], "execution_mode": key[2],
                "task_type": key[3], **aggregate,
            })
        return result

    def _observations(self) -> list[dict[str, Any]]:
        if self.replay is None:
            return []
        observations: list[dict[str, Any]] = []
        try:
            runs = self.replay.list_runs(limit=500)
        except Exception:  # Replay failure never changes routing safety
            return []
        for run in runs:
            task_type = ""
            execution_mode = ""
            tool_success = None
            try:
                events = self.replay.get_events(run["run_id"])
            except Exception:
                events = []
            successful_tools = 0
            failed_tools = 0
            for event in events:
                payload = event.get("safe_payload") or {}
                if event.get("event_type") in {"fabric_route", "fabric_selection"}:
                    task_type = str(payload.get("task_type") or task_type)
                    execution_mode = str(payload.get("execution_mode") or execution_mode)
                if event.get("event_type") == "tool_completed":
                    successful_tools += 1
                elif event.get("event_type") in {"tool_failed", "tool_denied"}:
                    failed_tools += 1
            if successful_tools + failed_tools:
                tool_success = successful_tools / (successful_tools + failed_tools)
            observations.append({
                "provider": str(run.get("provider") or ""),
                "model": str(run.get("model") or ""),
                "execution_mode": execution_mode,
                "task_type": task_type,
                "success": run.get("status") == "completed",
                "latency_ms": run.get("latency_ms"),
                "tool_success": tool_success,
            })
        return observations

    def _aggregate(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(rows)
        latencies = [max(0, int(row["latency_ms"])) for row in rows
                     if row.get("latency_ms") is not None]
        successes = sum(bool(row["success"]) for row in rows)
        raw_success = successes / count if count else None
        sufficient = count >= self.min_samples
        if not sufficient:
            score = 0.5
        else:
            # A small fixed prior keeps any one outcome from dominating.
            smoothed_success = (successes + 2) / (count + 4)
            latency_score = (
                1 / (1 + (median(latencies) / 10_000)) if latencies else 0.5
            )
            tool_values = [float(row["tool_success"]) for row in rows
                           if row.get("tool_success") is not None]
            tool_score = mean(tool_values) if tool_values else 0.5
            score = 0.65 * smoothed_success + 0.25 * latency_score + 0.10 * tool_score
        return {
            "sample_count": count,
            "sufficient_samples": sufficient,
            "success_rate": round(raw_success, 4) if raw_success is not None else None,
            "average_latency_ms": round(mean(latencies), 2) if latencies else None,
            "median_latency_ms": round(median(latencies), 2) if latencies else None,
            "score": round(max(0.0, min(1.0, score)), 6),
        }

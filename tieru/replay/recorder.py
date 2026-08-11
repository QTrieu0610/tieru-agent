"""Failure-isolated observer adapter for Replay."""

from __future__ import annotations

from collections import Counter
from typing import Any

from tieru.replay.service import ReplayService


class ReplayRecorder:
    """Replay may degrade, but it must never change execution or authorization."""

    def __init__(self, service: ReplayService):
        self.service = service
        self.run_id = ""
        self.errors: list[str] = []
        self._terminal_tools: Counter[str] = Counter()

    @property
    def degraded(self) -> bool:
        return bool(self.errors)

    def start(self, **fields) -> str:
        self.run_id = str(fields.get("run_id") or "")
        try:
            self.run_id = self.service.start_run(**fields).run_id
        except Exception as exc:  # observability must not take down a safe turn
            self.errors.append(f"start:{type(exc).__name__}")
        return self.run_id

    def event(self, kind: str, event: dict[str, Any]) -> None:
        if not self.run_id:
            return
        tool = str((event or {}).get("tool") or "")
        if kind in {"tool_completed", "tool_failed", "tool_denied"}:
            self._terminal_tools[tool] += 1
        elif kind == "tool" and self._terminal_tools[tool]:
            self._terminal_tools[tool] -= 1
            return
        try:
            if kind == "gate" and event.get("reason") == "exact Memory Graph match":
                self.service.record_event(
                    self.run_id,
                    "graph_lookup",
                    {"source": "memory_graph", "decision": "retrieve"},
                )
            self.service.record_event(self.run_id, kind, event)
        except Exception as exc:
            self.errors.append(f"event:{type(exc).__name__}")

    def complete(self, **fields) -> None:
        if not self.run_id:
            return
        try:
            self.service.complete_run(self.run_id, **fields)
        except Exception as exc:
            self.errors.append(f"complete:{type(exc).__name__}")

    def fail(self, **fields) -> None:
        if not self.run_id:
            return
        try:
            self.service.fail_run(self.run_id, **fields)
        except Exception as exc:
            self.errors.append(f"fail:{type(exc).__name__}")

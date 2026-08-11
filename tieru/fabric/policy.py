"""Deterministic policy translating task shape into one sticky mode."""

from __future__ import annotations

from tieru.fabric.models import ExecutionMode, TaskProfile


class FabricPolicy:
    def __init__(self, default_mode: str = "standard"):
        self.default_mode = ExecutionMode(default_mode)

    def select(self, task: TaskProfile) -> tuple[ExecutionMode, tuple[str, ...]]:
        reasons = list(task.reason_codes)
        if task.requires_deep_context or task.requires_verification:
            return ExecutionMode.DEEP, tuple(reasons or ["deep_context_required"])
        if task.requires_tools:
            return ExecutionMode.AGENT, tuple(reasons or ["tools_required"])
        if task.task_type == "greeting" and task.complexity == "low":
            return ExecutionMode.QUICK, tuple(reasons or ["lightweight_conversation"])
        if task.task_type == "unknown":
            return ExecutionMode.STANDARD, tuple(reasons or ["safe_default"])
        known = {"chat", "lookup", "summarization", "coding", "analysis", "planning",
                 "tool_task", "greeting"}
        if task.task_type not in known:
            return self.default_mode, tuple(reasons or ["configured_default"])
        return ExecutionMode.STANDARD, tuple(reasons or ["ordinary_request"])

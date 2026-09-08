"""Deterministic benchmark evaluation for capability routing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from tieru.capabilities.models import CapabilityRouterConfig
from tieru.capabilities.router import CapabilityRouter
from tieru.config import Settings
from tieru.context import ContextTrust
from tieru.tools import build_registry
from tieru.tools.registry import ToolRegistry


def evaluate_routing_fixture(
    fixture_path: Path, registry: ToolRegistry | None = None
) -> dict[str, Any]:
    """Run deterministic benchmark evaluation against capability routing cases."""
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    cases = raw.get("cases", [])
    if not cases:
        return {
            "total_cases": 0,
            "required_tool_recall": 1.0,
            "forbidden_tool_exclusion": 1.0,
            "no_tool_accuracy": 1.0,
            "average_visible_tools": 0.0,
            "tool_schema_reduction_rate": 0.0,
        }

    if registry is None:
        conn = sqlite3.connect(":memory:")
        settings = Settings()
        registry = build_registry(conn, settings)

    router = CapabilityRouter(CapabilityRouterConfig(max_visible_tools=8, min_score=0.30, top_k=4))

    total_required = 0
    recalled_required = 0
    total_forbidden = 0
    excluded_forbidden = 0
    no_tool_cases = 0
    no_tool_successes = 0
    visible_counts: list[int] = []
    reductions: list[float] = []

    for case in cases:
        query = str(case["query"])
        required_tools = set(case.get("required_tools", []))
        forbidden_tools = set(case.get("forbidden_tools", []))
        category = str(case.get("category", ""))

        # Context Firewall check for adversarial DATA category
        context_trust = (
            ContextTrust.DATA
            if category == "adversarial"
            else ContextTrust.USER
        )

        result = router.route(query, registry, context_trust=context_trust)
        visible_set = set(result.selected_tools)
        visible_counts.append(len(visible_set))

        if result.candidate_tool_count > 0:
            reduction = 1.0 - (len(visible_set) / result.candidate_tool_count)
            reductions.append(reduction)

        if required_tools:
            total_required += len(required_tools)
            recalled_required += len(required_tools & visible_set)

        if forbidden_tools:
            total_forbidden += len(forbidden_tools)
            excluded_forbidden += len(forbidden_tools - visible_set)

        if category == "no-tool":
            no_tool_cases += 1
            # In no-tool queries, only mandatory tools (if any) or 0 tools should be visible
            if len(visible_set) == 0:
                no_tool_successes += 1

    recall = (recalled_required / total_required) if total_required > 0 else 1.0
    exclusion = (excluded_forbidden / total_forbidden) if total_forbidden > 0 else 1.0
    no_tool_acc = (no_tool_successes / no_tool_cases) if no_tool_cases > 0 else 1.0
    avg_visible = (sum(visible_counts) / len(visible_counts)) if visible_counts else 0.0
    avg_reduction = (sum(reductions) / len(reductions)) if reductions else 0.0

    return {
        "total_cases": len(cases),
        "required_tool_recall": round(recall, 4),
        "forbidden_tool_exclusion": round(exclusion, 4),
        "no_tool_accuracy": round(no_tool_acc, 4),
        "average_visible_tools": round(avg_visible, 2),
        "tool_schema_reduction_rate": round(avg_reduction, 4),
    }

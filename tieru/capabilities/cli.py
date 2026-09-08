"""CLI entry point for capability inspection and routing."""

from __future__ import annotations

import argparse
import json
import sqlite3

from tieru.capabilities.models import CapabilityRouterConfig
from tieru.capabilities.router import CapabilityRouter
from tieru.config import Settings
from tieru.tools import build_registry


def run_capability_cli(args: argparse.Namespace | list[str], settings: Settings | None = None) -> int:
    if isinstance(args, (list, tuple)):
        parser = argparse.ArgumentParser(prog="tieru capability")
        sub = parser.add_subparsers(dest="capability_command")
        route_p = sub.add_parser("route")
        route_p.add_argument("query")
        route_p.add_argument("--json", action="store_true")
        parsed_args = parser.parse_args(args)
    else:
        parsed_args = args

    if settings is None:
        settings = Settings()

    conn = sqlite3.connect(":memory:")
    registry = build_registry(conn, settings)
    config = CapabilityRouterConfig(
        max_visible_tools=getattr(settings, "capability_max_visible_tools", 8),
        min_score=getattr(settings, "capability_min_score", 0.30),
        top_k=getattr(settings, "capability_top_k", 4),
    )
    router = CapabilityRouter(config)
    query = getattr(parsed_args, "query", "")
    result = router.route(query, registry)

    if getattr(parsed_args, "json", False):
        output = {
            "query": query,
            "candidate_tool_count": result.candidate_tool_count,
            "truncated": result.truncated,
            "selected_capabilities": [
                {
                    "capability_id": m.capability_id,
                    "score": m.score,
                    "lexical_score": m.lexical_score,
                    "semantic_score": m.semantic_score,
                    "reason": m.reason,
                    "matched_alias": m.matched_alias,
                    "tool_names": list(m.tool_names),
                }
                for m in result.selected_capabilities
            ],
            "selected_tools": list(result.selected_tools),
        }
        print(json.dumps(output, indent=2))
        return 0

    print("Selected capabilities:")
    if result.selected_capabilities:
        for m in result.selected_capabilities:
            print(f"- {m.capability_id} {m.score:.2f} ({m.reason})")
    else:
        print("(none)")

    print("\nTools:")
    if result.selected_tools:
        for t in result.selected_tools:
            print(f"- {t}")
    else:
        print("(none)")
    return 0

"""Read-only Model Fabric planning commands."""

from __future__ import annotations

import json

from tieru.fabric import ModelFabric
from tieru.fabric.selection import ModelSelectionError, RoutingOverrides
from tieru.loop.models import ModelRouter


def run_fabric_cli(args, settings) -> int:
    replay = None
    conn = None
    if args.fabric_command == "stats":
        from tieru.db import connect
        from tieru.replay import ReplayService

        settings.ensure_home()
        conn = connect(settings.home)
        replay = ReplayService(conn, settings)
    fabric = ModelFabric(settings, ModelRouter(settings), replay=replay)
    if args.fabric_command == "status":
        result = fabric.status()
    elif args.fabric_command == "modes":
        result = {"modes": fabric.modes()}
    elif args.fabric_command == "models":
        result = {"models": fabric.models(available_only=args.available)}
    elif args.fabric_command == "refresh":
        result = {"availability": fabric.refresh()}
    elif args.fabric_command == "stats":
        result = {
            "min_history_samples": settings.fabric_min_history_samples,
            "performance": fabric.performance.stats(),
        }
    elif args.fabric_command in {"explain", "classify"}:
        overrides = RoutingOverrides(
            force_local=args.local, preferred_model=args.preferred_model,
            force_mode=args.mode,
        )
        try:
            result = fabric.explain(args.message, overrides=overrides)
        except ModelSelectionError as exc:
            result = {
                "error": "no_eligible_model",
                "message": str(exc),
                "candidates": [item.public() for item in exc.evaluations],
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 2
    elif args.fabric_command == "score":
        overrides = RoutingOverrides(
            force_local=args.local, preferred_model=args.preferred_model,
            force_mode=args.mode,
        )
        try:
            decision = fabric.route(args.message, allow_classifier=False, overrides=overrides)
            result = {
                "execution_mode": decision.mode.value,
                "privacy_policy": settings.fabric_routing_policy,
                "model_selection": decision.model_selection.public(),
            }
        except ModelSelectionError as exc:
            result = {
                "error": "no_eligible_model", "message": str(exc),
                "candidates": [item.public() for item in exc.evaluations],
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 2
    else:
        raise ValueError(f"unknown Fabric command: {args.fabric_command}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if conn is not None:
        conn.close()
    return 0

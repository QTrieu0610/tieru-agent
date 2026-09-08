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
    elif args.fabric_command == "roles":
        from pathlib import Path

        from tieru.fabric.roles import (
            ModelRole,
            load_role_policy,
            resolve_effective_role_assignment,
        )

        policy_path = getattr(settings, "role_policy_path", None) or Path("evals/baselines/model_role_policy.json")
        policy = load_role_policy(policy_path)

        if getattr(args, "recommendations", False):
            recs = []
            if policy and policy.evidence_summary:
                for r in ModelRole:
                    assign = policy.assignments.get(r.value, {})
                    rec_item = {
                        "role": r.value,
                        "current_model": assign.get("fallback_model") or assign.get("primary_model", ""),
                        "recommended_model": assign.get("primary_model", ""),
                        "confidence": assign.get("confidence", "LOW"),
                        "evidence": assign.get("evidence_source", ""),
                        "selection_source": assign.get("selection_source", "default"),
                    }
                    recs.append(rec_item)
            if getattr(args, "json", False):
                print(json.dumps(recs, indent=2, ensure_ascii=False))
                return 0
            print(f"{'Role':<18} {'Current':<18} {'Recommended':<18} {'Confidence':<12} {'Source':<16}")
            print("-" * 82)
            for item in recs:
                print(f"{item['role']:<18} {item['current_model']:<18} {item['recommended_model']:<18} {item['confidence']:<12} {item['selection_source']:<16}")
            return 0

        if getattr(args, "apply_recommended", False):
            if getattr(args, "dry_run", False):
                plan = []
                for r in ModelRole:
                    assign = resolve_effective_role_assignment(r, settings, policy=policy)
                    plan.append({
                        "role": r.value,
                        "current_effective": settings.role(r.value).model,
                        "would_assign": assign.primary_model,
                        "source": assign.selection_source,
                    })
                if getattr(args, "json", False):
                    print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
                else:
                    print("DRY RUN: Evaluated evidence-backed recommendations (no persistent files modified):")
                    for p in plan:
                        print(f"  - {p['role']}: {p['current_effective']} -> {p['would_assign']} ({p['source']})")
                return 0
            print("Error: Persistent automatic config modification disabled in M34. Use --dry-run or configure settings explicitly.")
            return 1

        assignments = []
        for r in ModelRole:
            assign = resolve_effective_role_assignment(r, settings, policy=policy)
            assignments.append({
                "role": r.value,
                "effective_model": assign.primary_model,
                "provider": assign.primary_provider,
                "source": assign.selection_source,
                "fallback": assign.fallback_model,
            })

        if getattr(args, "json", False):
            print(json.dumps(assignments, indent=2, ensure_ascii=False))
            return 0

        print(f"{'Role':<18} {'Effective Model':<22} {'Source':<16}")
        print("-" * 56)
        for a in assignments:
            print(f"{a['role']:<18} {a['effective_model']:<22} {a['source']:<16}")
        return 0
    else:
        raise ValueError(f"unknown Fabric command: {args.fabric_command}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if conn is not None:
        conn.close()
    return 0


def run_model_cli(args, settings) -> int:
    """Entrypoint for tieru model <command>."""
    args.fabric_command = getattr(args, "model_command", "roles")
    return run_fabric_cli(args, settings)

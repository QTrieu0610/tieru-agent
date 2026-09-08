"""Explicit, foreground CLI for human recovery decisions."""

from __future__ import annotations

import json
import sys

from tieru.db import connect
from tieru.recovery import RecoveryError, RecoveryResolution, RecoveryService, RecoveryStore
from tieru.replay import ReplayService


def _default(value):
    return getattr(value, "value", str(value))


def _print(value, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=_default))
        return
    if isinstance(value, list):
        if not value:
            print("No uncertain executions.")
            return
        for item in value:
            print(
                f"{item['action_fingerprint']}  {item['status']}  {item['tool_name']}  "
                f"attempts={item['attempt_count']}"
            )
        return
    execution = value.get("execution") if isinstance(value, dict) else None
    if isinstance(execution, dict):
        print(f"Execution: {execution['action_fingerprint']}")
        print(f"Tool: {execution['tool_name']}")
        print(f"Status: {execution['status']}")
        print(f"Completion source: {execution.get('completion_source', 'tool')}")
        print(f"Attempts: {execution['attempt_count']}")
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, default=_default))


def run_recovery_cli(args, settings) -> int:
    conn = connect(settings.home)
    try:
        service = RecoveryService(
            RecoveryStore(conn), replay=ReplayService(conn, settings)
        )
        command = args.recovery_command
        if command == "list":
            _print(service.list_uncertain(limit=args.limit), json_output=args.json)
            return 0
        if command == "show":
            _print(service.show_execution(args.resource_id), json_output=args.json)
            return 0
        if not args.yes:
            print(
                "Recovery mutation requires --yes after human inspection.",
                file=sys.stderr,
            )
            return 2
        resolution = {
            "completed": RecoveryResolution.CONFIRMED_COMPLETED,
            "not-executed": RecoveryResolution.CONFIRMED_NOT_EXECUTED,
            "abandoned": RecoveryResolution.ABANDONED,
        }[args.resolution]
        value = service.resolve_execution(
            args.action_fingerprint,
            resolution,
            note=args.note,
            source="cli",
        )
        _print(value, json_output=args.json)
        return 0
    except (RecoveryError, KeyError) as exc:
        code = getattr(exc, "code", "recovery_not_found")
        print(f"{code}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

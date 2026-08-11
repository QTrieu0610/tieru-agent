"""CLI presentation for passive Shadow metadata and explicit Forge handoff."""

from __future__ import annotations

import json

from tieru.db import connect
from tieru.shadow import ShadowService


def run_shadow_cli(args, settings) -> int:
    settings.ensure_home()
    conn = connect(settings.home)
    service = ShadowService(conn, settings)
    try:
        command = args.shadow_command
        if command == "status":
            result = service.status()
        elif command == "patterns":
            result = {"patterns": service.patterns()}
        elif command == "suggestions":
            result = {"suggestions": service.suggestions(include_inactive=args.all)}
        elif command == "inspect":
            result = service.inspect(args.suggestion_id)
        elif command == "ignore":
            result = service.ignore(args.suggestion_id).public()
        elif command == "snooze":
            result = service.snooze(args.suggestion_id, days=args.days).public()
        elif command == "dismiss":
            result = service.dismiss(args.suggestion_id).public()
        elif command == "enable":
            result = service.set_enabled(True)
        elif command == "disable":
            result = service.set_enabled(False)
        elif command == "forge":
            from tieru.forge.cli import make_service

            forge_service = make_service(settings, use_model=True)
            try:
                result = service.forge(args.suggestion_id, forge_service).public()
            finally:
                forge_service.replay.store.conn.close()
        else:
            raise ValueError(f"unknown Shadow command: {command}")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    finally:
        conn.close()

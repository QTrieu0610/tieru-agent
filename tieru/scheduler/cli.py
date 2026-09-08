"""CLI adapter for explicit schedule management and bounded ticks."""

from __future__ import annotations

import json
from dataclasses import asdict

from tieru.app import Tieru
from tieru.db import connect
from tieru.scheduler.models import ScheduleStatus
from tieru.scheduler.runner import SchedulerRunner
from tieru.scheduler.service import SchedulerService
from tieru.scheduler.store import ScheduleStore


def _print(value, *, json_output: bool) -> None:
    payload = [asdict(item) for item in value] if isinstance(value, list) else asdict(value)
    if json_output:
        print(json.dumps(payload, indent=2, default=str))
        return
    if isinstance(payload, list):
        for item in payload:
            print(
                f"{item.get('schedule_id') or item.get('run_id')} "
                f"{item['status']} {item.get('name') or item.get('scheduled_for', '')}"
            )
    else:
        print(json.dumps(payload, indent=2, default=str))


def run_schedule_cli(args, settings) -> int:
    if args.schedule_command == "tick":
        tieru = Tieru(settings=settings)
        try:
            result = SchedulerRunner(
                ScheduleStore(tieru.conn), tieru.tasks, replay=tieru.replay
            ).tick(max_occurrences=args.max_occurrences)
            _print(result, json_output=args.json)
        finally:
            tieru.close()
            tieru.conn.close()
        return 0

    settings.ensure_home()
    conn = connect(settings.home)
    try:
        service = SchedulerService(ScheduleStore(conn))
        command = args.schedule_command
        if command == "create":
            if args.at:
                value = service.create_once(
                    name=args.name, goal=args.goal, at=args.at,
                    timezone_name=args.timezone,
                )
            else:
                value = service.create_interval(
                    name=args.name, goal=args.goal, every=args.every,
                    timezone_name=args.timezone,
                )
        elif command == "list":
            status = ScheduleStatus(args.status) if args.status else None
            value = service.list(status=status, limit=args.limit)
        elif command == "show":
            value = service.show(args.schedule_id)
        elif command == "runs":
            value = service.runs(args.schedule_id, limit=args.limit)
        elif command == "pause":
            value = service.pause(args.schedule_id)
        elif command == "resume":
            value = service.resume(args.schedule_id)
        elif command == "cancel":
            value = service.cancel(args.schedule_id)
        else:
            raise ValueError(f"unknown schedule command: {command}")
        _print(value, json_output=args.json)
        return 0
    finally:
        conn.close()

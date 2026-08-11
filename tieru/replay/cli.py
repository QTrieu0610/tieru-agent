"""Small read-only CLI renderer for Tieru Replay."""

from __future__ import annotations

import json
from typing import Any

from tieru.config import Settings
from tieru.db import connect
from tieru.replay.service import ReplayService


def _stamp(value: str) -> str:
    return (value or "").replace("T", " ")[:23]


def _event_line(event: dict[str, Any]) -> str:
    subject = event.get("tool") or event.get("model") or event.get("node") or ""
    payload = event.get("safe_payload") or {}
    verdict = ""
    if event.get("event_type") == "trust_decision":
        verdict = "allowed" if payload.get("allowed") else "denied"
    elif event.get("event_type", "").startswith("tool_"):
        verdict = event["event_type"].removeprefix("tool_")
    detail = " → ".join(part for part in (subject, verdict) if part)
    duration = event.get("duration_ms")
    timing = f" · {duration}ms" if duration is not None else ""
    return (
        f"{event['sequence']:02d}  {event['category'].title():<9} "
        f"{event['event_type']}{(' · ' + detail) if detail else ''}{timing}"
    )


def run_replay_cli(args, settings: Settings) -> int:
    settings.ensure_home()
    conn = connect(settings.home)
    service = ReplayService(conn, settings)
    selector = args.replay_selector
    limit = args.limit
    try:
        if selector == "list":
            runs = service.list_runs(limit=limit)
            if args.json:
                print(json.dumps(runs, ensure_ascii=False, indent=2))
                return 0
            print("Tieru Replay")
            if not runs:
                print("No Replay runs recorded yet.")
                return 0
            for item in runs:
                duration = (
                    f"{item['latency_ms']}ms" if item["latency_ms"] is not None else "running"
                )
                print(
                    f"{item['run_id']}  {item['status']:<9} {_stamp(item['started_at'])}  "
                    f"{item['source']}  {item['model'] or '—'}  {duration}  "
                    f"tools={item['tool_count']}"
                )
            return 0

        if selector == "last":
            runs = service.list_runs(limit=1)
            if not runs:
                print("No Replay runs recorded yet.")
                return 0
            selector = runs[0]["run_id"]

        detail = service.inspect(selector, include_events=True)
        if args.json:
            print(json.dumps(detail, ensure_ascii=False, indent=2))
            return 0
        summary = detail["summary"]
        print("Tieru Replay")
        print(f"Run: {detail['run_id']}")
        print(f"Status: {detail['status']}")
        print(f"Model: {detail['model'] or '—'}")
        print(f"Provider: {detail['provider'] or '—'}")
        print(f"Duration: {detail['latency_ms'] if detail['latency_ms'] is not None else '—'}ms")
        print(f"Iterations: {detail['iterations']}")
        if detail["error_summary"]:
            print(f"Error: {detail['error_code']} · {detail['error_summary']}")
        print("\nTimeline")
        for event in detail["events"]:
            print(_event_line(event))
            if args.events and event["safe_payload"]:
                print("    " + json.dumps(event["safe_payload"], ensure_ascii=False, sort_keys=True))
        tools, trust = summary["tools"], summary["trust"]
        print(
            "\nSummary: " + summary["sentence"] + " "
            f"Tools {tools['successful']} ok/{tools['denied']} denied/{tools['failed']} failed; "
            f"Trust {trust['allowed']} allowed/{trust['denied']} denied."
        )
        return 0
    except KeyError:
        print(f"Replay run not found: {selector}")
        return 1
    finally:
        conn.close()

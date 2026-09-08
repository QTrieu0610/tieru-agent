"""Focused CLI for explicit durable-task creation and execution."""

from __future__ import annotations

import json
from dataclasses import asdict

from tieru.db import connect
from tieru.tasks.models import TaskBudget, TaskStatus
from tieru.tasks.store import TaskStore


def _json_default(value):
    return getattr(value, "value", str(value))


def _print(value, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default))
        return
    if isinstance(value, list):
        if not value:
            print("No durable tasks.")
            return
        for item in value:
            print(f"{item['task_id']}  {item['status']}  {item['goal']}")
        return
    task = value.get("task", value)
    print(f"Task: {task['task_id']}")
    print(f"Status: {task['status']}")
    print(f"Goal: {task['goal']}")

    contract = value.get("goal_contract")
    if contract:
        constraints = contract.get("constraints", [])
        if constraints:
            print("\nConstraints:")
            for c in constraints:
                print(f"  - [{c.get('constraint_id', 'c')}] {c.get('description')}")
        criteria = contract.get("success_criteria", [])
        if criteria:
            print("\nSuccess Criteria:")
            ver = value.get("goal_verification") or {}
            c_statuses = {
                cr.get("criterion_id"): cr.get("status")
                for cr in ver.get("criterion_results", [])
            }
            crit_syms = {"pass": "✓", "fail": "✗", "blocked": "!", "unknown": "?"}
            for sc in criteria:
                cid = sc.get("criterion_id")
                st = c_statuses.get(cid, "pending")
                sym = crit_syms.get(st, " ")
                print(f"  {sym} [{cid}] {sc.get('description')}")

    ver = value.get("goal_verification")
    if ver:
        print(f"\nGoal Verification: {ver.get('status', '').upper()}")
        if ver.get("summary"):
            print(f"  Summary: {ver.get('summary')}")

    budget_info = value.get("budget")
    if budget_info:
        limits = budget_info.get("limits", {})
        usage = budget_info.get("usage", {})
        rem = budget_info.get("remaining", {})
        print("\nBudget Governance:")
        print(f"  Model calls:  {usage.get('model_calls', 0)}/{limits.get('max_model_calls', 0)} (remaining: {rem.get('model_calls', 0)})")
        print(f"  Tool calls:   {usage.get('tool_calls', 0)}/{limits.get('max_tool_calls', 0)} (remaining: {rem.get('tool_calls', 0)})")
        print(f"  Steps:        {usage.get('steps', 0)}/{limits.get('max_steps', 0)} (remaining: {rem.get('steps', 0)})")
        print(f"  Replans:      {usage.get('replans', 0)}/{limits.get('max_replans', 0)} (remaining: {rem.get('replans', 0)})")
        print(f"  Active time:  {usage.get('active_runtime_seconds', 0.0):.1f}s/{limits.get('max_active_runtime_seconds', 0.0):.1f}s")
        print(f"  Command time: {usage.get('command_runtime_seconds', 0.0):.1f}s/{limits.get('max_command_runtime_seconds', 0.0):.1f}s")

    revisions = value.get("revisions")
    if not revisions:
        for step in value.get("steps", []):
            print(
                f"  {step['position']}. [{step['status']}] {step['title']}"
                f"\n     {step['instruction']}"
            )
        return

    steps = value.get("steps", [])
    rev_numbers = {r["revision_id"]: r["revision_number"] for r in revisions}
    by_rev: dict[int, list] = {r["revision_number"]: [] for r in revisions}
    for step in steps:
        rev_id = step.get("plan_revision_id")
        num = rev_numbers.get(rev_id, 0)
        by_rev.setdefault(num, []).append(step)

    symbols = {
        "succeeded": "✓",
        "superseded": "~",
        "running": "→",
        "failed": "✗",
        "blocked": "!",
        "skipped": "-",
        "pending": " ",
    }
    for r in revisions:
        num = r["revision_number"]
        print(f"\nPlan revision {num}")
        for step in by_rev.get(num, []):
            sym = symbols.get(step["status"], " ")
            tag = " [superseded]" if step["status"] == "superseded" else ""
            print(f"  {sym} {step['position']}. {step['title']}{tag}")
            print(f"     {step['instruction']}")


def _read_store(settings):
    conn = connect(settings.home)
    return conn, TaskStore(conn)


def run_task_cli(args, settings) -> int:
    command = args.task_command
    if command in {"list", "show", "cancel", "budget-extend"}:
        conn, store = _read_store(settings)
        try:
            if command == "list":
                status = TaskStatus(args.status) if args.status else None
                items = [
                    asdict(task)
                    for task in store.list_tasks(status=status, limit=args.limit)
                ]
                _print(items, json_output=args.json)
            elif command == "show":
                contract = store.get_goal_contract(args.task_id)
                latest_ver = store.get_latest_goal_verification(args.task_id)
                budget = store.get_task_budget(args.task_id)
                usage = store.get_task_budget_usage(args.task_id)
                remaining = store.get_budget_remaining(args.task_id)
                allocations = store.list_budget_allocations(args.task_id)
                value = {
                    "task": asdict(store.get_task(args.task_id)),
                    "steps": [asdict(step) for step in store.list_steps(args.task_id)],
                    "revisions": [
                        asdict(rev) for rev in store.list_revisions(args.task_id)
                    ],
                    "goal_contract": asdict(contract),
                    "goal_verification": asdict(latest_ver) if latest_ver is not None else None,
                    "budget": {
                        "limits": asdict(budget),
                        "usage": asdict(usage),
                        "remaining": remaining,
                        "allocations": [asdict(a) for a in allocations],
                    },
                }
                _print(value, json_output=args.json)
            elif command == "budget-extend":
                increments = {
                    "max_model_calls": args.model_calls,
                    "max_tool_calls": args.tool_calls,
                    "max_steps": args.steps,
                    "max_replans": args.replans,
                    "max_command_runtime_seconds": args.command_runtime,
                    "max_active_runtime_seconds": args.active_runtime,
                }
                increments = {k: v for k, v in increments.items() if v > 0}
                new_budget = store.extend_budget(
                    args.task_id,
                    actor="operator",
                    reason=args.reason,
                    increments=increments,
                )
                _print({"task_id": args.task_id, "budget": asdict(new_budget)}, json_output=args.json)
            else:
                task = store.cancel(args.task_id)
                _print(asdict(task), json_output=args.json)
            return 0
        finally:
            conn.close()

    from tieru.app import Tieru

    tieru = Tieru(settings=settings)
    try:
        service = tieru.tasks
        if command == "create":
            custom_budget = None
            if any([
                getattr(args, "max_model_calls", None),
                getattr(args, "max_tool_calls", None),
                getattr(args, "max_steps", None),
                getattr(args, "max_active_runtime", None),
            ]):
                def_b = service.store.limits.default_budget()
                custom_budget = TaskBudget(
                    max_model_calls=getattr(args, "max_model_calls", None) or def_b.max_model_calls,
                    max_tool_calls=getattr(args, "max_tool_calls", None) or def_b.max_tool_calls,
                    max_steps=getattr(args, "max_steps", None) or def_b.max_steps,
                    max_active_runtime_seconds=getattr(args, "max_active_runtime", None) or def_b.max_active_runtime_seconds,
                )
            task = service.create(
                goal=args.goal,
                budget=custom_budget,
                source=args.source,
                session_id=args.session_id,
            )
            _print(service.show(task.task_id), json_output=args.json)
        elif command in {"run", "resume"}:
            results = (
                service.resume(args.task_id, max_steps=args.max_steps)
                if command == "resume"
                else service.run(args.task_id, max_steps=args.max_steps)
            )
            value = {
                "results": [asdict(result) for result in results],
                **service.show(args.task_id),
            }
            _print(value, json_output=args.json)
        else:
            if not args.yes:
                print("Task recovery requires --yes after human inspection.")
                return 2
            value = service.recover(
                args.task_id,
                note=args.note,
                source="cli",
            )
            _print(value, json_output=args.json)
        return 0
    finally:
        tieru.close()
        tieru.conn.close()

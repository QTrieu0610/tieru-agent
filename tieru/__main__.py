"""Tieru command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tieru.config import (
    ConfigError,
    load_settings,
    migrate_legacy_home,
    save_active_profile,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tieru",
        description="Tieru — Local-first Personal AI Runtime. One memory. Any model. Your rules.",
    )
    parser.add_argument("--profile", help="model profile name (highest precedence)")
    parser.add_argument("--config", dest="config_path", help="YAML config path")
    parser.add_argument("--main-model", help="override the main role model")
    parser.add_argument("--small-model", help="override the small role model")
    parser.add_argument("--judge-model", help="override the judge role model")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser(
        "doctor", help="run safe, offline-first runtime and configuration diagnostics"
    )
    doctor.add_argument("--json", action="store_true", help="emit share-safe structured JSON")
    init = sub.add_parser(
        "init", help="create a conservative first-run Tieru configuration"
    )
    init.add_argument("--dry-run", action="store_true", help="validate and preview without writing")
    init.add_argument(
        "--mode", choices=("local-only", "local-first", "advanced"),
        help="bounded setup mode; local-only is the non-interactive default",
    )
    init.add_argument("--model", help="local Ollama model identifier")
    init.add_argument(
        "--fabric-policy",
        choices=("local_only", "local_first", "balanced", "quality_first"),
        help="advanced-mode Fabric routing policy",
    )
    init.add_argument("--cloud-provider", help="explicit optional built-in cloud provider")
    init.add_argument("--cloud-model", help="model ID for the explicit cloud provider")
    init.add_argument("--enable-shadow", action="store_true", help="explicitly enable Shadow")
    init.add_argument(
        "--enable-browser", action="store_true", help="explicitly enable restricted browser tools"
    )
    init.add_argument(
        "--browser-domain", action="append", help="allowed browser hostname; may be repeated"
    )
    init.add_argument(
        "--non-interactive", action="store_true", help="use supplied flags and safe defaults"
    )
    init.add_argument("--yes", action="store_true", help="accept a fresh non-interactive plan")
    init.add_argument(
        "--replace",
        action="store_true",
        help="explicitly allow backup and replacement of an existing config",
    )
    models = sub.add_parser("models", help="list models for a provider")
    models.add_argument("--provider", help="provider name; defaults to the main role")
    config = sub.add_parser("config", help="manage Tieru configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    use = config_sub.add_parser("use-profile", help="persist the active YAML profile")
    use.add_argument("name")
    sub.add_parser("dashboard", help="start the local dashboard")
    sub.add_parser("voice", help="start the optional voice gateway")
    sub.add_parser("telegram", help="start the optional Telegram gateway")
    sub.add_parser("discord", help="start the optional Discord gateway")
    sub.add_parser("whatsapp", help="start the optional WhatsApp gateway")
    sub.add_parser("brief", help="run the morning brief loop")
    sub.add_parser("gather", help="run the morning gather graph")
    task = sub.add_parser("task", help="manage explicit local durable tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser(
        "create", help="plan and persist a task without running it"
    )
    task_create.add_argument("goal")
    task_create.add_argument("--source", default="cli")
    task_create.add_argument("--session-id")
    task_create.add_argument("--json", action="store_true")
    task_list = task_sub.add_parser("list", help="list durable tasks")
    task_list.add_argument(
        "--status",
        choices=("planned", "running", "paused", "blocked", "failed", "completed", "cancelled"),
    )
    task_list.add_argument("--limit", type=int, default=50)
    task_list.add_argument("--json", action="store_true")
    task_show = task_sub.add_parser("show", help="show a task and its inspectable plan")
    task_show.add_argument("task_id")
    task_show.add_argument("--json", action="store_true")
    for task_command in ("run", "resume"):
        task_run = task_sub.add_parser(
            task_command, help=f"explicitly {task_command} a task"
        )
        task_run.add_argument("task_id")
        task_run.add_argument("--max-steps", type=int, default=1)
        task_run.add_argument("--json", action="store_true")
    task_cancel = task_sub.add_parser("cancel", help="prevent future task steps")
    task_cancel.add_argument("task_id")
    task_cancel.add_argument("--json", action="store_true")
    task_recover = task_sub.add_parser(
        "recover", help="explicitly recover one blocked task step without executing it"
    )
    task_recover.add_argument("task_id")
    task_recover.add_argument("--note", default="")
    task_recover.add_argument("--yes", action="store_true")
    task_recover.add_argument("--json", action="store_true")
    task_extend = task_sub.add_parser(
        "budget-extend", help="explicitly add resource budget to a task"
    )
    task_extend.add_argument("task_id")
    task_extend.add_argument("--model-calls", type=float, default=0.0)
    task_extend.add_argument("--tool-calls", type=float, default=0.0)
    task_extend.add_argument("--steps", type=float, default=0.0)
    task_extend.add_argument("--replans", type=float, default=0.0)
    task_extend.add_argument("--command-runtime", type=float, default=0.0)
    task_extend.add_argument("--active-runtime", type=float, default=0.0)
    task_extend.add_argument("--reason", default="operator_manual_extension")
    task_extend.add_argument("--json", action="store_true")
    schedule = sub.add_parser("schedule", help="manage local persistent schedules")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_create = schedule_sub.add_parser(
        "create", help="persist a schedule without pre-authorizing execution"
    )
    schedule_create.add_argument("goal")
    schedule_create.add_argument("--name", required=True)
    trigger = schedule_create.add_mutually_exclusive_group(required=True)
    trigger.add_argument("--at", help="one-shot ISO 8601 time")
    trigger.add_argument("--every", help="fixed elapsed interval, for example 6h")
    schedule_create.add_argument("--timezone", default="UTC")
    schedule_create.add_argument("--json", action="store_true")
    schedule_list = schedule_sub.add_parser("list", help="list schedules")
    schedule_list.add_argument(
        "--status", choices=("active", "paused", "completed", "cancelled")
    )
    schedule_list.add_argument("--limit", type=int, default=100)
    schedule_list.add_argument("--json", action="store_true")
    schedule_show = schedule_sub.add_parser("show", help="show one schedule")
    schedule_show.add_argument("schedule_id")
    schedule_show.add_argument("--json", action="store_true")
    schedule_runs = schedule_sub.add_parser("runs", help="list schedule occurrences")
    schedule_runs.add_argument("schedule_id")
    schedule_runs.add_argument("--limit", type=int, default=100)
    schedule_runs.add_argument("--json", action="store_true")
    for schedule_command in ("pause", "resume", "cancel"):
        item = schedule_sub.add_parser(schedule_command, help=f"{schedule_command} a schedule")
        item.add_argument("schedule_id")
        item.add_argument("--json", action="store_true")
    schedule_tick = schedule_sub.add_parser("tick", help="run one bounded foreground pass")
    schedule_tick.add_argument("--max-occurrences", type=int, default=10)
    schedule_tick.add_argument("--json", action="store_true")
    recovery = sub.add_parser("recovery", help="inspect and reconcile ambiguous execution")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_list = recovery_sub.add_parser("list", help="list uncertain executions")
    recovery_list.add_argument("--limit", type=int, default=50)
    recovery_list.add_argument("--json", action="store_true")
    recovery_show = recovery_sub.add_parser("show", help="show safe execution recovery evidence")
    recovery_show.add_argument("resource_id")
    recovery_show.add_argument("--json", action="store_true")
    recovery_resolve = recovery_sub.add_parser(
        "resolve-execution", help="persist an explicit human execution decision"
    )
    recovery_resolve.add_argument("action_fingerprint")
    recovery_resolve.add_argument(
        "--resolution", choices=("completed", "not-executed", "abandoned"), required=True
    )
    recovery_resolve.add_argument("--note", default="")
    recovery_resolve.add_argument("--yes", action="store_true")
    recovery_resolve.add_argument("--json", action="store_true")
    skill = sub.add_parser("skill", help="manage procedural skills")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    forge = skill_sub.add_parser("forge", help="create an inactive draft from Replay runs")
    forge.add_argument("run_ids", nargs="+")
    forge.add_argument("--json", action="store_true")
    drafts = skill_sub.add_parser("drafts", help="list local Forge drafts")
    drafts.add_argument("--json", action="store_true")
    for command in ("inspect", "validate", "evaluate", "reject"):
        item = skill_sub.add_parser(command, help=f"{command} a Forge draft")
        item.add_argument("draft_id")
        item.add_argument("--json", action="store_true")
    install = skill_sub.add_parser("install", help="install a reviewed draft or legacy URL")
    install.add_argument("target")
    install.add_argument("--json", action="store_true")
    replay = sub.add_parser("replay", help="inspect recorded Replay runs (read-only)")
    replay.add_argument("replay_selector", nargs="?", default="last",
                        help="run ID, 'last', or 'list'")
    replay.add_argument("--limit", type=int, default=20, help="maximum runs to list")
    replay.add_argument("--json", action="store_true", help="emit structured JSON")
    replay.add_argument("--events", action="store_true",
                        help="show each event's safe bounded payload")
    eval_parser = sub.add_parser(
        "eval", help="run evidence-based reliability evaluation and regression reports"
    )
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_run = eval_sub.add_parser("run", help="run the isolated deterministic corpus")
    eval_run.add_argument("--corpus", action="append", help="JSON corpus path; repeatable")
    eval_run.add_argument("--category", help="run one category")
    eval_run.add_argument("--case", dest="case_id", help="run one stable case ID")
    eval_run.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    eval_run.add_argument("--output", help="write a secret-safe result artifact")
    eval_run.add_argument("--compare", help="compare with a versioned baseline")
    eval_run.add_argument("--judge", action="store_true", help="enable optional tool-free judge")
    eval_run.add_argument("--live", action="store_true", help="explicitly use a configured live adapter")
    eval_run.add_argument("--runs", type=int, default=1, help="number of repeated evaluation runs per case")
    eval_run.add_argument("--max-cases", type=int, help="bounded live subset; recorded as PARTIAL")
    eval_run.add_argument(
        "--case-timeout-seconds",
        type=int,
        help="override each selected live case timeout (1-600 seconds)",
    )
    eval_run.add_argument(
        "--overall-timeout-seconds",
        type=int,
        default=3600,
        help="stop a live run after this bounded wall-clock duration",
    )
    eval_run.add_argument(
        "--provider-failure-threshold",
        type=int,
        default=3,
        help="stop after this many consecutive provider-unavailable results",
    )
    eval_run.add_argument(
        "--role-model",
        action="append",
        help="eval-only model override for a role in format <role>=<model>",
    )
    eval_run.add_argument(
        "--role-policy",
        help="eval-only path to evidence-backed role policy artifact (nonpersistent)",
    )
    eval_report = eval_sub.add_parser("report", help="render a saved result artifact")
    eval_report.add_argument("result")
    eval_compare = eval_sub.add_parser("compare", help="compare baseline and current artifacts")
    eval_compare.add_argument("baseline")
    eval_compare.add_argument("current")
    eval_doctor = eval_sub.add_parser("doctor", help="check provider and live evaluation readiness")
    eval_doctor.add_argument("--live", action="store_true", help="probe live configured provider")
    eval_doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    eval_roles = eval_sub.add_parser("roles", help="run role-specific model capability profiling (M33)")
    eval_roles.add_argument("--role", help="filter by specific cognitive role")
    eval_roles.add_argument("--model", help="filter by candidate model")
    eval_roles.add_argument("--runs", type=int, default=1, help="number of repeated benchmark runs per case")
    eval_roles.add_argument("--json", action="store_true", help="emit structured JSON output")
    eval_roles.add_argument("--save-baseline", help="save versioned baseline artifact to path")
    eval_baseline = eval_sub.add_parser("baseline", help="manage explicit baselines")
    eval_baseline_sub = eval_baseline.add_subparsers(
        dest="baseline_command", required=True
    )
    eval_save = eval_baseline_sub.add_parser("save", help="save a result as a baseline")
    eval_save.add_argument("result")
    eval_save.add_argument("path")
    eval_save.add_argument("--live", action="store_true", help="tag baseline as live")
    eval_trace = eval_sub.add_parser(
        "trace", help="trace task execution, checkpoints, and verification provenance"
    )
    eval_trace.add_argument("task_id")
    eval_trace.add_argument("--json", action="store_true")
    eval_trace.add_argument("--db", help="path to database file or directory")
    shadow = sub.add_parser("shadow", help="inspect passive repeated-workflow suggestions")
    shadow_sub = shadow.add_subparsers(dest="shadow_command", required=True)
    for command in ("status", "patterns", "enable", "disable"):
        shadow_sub.add_parser(command)
    suggestions = shadow_sub.add_parser("suggestions")
    suggestions.add_argument("--all", action="store_true", help="include inactive suggestions")
    for command in ("inspect", "forge", "ignore", "dismiss"):
        item = shadow_sub.add_parser(command)
        item.add_argument("suggestion_id")
    snooze = shadow_sub.add_parser("snooze")
    snooze.add_argument("suggestion_id")
    snooze.add_argument("--days", type=int)
    fabric = sub.add_parser("fabric", help="inspect Model Fabric execution planning")
    fabric_sub = fabric.add_subparsers(dest="fabric_command", required=True)
    for command in ("status", "modes", "refresh", "stats"):
        fabric_sub.add_parser(command)
    models = fabric_sub.add_parser("models")
    models.add_argument("--available", action="store_true")
    roles_parser = fabric_sub.add_parser("roles", help="inspect cognitive role assignments and routing")
    roles_parser.add_argument("--json", action="store_true")
    roles_parser.add_argument("--recommendations", action="store_true")
    roles_parser.add_argument("--apply-recommended", action="store_true")
    roles_parser.add_argument("--dry-run", action="store_true")
    for command in ("explain", "classify", "score"):
        item = fabric_sub.add_parser(command)
        item.add_argument("message")
        item.add_argument("--local", action="store_true", help="require a local candidate")
        item.add_argument("--model", dest="preferred_model", default="")
        item.add_argument("--mode", choices=("quick", "standard", "agent", "deep"), default="")
    model = sub.add_parser("model", help="inspect cognitive role routing")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    m_roles = model_sub.add_parser("roles", help="inspect cognitive role assignments and routing")
    m_roles.add_argument("--json", action="store_true")
    m_roles.add_argument("--recommendations", action="store_true")
    m_roles.add_argument("--apply-recommended", action="store_true")
    m_roles.add_argument("--dry-run", action="store_true")
    capsule = sub.add_parser("capsule", help="export, inspect, or import a portable Tieru Capsule")
    capsule_sub = capsule.add_subparsers(dest="capsule_command", required=True)
    export = capsule_sub.add_parser("export", help="build a selective local .tieru snapshot")
    export.add_argument("path")
    export.add_argument("--profile", dest="export_profile",
                        choices=("portable", "full-local-history"), default="portable")
    export.add_argument("--include", action="append",
                        choices=("identity", "memory", "skills", "preferences", "trust", "fabric", "replay", "forge", "shadow"))
    export.add_argument("--exclude", action="append",
                        choices=("identity", "memory", "skills", "preferences", "trust", "fabric", "replay", "forge", "shadow"))
    export.add_argument("--dry-run", action="store_true")
    inspect = capsule_sub.add_parser("inspect", help="verify and describe without importing")
    inspect.add_argument("path")
    import_parser = capsule_sub.add_parser("import", help="preview or execute an additive import")
    import_parser.add_argument("path")
    import_parser.add_argument("--dry-run", action="store_true")
    migrate = sub.add_parser("migrate-home", help="copy legacy .waku state to .tieru without deleting source")
    migrate.add_argument("--from", dest="source", default=".waku")
    migrate.add_argument("--to", dest="target", default=".tieru")
    migrate.add_argument("--yes", action="store_true", help="perform the copy")
    cap = sub.add_parser("capability", help="inspect capability discovery and tool routing")
    cap_sub = cap.add_subparsers(dest="capability_command", required=True)
    cap_route = cap_sub.add_parser("route", help="route tools for a query")
    cap_route.add_argument("query")
    cap_route.add_argument("--json", action="store_true")
    return parser


def _process_overrides(args: argparse.Namespace) -> None:
    mapping = {
        "profile": "TIERU_PROFILE",
        "config_path": "TIERU_CONFIG",
        "main_model": "TIERU_MAIN_MODEL",
        "small_model": "TIERU_SMALL_MODEL",
        "judge_model": "TIERU_JUDGE_MODEL",
    }
    for field, env_name in mapping.items():
        value = getattr(args, field, None)
        if value is not None:
            os.environ[env_name] = str(value)


def _doctor(
    *, json_output: bool = False, selection: dict[str, bool] | None = None
) -> int:
    from tieru.ops.doctor import render_human, render_json, run_doctor

    report = run_doctor(selection=selection)
    print(render_json(report) if json_output else render_human(report))
    return report.exit_code


def _models(provider: str | None) -> int:
    from tieru.ops.catalog import list_models

    result = list_models(provider)
    print(json.dumps(result, indent=2))
    return 1 if result.get("error") and not result.get("models") else 0


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    _process_overrides(args)
    try:
        if args.command == "init":
            from tieru.ops.init import run_init_cli

            raise SystemExit(run_init_cli(args))
        if args.command == "doctor":
            raise SystemExit(
                _doctor(
                    json_output=args.json,
                    selection={
                        "profile_explicit": args.profile is not None,
                        "config_explicit": args.config_path is not None,
                    },
                )
            )
        if args.command == "models":
            raise SystemExit(_models(args.provider))
        if args.command == "config":
            path = save_active_profile(
                args.name, Path(args.config_path) if args.config_path else None
            )
            print(f"Active profile: {args.name} ({path})")
            print("Existing .tieru data was not moved or merged.")
            return
        if args.command == "migrate-home":
            result = migrate_legacy_home(
                Path(args.source), Path(args.target), confirmed=args.yes
            )
            if result["copied"]:
                print(
                    f"Copied {result['source']} to {result['target']}; "
                    "the source was preserved."
                )
            else:
                print(
                    f"Would copy {result['source']} to {result['target']}. "
                    "Re-run with --yes; the source will be preserved."
                )
            return
        if args.command is None:
            from tieru.gateway.cli import main as cli_main

            cli_main()
        elif args.command == "dashboard":
            from tieru.ops.dashboard import main as dashboard_main

            dashboard_main()
        elif args.command == "voice":
            from tieru.gateway.voice import main as voice_main

            voice_main()
        elif args.command == "telegram":
            from tieru.gateway.telegram import main as telegram_main

            telegram_main()
        elif args.command == "discord":
            from tieru.gateway.discord import main as discord_main

            discord_main()
        elif args.command == "whatsapp":
            from tieru.gateway.whatsapp import main as whatsapp_main

            whatsapp_main()
        elif args.command == "brief":
            from tieru.ops.brief import main as brief_main

            brief_main()
        elif args.command == "gather":
            from tieru.ops.gather import main as gather_main

            gather_main()
        elif args.command == "task":
            from tieru.tasks.cli import run_task_cli

            raise SystemExit(run_task_cli(args, load_settings()))
        elif args.command == "schedule":
            from tieru.scheduler.cli import run_schedule_cli

            raise SystemExit(run_schedule_cli(args, load_settings()))
        elif args.command == "recovery":
            from tieru.recovery.cli import run_recovery_cli

            raise SystemExit(run_recovery_cli(args, load_settings()))
        elif args.command == "skill":
            from tieru.forge.cli import run_skill_cli

            raise SystemExit(run_skill_cli(args, load_settings()))
        elif args.command == "replay":
            from tieru.replay.cli import run_replay_cli

            raise SystemExit(run_replay_cli(args, load_settings()))
        elif args.command == "eval":
            from tieru.evals.cli import run_eval_cli

            raise SystemExit(run_eval_cli(args, load_settings()))
        elif args.command == "shadow":
            from tieru.shadow.cli import run_shadow_cli

            raise SystemExit(run_shadow_cli(args, load_settings()))
        elif args.command == "fabric":
            from tieru.fabric.cli import run_fabric_cli

            raise SystemExit(run_fabric_cli(args, load_settings()))
        elif args.command == "model":
            from tieru.fabric.cli import run_model_cli

            raise SystemExit(run_model_cli(args, load_settings()))
        elif args.command == "capsule":
            from tieru.capsule.cli import run_capsule_cli

            raise SystemExit(run_capsule_cli(args, load_settings()))
        elif args.command == "capability":
            from tieru.capabilities.cli import run_capability_cli

            run_capability_cli(args, load_settings())
            return
    except ConfigError as exc:
        parser.error(str(exc))


def legacy_main() -> None:
    print(
        "warning: the legacy Waku command is deprecated; use the canonical 'tieru' command.",
        file=sys.stderr,
    )
    main()


if __name__ == "__main__":
    main()

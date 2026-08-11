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
    for command in ("explain", "classify", "score"):
        item = fabric_sub.add_parser(command)
        item.add_argument("message")
        item.add_argument("--local", action="store_true", help="require a local candidate")
        item.add_argument("--model", dest="preferred_model", default="")
        item.add_argument("--mode", choices=("quick", "standard", "agent", "deep"), default="")
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
        elif args.command == "skill":
            from tieru.forge.cli import run_skill_cli

            raise SystemExit(run_skill_cli(args, load_settings()))
        elif args.command == "replay":
            from tieru.replay.cli import run_replay_cli

            raise SystemExit(run_replay_cli(args, load_settings()))
        elif args.command == "shadow":
            from tieru.shadow.cli import run_shadow_cli

            raise SystemExit(run_shadow_cli(args, load_settings()))
        elif args.command == "fabric":
            from tieru.fabric.cli import run_fabric_cli

            raise SystemExit(run_fabric_cli(args, load_settings()))
        elif args.command == "capsule":
            from tieru.capsule.cli import run_capsule_cli

            raise SystemExit(run_capsule_cli(args, load_settings()))
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

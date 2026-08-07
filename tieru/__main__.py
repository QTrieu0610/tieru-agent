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
from tieru.providers.ollama import OllamaError, OllamaIntegration


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tieru",
        description="Tieru — a transparent, local-first personal AI agent.",
    )
    parser.add_argument("--profile", help="model profile name (highest precedence)")
    parser.add_argument("--config", dest="config_path", help="YAML config path")
    parser.add_argument("--main-model", help="override the main role model")
    parser.add_argument("--small-model", help="override the small role model")
    parser.add_argument("--judge-model", help="override the judge role model")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="validate config, credentials, home, and local providers")
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
    install = skill_sub.add_parser("install")
    install.add_argument("url")
    migrate = sub.add_parser("migrate-home", help="copy .tieru to .tieru without deleting source")
    migrate.add_argument("--from", dest="source", default=".tieru")
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


def _doctor() -> int:
    settings = load_settings()
    print(json.dumps(settings.redacted(), indent=2))
    issues = []
    checked_ollama = set()
    for role_name in ("main", "small", "judge"):
        role = settings.role(role_name)
        provider = settings.providers[role.provider]
        if not provider.keyless and not settings.secret_for(role_name):
            issues.append(
                f"{role_name}: missing {role.api_key_env or 'provider API key'}"
            )
        if role.provider == "ollama" and role.base_url not in checked_ollama:
            checked_ollama.add(role.base_url)
            try:
                result = OllamaIntegration(role.base_url or "").doctor(role.model)
                print(json.dumps({"ollama": result}, indent=2))
                if not result["model_present"]:
                    issues.append(result["error"])
            except OllamaError as exc:
                issues.append(str(exc))
    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    print("Tieru doctor: OK")
    return 0


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
        if args.command == "doctor":
            raise SystemExit(_doctor())
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
            from tieru.memory.procedural.installer import install

            install(args.url)
    except ConfigError as exc:
        parser.error(str(exc))


def legacy_main() -> None:
    print(
        "warning: 'tieru' is deprecated; use the canonical 'tieru' command.",
        file=sys.stderr,
    )
    main()


if __name__ == "__main__":
    main()

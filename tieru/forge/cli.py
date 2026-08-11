"""Command-line presentation for the explicit Skill Forge lifecycle."""

from __future__ import annotations

import json
import sys

from tieru.db import connect
from tieru.forge.service import ForgeService


def _approve(request) -> bool:
    if not sys.stdin.isatty():
        return False
    prompt = f"Approve {request.operation} at {request.target}? [y/N] "
    return input(prompt).strip().lower() in {"y", "yes"}


def make_service(settings, approval_handler=_approve, *, use_model: bool = True) -> ForgeService:
    settings.ensure_home()
    client = None
    model = provider = ""
    if use_model:
        try:
            from tieru.loop.models import ModelRouter

            router = ModelRouter(settings)
            role = router.role("small")
            client, model, provider = router.client("small"), role.model, role.provider
        except (Exception, SystemExit):
            client = None
    return ForgeService(
        connect(settings.home), settings, model_client=client, model=model, provider=provider,
        approval_handler=approval_handler,
    )


def run_skill_cli(args, settings) -> int:
    service = make_service(settings)
    command = args.skill_command
    if command == "forge":
        result = service.forge(args.run_ids).public()
    elif command == "drafts":
        result = {"drafts": service.list()}
    elif command == "inspect":
        result = service.get(args.draft_id).public()
        result["duplicates"] = service.duplicates(service.get(args.draft_id))
    elif command == "validate":
        result = service.validate(args.draft_id).public()
    elif command == "evaluate":
        result = service.evaluate(args.draft_id).public()
    elif command == "reject":
        result = service.reject(args.draft_id).public()
    elif command == "install":
        if str(args.target).startswith(("http://", "https://")):
            from tieru.memory.procedural.installer import install
            install(args.target)
            return 0
        approved = sys.stdin.isatty() and input(
            f"Install reviewed Forge draft {args.target} into your active skills? [y/N] "
        ).strip().lower() in {"y", "yes"}
        result = service.install(args.target, approved=approved).public()
    else:
        raise ValueError(f"unknown skill command: {command}")
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif command == "drafts":
        for item in result["drafts"]:
            print(f"{item['draft_id']}  {item['status']}  {item['skill_id']}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0

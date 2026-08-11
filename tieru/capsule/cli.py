"""Command-line surface for local Capsule lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

from tieru.capsule import CapsuleError, CapsuleService
from tieru.db import connect
from tieru.trust import TrustKernel


def _service(settings) -> tuple[CapsuleService, object]:
    settings.ensure_home()
    conn = connect(settings.home)
    # The command itself is an explicit human action. Trust policy still gets
    # the final say: explicit deny is deny; prompt can be satisfied here.
    kernel = TrustKernel(
        settings.trust_policy, settings.tool_permissions,
        approval_handler=lambda _request: True,
        context={"home": str(settings.home.resolve())},
    )
    return CapsuleService(settings, conn, trust_kernel=kernel), conn


def run_capsule_cli(args, settings) -> int:
    service, conn = _service(settings)
    try:
        source = Path(args.path)
        if args.capsule_command == "export":
            result = service.export(
                source, profile=args.export_profile, include=tuple(args.include or ()),
                exclude=tuple(args.exclude or ()), dry_run=args.dry_run,
            )
        elif args.capsule_command == "inspect":
            result = service.inspect(source)
        else:
            plan = service.plan_import(source)
            result = service.import_capsule(source, plan=plan, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (CapsuleError, PermissionError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1
    finally:
        conn.close()

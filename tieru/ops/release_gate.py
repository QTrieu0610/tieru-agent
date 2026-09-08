"""Release gate — the diamond before "Release" on the whiteboard.

Changed the prompt? Swapped the model? Tuned retrieval top-k? Run the gate:

    python -m tieru.ops.release_gate     (or: make gate)

Deterministic evals must pass 100% — they are unit tests; one failure blocks.
Judge evals run when a key is present and report scores. Exit code 0 = ship.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # the key check below must see .env, same as the app does

REPO = Path(__file__).resolve().parents[2]

SHIPPED_WHEEL_FILES = {
    "tieru/tieru.example.yaml",
    "tieru/ops/static/index.html",
    "tieru/ops/static/style.css",
    "tieru/ops/static/js/main.js",
    "tieru/ops/doctor.py",
    "tieru/ops/init.py",
    "tieru/trust/kernel.py",
    "tieru/replay/service.py",
    "tieru/forge/service.py",
    "tieru/shadow/service.py",
    "tieru/fabric/service.py",
    "tieru/capsule/service.py",
    "tieru/memory/graph/service.py",
    "tieru/evals/cases/core.json",
    "tieru/evals/live/core.json",
    "tieru/evals/fixtures/skill_retrieval_cases.json",
    "tieru/skills/community/meeting-prep/SKILL.md",
    "tieru/skills/schedule-meeting/SKILL.md",
    "tieru/skills/weekly-brief/SKILL.md",
}

SHIPPED_SDIST_FILES = {
    "examples/README.md",
    "examples/mcp.demo.json",
    "examples/mcp_demo_server.py",
    "examples/local-only/README.md",
    "examples/local-only/config.yaml",
    "examples/local-multi-model/README.md",
    "examples/local-multi-model/config.yaml",
    "examples/local-plus-cloud/README.md",
    "examples/local-plus-cloud/config.yaml",
    "examples/privacy-first/README.md",
    "examples/privacy-first/config.yaml",
    "examples/repository-agent/README.md",
    "examples/repository-agent/config.yaml",
    "tieru/tieru.example.yaml",
    "tieru/ops/static/index.html",
    "tieru/ops/doctor.py",
    "tieru/ops/init.py",
    "tieru/trust/kernel.py",
    "tieru/replay/service.py",
    "tieru/forge/service.py",
    "tieru/shadow/service.py",
    "tieru/fabric/service.py",
    "tieru/capsule/service.py",
    "tieru/memory/graph/service.py",
    "evals/cases/core.json",
    "evals/live/core.json",
    "evals/fixtures/skill_retrieval_cases.json",
    "skills/community/meeting-prep/SKILL.md",
    "skills/schedule-meeting/SKILL.md",
    "skills/weekly-brief/SKILL.md",
}


def _forbidden_artifact_path(name: str) -> bool:
    path = Path(name.replace("\\", "/"))
    parts = {part.lower() for part in path.parts}
    basename = path.name.lower()
    return (
        bool(parts & {
            ".tieru", ".waku", ".pytest_cache", ".ruff_cache", "__pycache__",
            "dist", "outbox", "screenshots", "traces",
        })
        or basename in {
            ".env", "state.db", "eval_report.json", "eval_runs.jsonl", "credentials.json",
            "google-token.json",
        }
        or basename.endswith((
            ".db", ".key", ".pem", ".pyc", ".pyo", ".sqlite", ".sqlite3", ".tmp",
            ".tieru",
        ))
    )


def run(suite: str) -> tuple[int, dict]:
    """Run a pytest suite; return (exit_code, {passed, failed}). Counts come
    from the -q summary line — zero extra deps; 0/0 on a miss is honest."""
    print(f"\n=== {suite} ===")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(REPO / "evals" / suite)],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    print(proc.stdout, end="")
    print(proc.stderr, end="", file=sys.stderr)
    counts = {k: (int(m.group(1)) if (m := re.search(rf"(\d+) {k}", proc.stdout)) else 0)
              for k in ("passed", "failed")}
    return proc.returncode, counts


def command(label: str, argv: list[str], *, cwd: Path = REPO) -> bool:
    """Run one release check without a shell; return whether it succeeded."""
    print(f"\n=== {label} ===")
    proc = subprocess.run(argv, cwd=cwd, check=False, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode == 0


def inspect_artifacts(directory: str) -> bool:
    """Pin required runtime data and keep personal files out of distributions."""
    wheel = next(Path(directory).glob("*.whl"), None)
    sdist = next(Path(directory).glob("*.tar.gz"), None)
    if wheel is None or sdist is None:
        print("missing wheel or sdist", file=sys.stderr)
        return False
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
    sdist_root = sdist_names[0].split("/", 1)[0] if sdist_names else ""
    required_sdist = {f"{sdist_root}/{name}" for name in SHIPPED_SDIST_FILES}
    forbidden = "template_Agent.md"
    all_names = [*wheel_names, *sdist_names]
    ok = (
        SHIPPED_WHEEL_FILES <= set(wheel_names)
        and required_sdist <= set(sdist_names)
        and not any(name.startswith("examples/") for name in wheel_names)
        and not any(name.endswith(forbidden) for name in all_names)
        and not any(_forbidden_artifact_path(name) for name in all_names)
    )
    print("artifact contents: PASSED" if ok else "artifact contents: FAILED")
    return ok


def release_checks() -> bool:
    """Lint, compile, build, and validate metadata after deterministic tests."""
    checks = [
        command("ruff", [sys.executable, "-m", "ruff", "check", "tieru", "evals", "scripts"]),
        command("compileall", [sys.executable, "-m", "compileall", "-q", "tieru"]),
    ]
    with tempfile.TemporaryDirectory(prefix="tieru-release-") as directory:
        checks.append(command(
            "wheel and sdist", [sys.executable, "-m", "build", "--outdir", directory]
        ))
        if checks[-1]:
            checks.append(inspect_artifacts(directory))
            artifacts = [str(path) for path in Path(directory).iterdir()]
            checks.append(command(
                "package metadata", [sys.executable, "-m", "twine", "check", *artifacts]
            ))
    return all(checks)


def report(deterministic: str, judge: str, suites: dict | None = None) -> None:
    """Persist the latest verdict AND append it to the run history."""
    import json
    from datetime import datetime

    from tieru.config import load_settings

    settings = load_settings()
    settings.ensure_home()
    record = {
        "deterministic": deterministic,
        "judge": judge,
        "suites": suites or {},
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (settings.home / "eval_report.json").write_text(json.dumps(record), encoding="utf-8")
    with (settings.home / "eval_runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    suites = {}
    code, suites["deterministic"] = run("deterministic")
    if code:
        report("fail", "not run", suites)
        print("\nGATE CLOSED — deterministic evals failed. Fix before releasing.")
        sys.exit(1)
    if not release_checks():
        report("fail", "not run", suites)
        print("\nGATE CLOSED — release checks failed.")
        sys.exit(1)

    # judge needs the ACTIVE provider's key (anthropic, openrouter, ...), same
    # rule as evals/helpers.HAS_KEY
    from tieru.config import load_settings
    from tieru.loop.models import PROVIDERS

    settings = load_settings()
    provider = PROVIDERS.get(settings.provider)
    if settings.api_key or (provider and os.getenv(provider.key_env)):
        code, suites["judge"] = run("judge")
        if code:
            report("pass", "fail", suites)
            print("\nGATE CLOSED — judge scores below threshold.")
            sys.exit(1)
        report("pass", "pass", suites)
    else:
        report("pass", "skipped", suites)
        print(f"\n(judge suite skipped: no API key for provider '{settings.provider}')")

    print("\nGATE OPEN — safe to release.")


if __name__ == "__main__":
    main()

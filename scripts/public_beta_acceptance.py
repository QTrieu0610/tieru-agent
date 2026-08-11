"""R3.1 fresh-install acceptance from built Tieru artifacts.

This is intentionally separate from the deterministic suite: it creates real
virtual environments and installs runtime dependencies. All consumer state is
scoped to an automatically removed operating-system temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "tieru-agent"
DEV_ONLY_PACKAGES = {"build", "pytest", "ruff", "twine"}
PROVIDER_SECRET_SUFFIXES = ("_API_KEY", "_PASSWORD", "_SECRET", "_TOKEN")
PROVIDER_SECRET_NAMES = {
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DISCORD_BOT_TOKEN",
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
    "MINIMAX_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WHATSAPP_TOKEN",
    "XAI_API_KEY",
    "ZHIPU_API_KEY",
}
USER_FLOW = (
    ("replay", "list", "--json"),
    ("fabric", "status"),
    ("shadow", "status"),
)


class AcceptanceError(RuntimeError):
    """A fresh-install acceptance invariant failed."""


@dataclass(frozen=True)
class AcceptanceResult:
    artifact: str
    version: str
    doctor_status: str
    installed_packages: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Tieru and run fresh consumer acceptance from wheel and/or sdist."
    )
    parser.add_argument(
        "--artifact",
        choices=("wheel", "sdist", "both"),
        default="both",
        help="artifact type to install; default validates both",
    )
    return parser


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_tieru(root: Path) -> Path:
    return root / ("Scripts/tieru.exe" if os.name == "nt" else "bin/tieru")


def _redact(text: str) -> str:
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


def _run(
    argv: list[str | Path],
    *,
    cwd: Path,
    env: dict[str, str],
    label: str,
    timeout: int = 300,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in argv]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        raise AcceptanceError(f"{label} timed out after {timeout}s") from exc
    if result.returncode:
        output = _redact("\n".join(part for part in (result.stdout, result.stderr) if part))
        raise AcceptanceError(f"{label} failed with exit code {result.returncode}\n{output}")
    print(f"PASS  {label}")
    return result


def _strip_product_environment(environment: dict[str, str]) -> dict[str, str]:
    for name in tuple(environment):
        upper = name.upper()
        if (
            upper.startswith(("TIERU_", "WAKU_"))
            or upper in PROVIDER_SECRET_NAMES
            or upper.endswith(PROVIDER_SECRET_SUFFIXES)
        ):
            environment.pop(name, None)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    return environment


def _clean_environment(home: Path, user_home: Path) -> dict[str, str]:
    environment = _strip_product_environment(dict(os.environ))
    environment.update(
        {
            "HOME": str(user_home),
            "USERPROFILE": str(user_home),
            "XDG_CACHE_HOME": str(user_home / ".cache"),
            "XDG_CONFIG_HOME": str(user_home / ".config"),
            "XDG_DATA_HOME": str(user_home / ".local" / "share"),
            "TIERU_HOME": str(home),
            "PYTHONUTF8": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "NO_COLOR": "1",
        }
    )
    return environment


def _build_artifacts(directory: Path) -> dict[str, Path]:
    environment = _strip_product_environment(dict(os.environ))
    result = _run(
        [sys.executable, "-m", "build", "--outdir", directory],
        cwd=REPO,
        env=environment,
        label="isolated wheel and sdist build",
    )
    del result
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AcceptanceError(
            f"expected one wheel and one sdist, found {len(wheels)} wheel(s) "
            f"and {len(sdists)} sdist(s)"
        )
    from tieru.ops.release_gate import inspect_artifacts

    if not inspect_artifacts(str(directory)):
        raise AcceptanceError("built artifact content audit failed")
    print("PASS  artifact content audit")
    return {"wheel": wheels[0], "sdist": sdists[0]}


def _installed_probe(python: Path, project: Path, env: dict[str, str]) -> dict[str, str]:
    code = (
        "import importlib.metadata,json,pathlib,tieru;"
        "print(json.dumps({'module':str(pathlib.Path(tieru.__file__).resolve()),"
        f"'version':importlib.metadata.version({PACKAGE_NAME!r})"
        "}))"
    )
    result = _run(
        [python, "-I", "-c", code],
        cwd=project,
        env=env,
        label="installed-package import and metadata",
    )
    return json.loads(result.stdout)


def _assert_clean_runtime_inventory(
    python: Path, project: Path, env: dict[str, str]
) -> int:
    result = _run(
        [python, "-m", "pip", "list", "--format=json"],
        cwd=project,
        env=env,
        label="consumer dependency inventory",
    )
    packages = {item["name"].lower() for item in json.loads(result.stdout)}
    leaked = packages & DEV_ONLY_PACKAGES
    if leaked:
        raise AcceptanceError("consumer venv contains dev-only packages: " + ", ".join(sorted(leaked)))
    return len(packages)


def _accept_artifact(kind: str, artifact: Path, root: Path) -> AcceptanceResult:
    scenario = root / kind
    project = scenario / "project"
    home = project / ".tieru"
    user_home = scenario / "user-home"
    venv_root = scenario / "venv"
    project.mkdir(parents=True)
    user_home.mkdir(parents=True)
    if home.exists():
        raise AcceptanceError(f"{kind} TIERU_HOME was not clean before acceptance")

    print(f"\n=== {kind.upper()} FRESH INSTALL ===")
    venv.EnvBuilder(with_pip=True).create(venv_root)
    python = _venv_python(venv_root)
    tieru = _venv_tieru(venv_root)
    if not python.is_file():
        raise AcceptanceError(f"{kind} venv did not create {python}")
    environment = _clean_environment(home, user_home)
    _run(
        [python, "-m", "pip", "install", "--no-cache-dir", artifact],
        cwd=project,
        env=environment,
        label=f"install {kind} without dev extras",
    )
    if not tieru.is_file():
        raise AcceptanceError(f"{kind} install did not create the tieru console script")

    probe = _installed_probe(python, project, environment)
    module = Path(probe["module"])
    if not module.is_relative_to(venv_root.resolve()):
        raise AcceptanceError(f"{kind} imported Tieru outside its clean venv: {module}")
    if module.is_relative_to(REPO.resolve()):
        raise AcceptanceError(f"{kind} leaked the source checkout into imports")
    package_count = _assert_clean_runtime_inventory(python, project, environment)

    _run([tieru, "--help"], cwd=project, env=environment, label="installed tieru console script")
    if home.exists():
        raise AcceptanceError(f"{kind} --help unexpectedly created TIERU_HOME")
    init = _run(
        [tieru, "init", "--non-interactive", "--yes"],
        cwd=project,
        env=environment,
        label="fresh local-only init",
    )
    config = home / "config.yaml"
    if not config.is_file() or "Configuration created" not in init.stdout:
        raise AcceptanceError(f"{kind} init did not create the expected configuration")
    config_text = config.read_text(encoding="utf-8")
    for expected in ("tieru-local", "provider: ollama", "routing_policy: local_only"):
        if expected not in config_text:
            raise AcceptanceError(f"{kind} init config is missing {expected!r}")
    if re.search(r"(?i)(api[_-]?key|password|token)\s*:", config_text):
        raise AcceptanceError(f"{kind} init wrote credential material")

    doctor = _run(
        [tieru, "doctor", "--json"],
        cwd=project,
        env=environment,
        label="fresh-home doctor",
    )
    doctor_payload = json.loads(doctor.stdout)
    if doctor_payload.get("ready") is not True:
        raise AcceptanceError(f"{kind} Doctor did not report ready")
    if doctor_payload.get("overall_status") not in {"READY", "READY_WITH_WARNINGS"}:
        raise AcceptanceError(f"{kind} Doctor returned an unexpected status")
    package_check = next(
        (item for item in doctor_payload["checks"] if item.get("id") == "runtime.package"),
        None,
    )
    if not package_check or "installed package metadata" not in package_check.get("detail", ""):
        raise AcceptanceError(f"{kind} Doctor did not inspect installed distribution metadata")

    for command in USER_FLOW:
        _run(
            [tieru, *command],
            cwd=project,
            env=environment,
            label="user flow: tieru " + " ".join(command),
        )
    gateway = _run(
        [tieru],
        cwd=project,
        env=environment,
        label="user flow: interactive memory and quit",
        input_text="/memory\n/quit\n",
    )
    if (
        "Semantic facts" not in gateway.stdout
        or "your memory stays in state.db" not in gateway.stdout
    ):
        raise AcceptanceError(f"{kind} interactive CLI did not complete its first-user flow")
    capsule = project / "first-run.tieru"
    _run(
        [tieru, "capsule", "export", capsule, "--dry-run"],
        cwd=project,
        env=environment,
        label="user flow: Capsule export dry-run",
    )
    if capsule.exists():
        raise AcceptanceError(f"{kind} Capsule dry-run wrote an archive")

    return AcceptanceResult(
        artifact=kind,
        version=probe["version"],
        doctor_status=doctor_payload["overall_status"],
        installed_packages=package_count,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="tieru-r31-") as raw_root:
            root = Path(raw_root)
            artifacts = _build_artifacts(root / "artifacts")
            selected = ("wheel", "sdist") if args.artifact == "both" else (args.artifact,)
            results = [_accept_artifact(kind, artifacts[kind], root) for kind in selected]
        print("\nR3.1 FRESH-INSTALL ACCEPTANCE: PASSED")
        for result in results:
            print(
                f"- {result.artifact}: Tieru {result.version}, "
                f"Doctor {result.doctor_status}, {result.installed_packages} runtime packages"
            )
        print("- temporary artifacts, venvs, homes, and projects removed")
        return 0
    except AcceptanceError as exc:
        print(f"\nR3.1 FRESH-INSTALL ACCEPTANCE: FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

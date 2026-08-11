"""R3.0 contract for public, fork-safe, multi-OS GitHub Actions CI."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    # BaseLoader keeps YAML 1.1 words such as ``on`` as strings instead of booleans.
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _step_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_ci_triggers_are_public_and_least_privilege():
    workflow = _workflow()
    assert workflow["name"] == "CI"
    assert {"pull_request", "push", "workflow_dispatch"} <= set(workflow["on"])
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name != 'workflow_dispatch' }}"
    )


def test_compatibility_matrix_covers_supported_public_platforms():
    job = _workflow()["jobs"]["compatibility"]
    strategy = job["strategy"]
    assert strategy["fail-fast"] == "false"
    assert strategy["matrix"]["os"] == [
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]
    assert strategy["matrix"]["python-version"] == ["3.11", "3.12"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["env"] == {"PYTHONUTF8": "1"}


def test_compatibility_job_uses_the_contributor_contract_and_offline_smokes():
    job = _workflow()["jobs"]["compatibility"]
    commands = _step_text(job)
    assert 'python -m pip install -e ".[dev]"' in commands
    assert "python -m compileall -q tieru" in commands
    assert 'python -c "import tieru"' in commands
    assert "python -m pytest -q evals/deterministic" in commands
    test_step = next(
        step for step in job["steps"]
        if step.get("name") == "Run deterministic suite (live integrations skip honestly)"
    )
    assert test_step["env"] == {"TIERU_HOME": "${{ runner.temp }}/tieru-ci"}
    for command in (
        "tieru --help",
        "tieru init --help",
        "tieru doctor --help",
        "tieru replay --help",
        "tieru fabric --help",
        "tieru shadow --help",
        "tieru capsule --help",
    ):
        assert command in commands

    python_step = next(step for step in job["steps"] if "setup-python@" in step.get("uses", ""))
    assert python_step["with"]["cache"] == "pip"
    assert python_step["with"]["cache-dependency-path"] == "pyproject.toml"


def test_quality_job_reuses_the_release_contract_once():
    job = _workflow()["jobs"]["quality"]
    commands = _step_text(job)
    assert job["runs-on"] == "ubuntu-latest"
    assert job["env"] == {"PYTHONUTF8": "1"}
    assert "python -m ruff check tieru evals scripts" in commands
    assert "python scripts/validate_skills.py" in commands
    assert "python -m tieru.ops.release_gate" in commands
    gate_step = next(
        step for step in job["steps"]
        if step.get("name") == "Run release gate (tests, build, metadata, and artifact audit)"
    )
    assert gate_step["env"] == {"TIERU_HOME": "${{ runner.temp }}/tieru-ci"}
    for script in ("main.js", "util.js", "views.js"):
        assert f"node --check tieru/ops/static/js/{script}" in commands


def test_ci_uses_pinned_official_actions_and_no_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    actions = re.findall(r"uses:\s*(actions/[^@\s]+)@([^\s]+)", text)
    assert actions
    assert {name for name, _ in actions} == {
        "actions/checkout",
        "actions/setup-python",
        "actions/setup-node",
    }
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in actions)
    lowered = text.lower()
    assert "pull_request_target" not in lowered
    assert "${{ secrets." not in lowered
    assert not re.search(r"(?:api[_-]?key|token|password)\s*:\s*\S+", lowered)
    assert text.count("${{ runner.temp }}/tieru-ci") == 2


def test_development_metadata_checker_accepts_current_build_metadata():
    with (REPO / "pyproject.toml").open("rb") as handle:
        development_dependencies = tomllib.load(handle)["project"]["optional-dependencies"]["dev"]
    assert "twine>=7,<8" in development_dependencies

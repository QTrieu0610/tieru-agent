"""R4.0 public-demo reproducibility, command, claim, and safety contracts."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tieru.__main__ import _parser
from tieru.config import load_settings
from tieru.db import connect
from tieru.shadow import ShadowService

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "DEMO_GUIDE.md"
README = REPO / "README.md"
EXAMPLES_INDEX = REPO / "examples" / "README.md"
FIXTURE = REPO / "examples" / "demo"
PREPARE = REPO / "scripts" / "prepare_demo.py"
PUBLIC_DEMO_FILES = (GUIDE, FIXTURE / "README.md", PREPARE)
SECRET_SHAPE = re.compile(
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|"
    r"gh[pousr]_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z_-]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


@pytest.fixture(autouse=True)
def clean_configuration_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("TIERU_", "WAKU_")):
            monkeypatch.delenv(name, raising=False)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_prepare(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PREPARE), *map(str, args)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_public_demo_surface_and_links_exist():
    required = (
        GUIDE,
        PREPARE,
        FIXTURE / "README.md",
        FIXTURE / "project" / "demo_math.py",
        FIXTURE / "project" / "check_demo_math.py",
        FIXTURE / "project" / "pytest.ini",
        FIXTURE / "configs" / "local-agent.yaml",
        FIXTURE / "configs" / "capsule-destination.yaml",
        FIXTURE / "portable-skill" / "SKILL.md",
    )
    assert all(path.is_file() for path in required)
    assert "[Public demo guide](docs/DEMO_GUIDE.md)" in _read(README)
    assert "[Public demo fixture](demo/README.md)" in _read(EXAMPLES_INDEX)
    assert "[`docs/DEMO_GUIDE.md`](../../docs/DEMO_GUIDE.md)" in _read(FIXTURE / "README.md")


def test_guide_defines_exactly_three_primary_demos():
    text = _read(GUIDE)
    headings = re.findall(r"(?m)^## Demo ([1-9]) — (.+)$", text)
    assert [number for number, _title in headings] == ["1", "2", "3"]
    assert [title for _number, title in headings] == [
        "Local Agent, Safe Actions, Full Replay",
        "From Repeated Workflow to Reusable Skill",
        "Move Your Tieru Identity to a Fresh Installation",
    ]
    assert "One memory. Any model. Your rules." in text


def test_every_documented_tieru_command_parses_without_execution():
    commands: list[tuple[int, str]] = []
    for line_number, line in enumerate(_read(GUIDE).splitlines(), start=1):
        command = line.strip()
        if command == "tieru" or command.startswith("tieru "):
            commands.append((line_number, command))
    assert len(commands) >= 25
    parser = _parser()
    for line_number, command in commands:
        try:
            parser.parse_args(shlex.split(command, posix=True)[1:])
        except SystemExit as exc:
            pytest.fail(f"{GUIDE}:{line_number} does not parse: {command!r} ({exc.code})")


def test_guide_uses_shipped_commands_for_each_product_moment():
    text = _read(GUIDE)
    for command in (
        "tieru doctor",
        "tieru doctor --json",
        "tieru fabric status",
        'tieru fabric explain "Remember the verified check for demo-project" --local',
        "tieru replay last --events",
        "tieru shadow enable",
        "tieru shadow suggestions",
        "tieru shadow forge <suggestion-id>",
        "tieru skill inspect <draft-id>",
        "tieru skill validate <draft-id>",
        "tieru skill evaluate <draft-id>",
        "tieru skill install <draft-id>",
        "tieru capsule export ../artifacts/tieru-demo.tieru",
        "tieru capsule inspect ../artifacts/tieru-demo.tieru",
        "tieru capsule import ../artifacts/tieru-demo.tieru --dry-run",
        "tieru capsule import ../artifacts/tieru-demo.tieru",
    ):
        assert command in text


@pytest.mark.parametrize(
    "name,expected_fabric",
    (("local-agent.yaml", True), ("capsule-destination.yaml", False)),
)
def test_demo_configs_load_through_real_config_loader(name, expected_fabric, tmp_path):
    path = FIXTURE / "configs" / name
    settings = load_settings({"config_path": path, "home": tmp_path / name})
    assert settings.role("main").provider == "ollama"
    assert settings.role("main").model == "gemma4:e2b"
    assert settings.role("small").provider == "ollama"
    assert settings.role("judge").api_key_env == ""
    assert settings.fabric_enabled is expected_fabric
    assert settings.shadow_enabled is False
    assert settings.browser_enabled is False
    assert settings.trust_policy["default"] == "deny"
    assert settings.trust_policy["capabilities"]["local_write"] == "confirm"
    assert not (tmp_path / name).exists()


def test_fixture_source_is_text_only_and_contains_no_runtime_or_secret_material():
    files = [path for path in FIXTURE.rglob("*") if path.is_file()]
    assert files
    forbidden_names = {
        "state.db", "MEMORY.md", ".env", "credentials.json", "token.json"
    }
    forbidden_suffixes = {
        ".tieru", ".db", ".sqlite", ".sqlite3", ".zip", ".gif", ".mp4", ".pyc"
    }
    for path in files:
        assert path.name not in forbidden_names
        assert path.suffix.lower() not in forbidden_suffixes
        text = _read(path)
        assert SECRET_SHAPE.search(text) is None, path


def test_demo_claims_preserve_product_and_privacy_boundaries():
    text = _read(GUIDE)
    normalized = " ".join(text.split()).lower()
    assert "private reasoning is not recorded" in normalized
    assert "does not remember every conversation automatically" in normalized
    assert "does not claim to select a universally best model" in normalized
    assert "shadow is passive repeated-workflow detection" in normalized
    assert "it only suggests; forge does not run automatically" in normalized
    assert "capsule is a snapshot, not cloud sync" in normalized
    assert "integrity detects corruption; it does not authenticate origin" in normalized
    assert "installed skill is not action authority" in normalized
    for unsafe in ("allow all", "disable trust", "auto approve", "chain-of-thought visibility"):
        assert unsafe not in normalized


def test_demo_docs_are_synthetic_and_have_no_maintainer_absolute_path():
    text = "\n".join(_read(path) for path in PUBLIC_DEMO_FILES)
    lower = text.lower()
    normalized = " ".join(text.split())
    assert "c:\\od\\tieru-agent" not in lower
    assert "c:/od/tieru-agent" not in lower
    assert "vderf" not in lower
    assert "demo-user" in text and "demo-project" in text
    assert "scripts/demo_seed.py" in text and "must not be used" in normalized
    assert "TIERU_HOME=../homes" in text


def test_media_plan_does_not_add_nonexistent_readme_assets():
    guide = _read(GUIDE)
    readme = _read(README)
    for asset in (
        "assets/demo/tieru-hero.gif",
        "assets/demo/forge.gif",
        "assets/demo/capsule.gif",
    ):
        assert asset in guide
        assert asset not in readme


def test_helper_refuses_repository_and_unmarked_replacement(tmp_path):
    repository_result = _run_prepare("--root", REPO)
    assert repository_result.returncode == 2
    assert "outside the Tieru repository" in repository_result.stdout

    unmarked = tmp_path / "not-owned"
    unmarked.mkdir()
    (unmarked / "keep.txt").write_text("keep\n", encoding="utf-8")
    replace_result = _run_prepare("--root", unmarked, "--replace")
    assert replace_result.returncode == 2
    assert (unmarked / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_helper_prepares_seeds_and_cleans_only_disposable_state(tmp_path):
    root = tmp_path / "tieru-public-demo"
    result = _run_prepare(
        "--root", root, "--seed-capsule-a", "--seed-shadow-fixture"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / ".tieru-public-demo-root").is_file()
    assert (root / "project" / "check_demo_math.py").is_file()
    project_check = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root / "project",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert project_check.returncode == 0, project_check.stdout + project_check.stderr
    assert "1 passed" in project_check.stdout

    capsule_home = root / "homes" / "capsule-a"
    capsule_settings = load_settings({
        "home": capsule_home,
        "config_path": root / "configs" / "local-agent.yaml",
    })
    capsule_conn = connect(capsule_home)
    try:
        assert capsule_conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        assert capsule_conn.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0] == 2
        assert capsule_conn.execute("SELECT COUNT(*) FROM graph_relations").fetchone()[0] == 1
        assert (capsule_home / "skills" / "demo-project-check" / "SKILL.md").is_file()
        assert capsule_settings.fabric_enabled is True
    finally:
        capsule_conn.close()

    shadow_home = root / "homes" / "shadow-forge"
    shadow_settings = load_settings({
        "home": shadow_home,
        "config_path": root / "configs" / "local-agent.yaml",
    })
    shadow_settings.shadow_enabled = True
    shadow_conn = connect(shadow_home)
    try:
        suggestions = ShadowService(shadow_conn, shadow_settings).suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["status"] == "ready"
        assert suggestions[0]["occurrence_count"] == 3
        assert suggestions[0]["required_tools"] == ["send_message"]
        rows = shadow_conn.execute(
            "SELECT source, model, provider FROM replay_runs ORDER BY started_at"
        ).fetchall()
        assert len(rows) == 3
        assert all(row["source"] == "deterministic_demo_fixture" for row in rows)
        assert all(row["model"] == "fixture-no-model" for row in rows)
        assert all(row["provider"] == "deterministic" for row in rows)
    finally:
        shadow_conn.close()

    cleanup_result = _run_prepare("--root", root, "--cleanup")
    assert cleanup_result.returncode == 0, cleanup_result.stdout + cleanup_result.stderr
    assert not root.exists()


def test_helper_contains_explicit_narrow_cleanup_guards():
    text = _read(PREPARE)
    assert "repository in root.parents" in text
    assert "root == Path(root.anchor)" in text
    assert "refusing to replace an unmarked directory" in text
    assert "refusing to clean an absent or unmarked directory" in text
    assert "shutil.rmtree(root)" in text
    assert "load_settings()" not in text


def test_guide_has_capture_cleanup_redaction_and_troubleshooting_sections():
    text = _read(GUIDE)
    for heading in (
        "## Prerequisites",
        "## Prepare isolated demo state",
        "### Hero filming plan (45–90 seconds)",
        "### Secondary filming plan (60–120 seconds)",
        "### Portability filming plan (45–90 seconds)",
        "## Recording stability and presentation",
        "## Privacy and redaction checklist",
        "## Product claim checklist",
        "## README media plan",
        "## Troubleshooting",
        "## Cleanup",
    ):
        assert heading in text
    assert all(term in text.lower() for term in ("capture", "cleanup", "redaction"))


def test_demo_work_does_not_change_package_version_or_add_release_actions():
    assert 'version = "0.3.0b1"' in _read(REPO / "pyproject.toml")
    text = "\n".join(_read(path) for path in PUBLIC_DEMO_FILES)
    assert "git push" not in text
    assert "git tag" not in text
    assert "github release" not in text.lower()

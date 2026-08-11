"""R2.2 public quickstart, examples, links, commands, and packaging regressions."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest
import yaml

from tieru.__main__ import _parser
from tieru.config import load_settings
from tieru.fabric.candidates import CandidateRegistry

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
EXAMPLES = REPO / "examples"
EXAMPLE_CONFIGS = {
    "local-only": "local_only",
    "local-multi-model": "local_only",
    "local-plus-cloud": "local_first",
    "privacy-first": "local_only",
    "repository-agent": "local_only",
}
_SECRET_KEYS = {"api_key", "token", "secret", "password", "credentials"}
_SECRET_SHAPE = re.compile(
    r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


@pytest.fixture(autouse=True)
def clean_configuration_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("TIERU_", "WAKU_")):
            monkeypatch.delenv(name, raising=False)
    for provider_key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MINIMAX_API_KEY",
        "MOONSHOT_API_KEY",
        "ZHIPU_API_KEY",
        "XAI_API_KEY",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_GO_API_KEY",
    ):
        monkeypatch.delenv(provider_key, raising=False)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _example_data(name: str) -> dict:
    return yaml.safe_load(_read(EXAMPLES / name / "config.yaml"))


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower().replace("-", "_")
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_readme_puts_the_verified_quickstart_before_subsystem_depth():
    text = _read(README)
    required = (
        "python -m venv .venv",
        ".\\.venv\\Scripts\\Activate.ps1",
        "python3 -m venv .venv",
        "source .venv/bin/activate",
        "python -m pip install -e .",
        "ollama pull gemma4:e2b",
        "tieru init",
        "tieru doctor",
    )
    assert all(command in text for command in required)
    assert text.index("## Five-minute Quick Start") < text.index("## Core Capabilities")
    assert len(text.splitlines()) < 500


def test_readme_states_local_identity_and_optional_credentials_accurately():
    text = _read(README)
    normalized = " ".join(text.split())
    assert "API keys and cloud providers are optional" in normalized
    assert "gemma4:e2b" in text and "verified" in text
    assert "Gemma is not Tieru's identity" in text
    assert "machine-wide YAML path searched automatically" in text
    assert "credentials alone" in text
    assert "Shadow is disabled by default" in text


def test_readme_has_no_invalid_positional_prompt_or_primary_milestone_noise():
    text = _read(README)
    assert re.search(r"(?m)^tieru\s+['\"]", text) is None
    assert not re.search(r"\bM(?:[5-9]|1[0-3])\b", text)
    assert "private chain-of-thought" in text


def test_documented_tieru_commands_all_parse_without_execution():
    markdown = [README, *sorted(EXAMPLES.rglob("README.md"))]
    commands: list[tuple[Path, int, str]] = []
    for path in markdown:
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            command = line.strip()
            if command == "tieru" or command.startswith("tieru "):
                commands.append((path, line_number, command))
    assert len(commands) >= 30
    parser = _parser()
    for path, line_number, command in commands:
        try:
            parser.parse_args(shlex.split(command, posix=True)[1:])
        except SystemExit as exc:
            pytest.fail(f"{path}:{line_number} does not parse: {command!r} ({exc.code})")


def test_feature_workflows_use_the_shipped_cli_shapes():
    text = _read(README)
    for command in (
        "tieru replay list",
        "tieru replay last --events",
        "tieru skill forge <run-id>",
        "tieru skill inspect <draft-id>",
        "tieru skill validate <draft-id>",
        "tieru skill evaluate <draft-id>",
        "tieru skill install <draft-id>",
        "tieru shadow status",
        "tieru shadow enable",
        "tieru shadow suggestions",
        'tieru fabric explain "analyze this repository"',
        "tieru fabric models",
        "tieru capsule export my-tieru.tieru",
        "tieru capsule import my-tieru.tieru --dry-run",
    ):
        assert command in text


def test_every_internal_markdown_link_from_public_readmes_exists():
    markdown = [README, *sorted(EXAMPLES.rglob("README.md"))]
    checked = 0
    for path in markdown:
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", _read(path)):
            if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.IGNORECASE):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            checked += 1
            resolved = (path.parent / relative).resolve()
            assert resolved.exists(), f"broken link in {path}: {target}"
    assert checked >= 20


def test_examples_index_covers_each_public_setup_and_credentials():
    text = _read(EXAMPLES / "README.md")
    for name in EXAMPLE_CONFIGS:
        assert f"{name}/README.md" in text
    assert "External credentials" in text
    assert "ANTHROPIC_API_KEY" in text
    assert "mcp.demo.json" in text


@pytest.mark.parametrize("name,expected_policy", EXAMPLE_CONFIGS.items())
def test_every_example_loads_through_the_real_settings_loader(
    name, expected_policy, tmp_path
):
    path = EXAMPLES / name / "config.yaml"
    settings = load_settings({"config_path": path, "home": tmp_path / name})
    assert settings.config_path == path
    assert settings.fabric_enabled is True
    assert settings.fabric_routing_policy == expected_policy
    assert settings.role("main").provider == "ollama"
    assert settings.role("small").provider == "ollama"
    assert settings.role("judge").api_key_env == ""
    assert settings.shadow_enabled is False
    assert settings.browser_enabled is False
    assert not (tmp_path / name).exists()


@pytest.mark.parametrize("name", EXAMPLE_CONFIGS)
def test_examples_contain_no_credentials_or_broad_trust(name):
    path = EXAMPLES / name / "config.yaml"
    text = _read(path)
    data = yaml.safe_load(text)
    assert not (_SECRET_KEYS & set(_walk_keys(data)))
    assert _SECRET_SHAPE.search(text) is None
    assert "trust" not in data
    assert "tool_permissions" not in data


def test_local_only_examples_have_no_cloud_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured-but-must-not-opt-in-cloud")
    for name, policy in EXAMPLE_CONFIGS.items():
        if policy != "local_only":
            continue
        data = _example_data(name)
        candidates = (data.get("fabric") or {}).get("models") or {}
        assert all(candidate.get("local", True) is True for candidate in candidates.values())
        settings = load_settings(
            {"config_path": EXAMPLES / name / "config.yaml", "home": tmp_path / name}
        )
        assert all(candidate.local for candidate in CandidateRegistry(settings).all())


def test_cloud_example_is_explicit_and_never_stores_key_material():
    data = _example_data("local-plus-cloud")
    candidates = data["fabric"]["models"]
    cloud = [candidate for candidate in candidates.values() if candidate["local"] is False]
    assert data["fabric"]["routing_policy"] == "local_first"
    assert len(cloud) == 1
    assert cloud[0]["provider"] == "anthropic"
    assert cloud[0]["enabled"] is True
    assert "ANTHROPIC_API_KEY" not in _read(
        EXAMPLES / "local-plus-cloud" / "config.yaml"
    )
    guide = _read(EXAMPLES / "local-plus-cloud" / "README.md")
    assert 'ANTHROPIC_API_KEY = "YOUR_API_KEY"' in guide
    assert "export ANTHROPIC_API_KEY=\"YOUR_API_KEY\"" in guide


def test_multi_model_example_marks_unverified_capability_unknown():
    data = _example_data("local-multi-model")
    placeholder = data["fabric"]["models"]["second-local-placeholder"]
    assert placeholder["model"] == "YOUR_SECOND_OLLAMA_MODEL"
    assert placeholder["capabilities"]["tool_calling"] == "unknown"
    assert "not a certified model" in _read(
        EXAMPLES / "local-multi-model" / "README.md"
    )


def test_repository_example_does_not_pre_authorize_process_or_mcp():
    data = _example_data("repository-agent")
    assert data["fabric"]["default_mode"] == "agent"
    assert "trust" not in data and "tool_permissions" not in data
    assert "mcp" not in data and "experimental" not in data
    guide = _read(EXAMPLES / "repository-agent" / "README.md")
    assert "does **not** grant repository or process\naccess" in guide
    assert "explicit approval" in guide


def test_readme_links_public_governance_and_attribution():
    text = _read(README)
    for target in (
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "LICENSE",
        "ShenSeanChen/waku-agent",
    ):
        assert target in text

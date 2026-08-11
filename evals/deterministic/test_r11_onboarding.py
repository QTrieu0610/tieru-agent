"""R1.1 public CLI, doctor, and documentation regressions."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from tieru.__main__ import _doctor, _parser
from tieru.config import load_settings

REPO = Path(__file__).resolve().parents[2]


def test_documented_quickstart_tieru_commands_parse():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert not re.search(r'^tieru\s+["\']', readme, re.MULTILINE)
    commands = (
        "tieru --profile ollama-gemma4-e2b doctor",
        "tieru --profile ollama-gemma4-e2b doctor --json",
        "tieru --profile ollama-gemma4-e2b",
        "tieru dashboard",
        "tieru replay list",
        "tieru replay last --events",
    )
    for command in commands:
        assert command in readme
        _parser().parse_args(shlex.split(command)[1:])


def test_missing_unused_judge_key_does_not_fail_configured_runtime(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIERU_HOME", str(tmp_path / "home"))
    settings = load_settings()
    runtime_key_envs = {settings.role(name).api_key_env for name in ("main", "small")}
    judge_key_env = settings.role("judge").api_key_env
    assert judge_key_env not in runtime_key_envs
    for key_env in runtime_key_envs:
        monkeypatch.setenv(key_env, "configured-test-key")
    monkeypatch.delenv(judge_key_env, raising=False)
    assert _doctor() == 0
    captured = capsys.readouterr()
    assert "OPTIONAL Judge:" in captured.out
    assert "not configured" in captured.out
    assert "Tieru doctor: OK" in captured.out
    assert "FAIL     Judge" not in captured.out


def test_missing_required_runtime_key_still_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TIERU_HOME", str(tmp_path / "home"))
    settings = load_settings()
    main_key_env = settings.role("main").api_key_env
    for name in ("main", "small", "judge"):
        monkeypatch.delenv(settings.role(name).api_key_env, raising=False)
    assert _doctor() == 1
    captured = capsys.readouterr()
    assert "FAIL     Main Model:" in captured.out
    assert main_key_env in captured.out
    assert "Tieru doctor: NOT READY" in captured.out


def test_public_docs_do_not_claim_global_yaml_or_nonexistent_google_command():
    active = [
        REPO / "README.md",
        REPO / "tieru" / "tools" / "google_calendar.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in active)
    assert "global YAML" not in text
    assert "Run `tieru connect google`" not in text
    assert "Connections tab" not in text

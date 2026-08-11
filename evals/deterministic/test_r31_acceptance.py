"""R3.1 contract for clean artifact-to-user acceptance."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "public_beta_acceptance.py"
GUIDE = REPO / "PUBLIC_BETA_ACCEPTANCE.md"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("tieru_public_beta_acceptance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_acceptance_defaults_to_both_distribution_formats():
    module = _acceptance_module()
    assert module._parser().parse_args([]).artifact == "both"
    assert module._parser().parse_args(["--artifact", "wheel"]).artifact == "wheel"


def test_consumer_environment_is_clean_and_scoped(monkeypatch, tmp_path):
    module = _acceptance_module()
    monkeypatch.setenv("TIERU_CONFIG", "developer-config.yaml")
    monkeypatch.setenv("WAKU_HOME", "developer-home")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-cross-boundary")
    monkeypatch.setenv("CUSTOM_SERVICE_TOKEN", "must-not-cross-boundary")
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    monkeypatch.setenv("VIRTUAL_ENV", str(REPO / ".venv"))
    home, user_home = tmp_path / "project" / ".tieru", tmp_path / "user"
    environment = module._clean_environment(home, user_home)
    assert environment["TIERU_HOME"] == str(home)
    assert environment["HOME"] == environment["USERPROFILE"] == str(user_home)
    for name in (
        "TIERU_CONFIG",
        "WAKU_HOME",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "CUSTOM_SERVICE_TOKEN",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        assert name not in environment


def test_acceptance_uses_portable_venv_executables(tmp_path):
    module = _acceptance_module()
    expected_bin = "Scripts" if os.name == "nt" else "bin"
    assert module._venv_python(tmp_path).parent.name == expected_bin
    assert module._venv_tieru(tmp_path).parent.name == expected_bin
    assert module._venv_python(tmp_path).name == (
        "python.exe" if os.name == "nt" else "python"
    )


def test_acceptance_installs_artifacts_without_editable_or_dev_extras():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--no-cache-dir" in text
    assert 'input_text="/memory\\n/quit\\n"' in text
    assert "pip\", \"install" in text
    assert "pip install -e" not in text
    assert "[dev]" not in text
    assert {"build", "pytest", "ruff", "twine"} <= set(
        _acceptance_module().DEV_ONLY_PACKAGES
    )


def test_acceptance_guide_documents_the_real_first_user_flow():
    text = GUIDE.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    for command in (
        "python scripts/public_beta_acceptance.py",
        "tieru init --non-interactive --yes",
        "tieru doctor --json",
        "tieru replay list --json",
        "tieru fabric status",
        "tieru shadow status",
        "`tieru` gateway with `/memory` then `/quit`",
    ):
        assert command in text
    assert "wheel and source distribution" in text
    assert "without editable" in text
    assert "Ollama and cloud credentials are not installation requirements" in text
    assert "wheel: Tieru 0.2.0" in text
    assert "sdist: Tieru 0.2.0" in text
    assert "does not claim a hosted GitHub Actions run" in normalized

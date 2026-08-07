"""M2 contract tests for canonical Tieru config, namespace, CLI, and migration."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tieru.config import ConfigError, TieruCompatibilityWarning, load_settings, migrate_legacy_home

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("TIERU_", "WAKU_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _write_config(path: Path, *, main: str = "yaml-main") -> Path:
    data = {
        "version": 1,
        "active_profile": "mixed",
        "profiles": {
            "mixed": {
                "main": {"provider": "anthropic", "model": main},
                "small": {"provider": "ollama", "model": "small-local"},
                "judge": {"provider": "openai", "model": "judge-hosted"},
            }
        },
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_yaml_profile_resolves_independent_roles(tmp_path):
    path = _write_config(tmp_path / "tieru.yaml")
    settings = load_settings({"config_path": path})
    assert settings.profile == "mixed"
    assert (settings.role("main").provider, settings.role("main").model) == (
        "anthropic", "yaml-main"
    )
    assert (settings.role("small").provider, settings.role("small").model) == (
        "ollama", "small-local"
    )
    assert (settings.role("judge").provider, settings.role("judge").model) == (
        "openai", "judge-hosted"
    )


def test_precedence_cli_then_tieru_then_waku_then_yaml(monkeypatch, tmp_path):
    path = _write_config(tmp_path / "tieru.yaml")
    assert load_settings({"config_path": path}).role("main").model == "yaml-main"

    monkeypatch.setenv("WAKU_MODEL", "legacy-model")
    with pytest.warns(FutureWarning, match="WAKU_MODEL"):
        assert load_settings({"config_path": path}).role("main").model == "legacy-model"

    monkeypatch.setenv("TIERU_MAIN_MODEL", "tieru-model")
    assert load_settings({"config_path": path}).role("main").model == "tieru-model"

    settings = load_settings({"config_path": path, "main_model": "cli-model"})
    assert settings.role("main").model == "cli-model"


def test_yaml_rejects_secrets(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "version: 1\nproviders:\n  bad:\n    protocol: openai\n    api_key: secret\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must not contain a secret"):
        load_settings({"config_path": path})


def test_runtime_home_rules_do_not_move_or_merge_data(monkeypatch, tmp_path):
    legacy = tmp_path / ".waku"
    legacy.mkdir()
    original = b"legacy-database-bytes"
    (legacy / "state.db").write_bytes(original)

    with pytest.warns(TieruCompatibilityWarning, match=r"\.waku"):
        settings = load_settings()
    assert settings.home == Path(".waku")
    assert not (tmp_path / ".tieru").exists()
    assert (legacy / "state.db").read_bytes() == original

    (tmp_path / ".tieru").mkdir()
    settings = load_settings()
    assert settings.home == Path(".tieru")
    assert (legacy / "state.db").read_bytes() == original

    monkeypatch.setenv("TIERU_HOME", "custom-tieru")
    assert load_settings().home == Path("custom-tieru")


def test_canonical_config_selects_tieru_home_over_legacy_data(tmp_path):
    legacy = tmp_path / ".waku"
    legacy.mkdir()
    (legacy / "state.db").write_bytes(b"legacy-stays")
    config = _write_config(tmp_path / "tieru.yaml")

    settings = load_settings({"config_path": config})

    assert settings.home == Path(".tieru")
    assert (legacy / "state.db").read_bytes() == b"legacy-stays"
    assert not Path(".tieru").exists()


def test_canonical_env_selects_tieru_home_over_legacy_data(monkeypatch, tmp_path):
    legacy = tmp_path / ".waku"
    legacy.mkdir()
    (legacy / "state.db").write_bytes(b"legacy-stays")
    monkeypatch.setenv("TIERU_PROFILE", "ollama-gemma4-e2b")

    settings = load_settings()

    assert settings.home == Path(".tieru")
    assert (legacy / "state.db").read_bytes() == b"legacy-stays"
    assert not Path(".tieru").exists()


def test_explicit_home_migration_preserves_source(tmp_path):
    source = tmp_path / ".waku"
    target = tmp_path / ".tieru"
    source.mkdir()
    (source / "state.db").write_bytes(b"source-stays")

    preview = migrate_legacy_home(source, target)
    assert preview["copied"] is False
    assert not target.exists()

    result = migrate_legacy_home(source, target, confirmed=True)
    assert result["copied"] is True
    assert (source / "state.db").read_bytes() == b"source-stays"
    assert (target / "state.db").read_bytes() == b"source-stays"
    with pytest.raises(ConfigError, match="refusing to merge"):
        migrate_legacy_home(source, target, confirmed=True)


def test_distribution_namespace_and_entry_points_are_canonical():
    with (REPO / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    assert project["project"]["name"] == "tieru-agent"
    assert project["project"]["scripts"]["tieru"] == "tieru.__main__:main"
    packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["tieru"]


def test_canonical_and_legacy_module_entry_points():
    canonical = subprocess.run(
        [sys.executable, "-m", "tieru", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert canonical.returncode == 0
    assert "Tieru" in canonical.stdout


def test_cli_use_profile_persists_yaml_without_secrets(tmp_path):
    path = tmp_path / "tieru.yaml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tieru",
            "--config",
            str(path),
            "config",
            "use-profile",
            "ollama-gemma4-e2b",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written == {"version": 1, "active_profile": "ollama-gemma4-e2b"}


def test_config_and_dashboard_settings_share_the_loader(monkeypatch, tmp_path):
    from tieru.ops.settings_api import settings_info

    path = _write_config(tmp_path / "tieru.yaml", main="shared-model")
    monkeypatch.setenv("TIERU_CONFIG", str(path))
    direct = load_settings()
    dashboard = settings_info()
    assert dashboard["profile"] == direct.profile
    assert dashboard["provider"] == direct.role("main").provider
    assert dashboard["model"] == direct.role("main").model
    assert dashboard["small_provider"] == direct.role("small").provider
    assert dashboard["judge_model"] == direct.role("judge").model


def test_secrets_are_redacted_from_repr_and_operational_state(monkeypatch):
    secret = "tieru-super-secret-value"
    monkeypatch.setenv("TIERU_MAIN_API_KEY", secret)
    settings = load_settings()
    assert secret not in repr(settings)
    assert secret not in str(settings.redacted())
    assert settings.redacted()["roles"]["main"]["key_set"] is True


def test_secret_is_not_written_when_redacted_state_is_traced(monkeypatch, tmp_path):
    from tieru.ops.tracing import Tracer

    secret = "never-write-this-secret"
    monkeypatch.setenv("TIERU_API_KEY", secret)
    settings = load_settings({"home": tmp_path})
    settings.ensure_home()
    tracer = Tracer(settings)
    tracer.event("config", settings.redacted())

    assert secret not in tracer.path.read_text(encoding="utf-8")


def test_adapter_error_redacts_provider_secret():
    from tieru.loop.adapters import _model_error

    secret = "provider-secret-value"
    error = _model_error("openai", RuntimeError(f"request used {secret}"), (secret,))
    assert secret not in str(error)
    assert "[REDACTED]" in str(error)

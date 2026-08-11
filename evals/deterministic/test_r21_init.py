"""R2.1 Tieru Init planning, safety, CLI, and first-run regressions."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tieru.__main__ import _parser
from tieru.config import VERIFIED_GEMMA_MODEL, load_settings
from tieru.ops.doctor import run_doctor
from tieru.ops.init import (
    CloudCandidate,
    InitError,
    InitMode,
    InitPlanner,
    SetupAnswers,
    build_config,
    discover_ollama,
    render_plan,
    resolve_target_path,
    run_init_cli,
    serialize_config,
    validate_generated_config,
    write_plan,
)
from tieru.providers.ollama import OllamaError


class HealthyOllama:
    calls = 0

    def __init__(self, base_url):
        self.base_url = base_url

    def health(self):
        type(self).calls += 1
        return {"ok": True, "version": "test-version", "endpoint": self.base_url}

    def models(self):
        return [
            SimpleNamespace(name=VERIFIED_GEMMA_MODEL),
            SimpleNamespace(name="unverified-local:latest"),
        ]


class MissingModelOllama(HealthyOllama):
    def models(self):
        return [SimpleNamespace(name="unverified-local:latest")]


class UnavailableOllama(HealthyOllama):
    def health(self):
        raise OllamaError("connection refused with fake-sensitive-detail")


def _clean_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("TIERU_", "WAKU_")):
            monkeypatch.delenv(name, raising=False)
    for name in (
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
        monkeypatch.delenv(name, raising=False)


def _args(*values):
    return _parser().parse_args(["init", *values])


def test_default_target_matches_automatic_project_discovery(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert resolve_target_path() == resolve_target_path(".tieru/config.yaml")
    assert resolve_target_path().resolve() == tmp_path / ".tieru/config.yaml"


def test_explicit_and_environment_target_follow_loader_precedence(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    tieru_path = tmp_path / "tieru.yaml"
    legacy_path = tmp_path / "waku.yaml"
    monkeypatch.setenv("WAKU_CONFIG", str(legacy_path))
    assert resolve_target_path() == legacy_path
    monkeypatch.setenv("TIERU_CONFIG", str(tieru_path))
    assert resolve_target_path() == tieru_path
    explicit = tmp_path / "explicit.yaml"
    assert resolve_target_path(explicit) == explicit


def test_local_only_config_is_minimal_local_and_safe():
    config = build_config(SetupAnswers())
    profile = config["profiles"][config["active_profile"]]
    assert set(config) == {
        "version",
        "active_profile",
        "profiles",
        "fabric",
        "shadow_enabled",
        "browser_enabled",
    }
    assert all(profile[role] == {"provider": "ollama", "model": VERIFIED_GEMMA_MODEL}
               for role in ("main", "small", "judge"))
    assert config["fabric"] == {"enabled": True, "routing_policy": "local_only"}
    assert config["shadow_enabled"] is False
    assert config["browser_enabled"] is False
    assert "trust" not in config
    assert "providers" not in config
    assert "gateways" not in config
    assert "mcp" not in config


def test_generated_yaml_loads_through_actual_settings_loader():
    answers = SetupAnswers()
    settings = validate_generated_config(serialize_config(build_config(answers)), answers)
    assert settings.profile == "tieru-local"
    assert settings.role("main").provider == "ollama"
    assert settings.role("small").model == VERIFIED_GEMMA_MODEL
    assert settings.role("judge").api_key_env == ""
    assert settings.fabric_enabled is True
    assert settings.fabric_routing_policy == "local_only"
    assert settings.trust_policy == {}


def test_local_first_does_not_infer_cloud_from_api_key(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-that-must-not-route")
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=HealthyOllama).plan(
        SetupAnswers(mode=InitMode.LOCAL_FIRST)
    )
    assert plan.fabric_policy == "local_first"
    assert plan.cloud_candidates == ()
    assert "models" not in plan.config["fabric"]
    assert "anthropic" not in plan.yaml_text
    assert "fake-key-that-must-not-route" not in plan.yaml_text


def test_explicit_cloud_candidate_uses_builtin_provider_metadata_only(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    answers = SetupAnswers(
        mode=InitMode.LOCAL_FIRST,
        cloud_candidates=(CloudCandidate("anthropic", "configured-model-id"),),
    )
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=HealthyOllama).plan(answers)
    models = plan.config["fabric"]["models"]
    assert models["local-main"]["local"] is True
    assert models["cloud-1-anthropic"] == {
        "provider": "anthropic",
        "model": "configured-model-id",
        "local": False,
        "roles": ["main", "small"],
        "capabilities": {"text": True, "tool_calling": True},
        "enabled": True,
        "preference": 0.5,
    }
    assert "api_key" not in plan.yaml_text.lower()
    assert "ANTHROPIC_API_KEY" not in plan.yaml_text


def test_cloud_candidate_remains_valid_without_credential(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    answers = SetupAnswers(
        mode=InitMode.ADVANCED,
        fabric_policy="balanced",
        cloud_candidates=(CloudCandidate("openai", "user-selected-model"),),
    )
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=HealthyOllama).plan(answers)
    assert plan.fabric_policy == "balanced"
    assert "user-selected-model" in plan.yaml_text


@pytest.mark.parametrize(
    "answers,message",
    [
        (
            SetupAnswers(
                cloud_candidates=(CloudCandidate("anthropic", "configured-model"),)
            ),
            "cannot contain cloud",
        ),
        (
            SetupAnswers(mode=InitMode.LOCAL_FIRST, fabric_policy="quality_first"),
            "requires the local_first",
        ),
        (
            SetupAnswers(mode=InitMode.ADVANCED, fabric_policy="unknown"),
            "Fabric policy",
        ),
        (SetupAnswers(local_model="bad\nmodel"), "single-line"),
    ],
)
def test_impossible_states_fail_before_write(answers, message):
    with pytest.raises(InitError, match=message):
        build_config(answers)


def test_ollama_discovery_reports_verified_model_and_bounds_results():
    discovery = discover_ollama(ollama_factory=HealthyOllama)
    assert discovery.reachable is True
    assert discovery.version == "test-version"
    assert discovery.has_model(VERIFIED_GEMMA_MODEL)
    assert "unverified-local:latest" in discovery.installed_models


def test_missing_model_plan_recommends_pull_without_running_it(tmp_path):
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=MissingModelOllama).plan(
        SetupAnswers()
    )
    assert any(
        warning == f"{VERIFIED_GEMMA_MODEL} is not installed. Run: "
        f"ollama pull {VERIFIED_GEMMA_MODEL}"
        for warning in plan.warnings
    )
    assert not plan.target_path.exists()


def test_unavailable_ollama_is_actionable_and_exception_detail_is_hidden(tmp_path):
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=UnavailableOllama).plan(
        SetupAnswers()
    )
    output = render_plan(plan)
    assert "Install/start Ollama" in output
    assert f"ollama pull {VERIFIED_GEMMA_MODEL}" in output
    assert "fake-sensitive-detail" not in output


def test_shadow_and_browser_require_explicit_answers(tmp_path):
    answers = SetupAnswers(
        mode=InitMode.ADVANCED,
        shadow_enabled=True,
        browser_enabled=True,
        browser_allowed_domains=("docs.example.com",),
    )
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=HealthyOllama).plan(answers)
    assert plan.config["shadow_enabled"] is True
    assert plan.config["browser_enabled"] is True
    assert plan.config["browser_allowed_domains"] == ["docs.example.com"]
    assert any("does not install" in warning for warning in plan.warnings)
    assert "browser_allow_local_fixture" not in plan.config


def test_browser_domain_without_enablement_is_rejected():
    with pytest.raises(InitError, match="require browser automation"):
        build_config(SetupAnswers(browser_allowed_domains=("example.com",)))


def test_existing_config_is_detected_and_default_write_refuses(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("version: 1\nactive_profile: default\n", encoding="utf-8")
    plan = InitPlanner(target, ollama_factory=HealthyOllama).plan(SetupAnswers())
    assert plan.existing_config is True
    assert plan.overwrite_required is True
    with pytest.raises(InitError, match="kept"):
        write_plan(plan)
    assert target.read_text(encoding="utf-8") == "version: 1\nactive_profile: default\n"


def test_fresh_write_is_immediately_loadable(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    plan = InitPlanner(ollama_factory=HealthyOllama).plan(SetupAnswers())
    result = write_plan(plan)
    assert result.path == resolve_target_path()
    assert result.path.resolve() == tmp_path / ".tieru/config.yaml"
    settings = load_settings()
    assert settings.config_path == resolve_target_path()
    assert settings.profile == "tieru-local"
    assert settings.fabric_routing_policy == "local_only"


def test_explicit_replacement_creates_timestamped_backup(tmp_path):
    target = tmp_path / "config.yaml"
    original = "version: 1\nactive_profile: default\n"
    target.write_text(original, encoding="utf-8")
    plan = InitPlanner(target, ollama_factory=HealthyOllama).plan(SetupAnswers())
    result = write_plan(
        plan,
        replace_existing=True,
        now=datetime(2026, 8, 11, 12, 30, tzinfo=UTC),
    )
    assert result.backup_path is not None
    assert result.backup_path.name == "config.yaml.bak.20260811T123000000000Z"
    assert result.backup_path.read_text(encoding="utf-8") == original
    assert target.read_text(encoding="utf-8") == plan.yaml_text


def test_failed_atomic_replacement_preserves_original(tmp_path):
    target = tmp_path / "config.yaml"
    original = "version: 1\nactive_profile: default\n"
    target.write_text(original, encoding="utf-8")
    plan = InitPlanner(target, ollama_factory=HealthyOllama).plan(SetupAnswers())

    def fail_replace(_source, _target):
        raise OSError("simulated atomic replacement failure")

    with pytest.raises(InitError, match="atomically"):
        write_plan(plan, replace_existing=True, replace_fn=fail_replace)
    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".tieru-init-*.tmp"))
    assert len(list(tmp_path.glob("config.yaml.bak.*"))) == 1


def test_dry_run_is_noninteractive_and_creates_nothing(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    output: list[str] = []
    result = run_init_cli(
        _args("--dry-run"),
        input_fn=lambda _prompt: pytest.fail("dry-run must not prompt"),
        output_fn=output.append,
        ollama_factory=HealthyOllama,
    )
    rendered = "\n".join(output)
    assert result == 0
    assert "Mode:          local-only" in rendered
    assert "Generated YAML:" in rendered
    assert "No files or directories were changed" in rendered
    assert not (tmp_path / ".tieru").exists()
    assert list(tmp_path.iterdir()) == []


def test_cancel_before_plan_produces_no_mutation(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    output: list[str] = []
    result = run_init_cli(
        _args(),
        input_fn=lambda _prompt: "cancel",
        output_fn=output.append,
        ollama_factory=HealthyOllama,
    )
    assert result == 130
    assert "cancelled" in "\n".join(output)
    assert not (tmp_path / ".tieru").exists()


def test_existing_config_enter_keeps_without_discovery(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".tieru/config.yaml"
    target.parent.mkdir()
    original = "version: 1\nactive_profile: default\n"
    target.write_text(original, encoding="utf-8")

    def forbidden_factory(_endpoint):
        pytest.fail("keeping an existing config should not probe Ollama")

    output: list[str] = []
    result = run_init_cli(
        _args(),
        input_fn=lambda _prompt: "",
        output_fn=output.append,
        ollama_factory=forbidden_factory,
    )
    assert result == 0
    assert target.read_text(encoding="utf-8") == original
    assert "kept" in "\n".join(output)


def test_noninteractive_existing_config_requires_replace_flag(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".tieru/config.yaml"
    target.parent.mkdir()
    target.write_text("sentinel", encoding="utf-8")
    output: list[str] = []
    result = run_init_cli(
        _args("--mode", "local-only", "--yes"),
        input_fn=lambda _prompt: pytest.fail("non-interactive mode must not prompt"),
        output_fn=output.append,
        ollama_factory=HealthyOllama,
    )
    assert result == 2
    assert target.read_text(encoding="utf-8") == "sentinel"
    assert "--replace" in "\n".join(output)


def test_existing_mcp_is_reported_and_never_modified(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    mcp = tmp_path / ".tieru/mcp.json"
    mcp.parent.mkdir()
    original = '{"servers": []}'
    mcp.write_text(original, encoding="utf-8")
    plan = InitPlanner(ollama_factory=HealthyOllama).plan(SetupAnswers())
    assert plan.mcp_detected is True
    assert any("Trust-controlled" in warning for warning in plan.warnings)
    assert mcp.read_text(encoding="utf-8") == original
    assert "mcp" not in plan.config


def test_fake_credentials_never_enter_yaml_or_output(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    fake_secret = "sk-fake-r21-never-print"
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_secret)
    answers = SetupAnswers(
        mode=InitMode.LOCAL_FIRST,
        cloud_candidates=(CloudCandidate("anthropic", "configured-model"),),
    )
    plan = InitPlanner(tmp_path / "config.yaml", ollama_factory=HealthyOllama).plan(answers)
    output = render_plan(plan)
    assert fake_secret not in output
    assert fake_secret not in plan.yaml_text
    assert "configured via ANTHROPIC_API_KEY" in output


def test_cli_surface_parses_supported_modes():
    assert _args().command == "init"
    assert _args("--dry-run").dry_run is True
    args = _args(
        "--mode",
        "advanced",
        "--fabric-policy",
        "balanced",
        "--enable-shadow",
        "--enable-browser",
        "--browser-domain",
        "docs.example.com",
        "--cloud-provider",
        "openai",
        "--cloud-model",
        "configured-model",
        "--non-interactive",
    )
    assert args.mode == "advanced"
    assert args.fabric_policy == "balanced"
    assert args.browser_domain == ["docs.example.com"]
    assert args.non_interactive is True


def test_clean_project_init_load_doctor_is_ready(monkeypatch, tmp_path):
    _clean_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    output: list[str] = []
    result = run_init_cli(
        _args("--mode", "local-only", "--non-interactive"),
        input_fn=lambda _prompt: pytest.fail("non-interactive mode must not prompt"),
        output_fn=output.append,
        ollama_factory=HealthyOllama,
    )
    assert result == 0
    settings = load_settings()
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert settings.config_path == resolve_target_path()
    assert settings.profile == "tieru-local"
    assert report.ready is True
    assert report.overall_status == "READY"
    assert "tieru doctor" in "\n".join(output)
    assert "\n  tieru" in "\n".join(output)

"""R2.0 public Doctor UX, safety, and deterministic readiness regressions."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tieru.__main__ import _parser
from tieru.config import load_settings
from tieru.ops.doctor import (
    DoctorRunner,
    DoctorStatus,
    python_satisfies,
    render_json,
    run_doctor,
)
from tieru.providers.ollama import OllamaError


class HealthyOllama:
    def __init__(self, base_url):
        self.base_url = base_url

    def health(self):
        return {"ok": True, "version": "test-version", "endpoint": self.base_url}

    def models(self):
        return [SimpleNamespace(name="gemma4:e2b")]


class MissingModelOllama(HealthyOllama):
    def models(self):
        return [SimpleNamespace(name="another-model:latest")]


class UnreachableOllama(HealthyOllama):
    def health(self):
        raise OllamaError("connection refused")


def _settings(monkeypatch, tmp_path, profile="ollama-gemma4-e2b"):
    secret_envs = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "MOONSHOT_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER",
        "WHATSAPP_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_ALLOWED_PHONE",
        "WHATSAPP_MAX_BODY_BYTES",
    }
    for name in list(os.environ):
        if name.startswith(("TIERU_", "WAKU_")) or name in secret_envs:
            monkeypatch.delenv(name, raising=False)
    return load_settings({"profile": profile, "home": tmp_path / "home"})


def _check(report, check_id):
    return next(check for check in report.checks if check.id == check_id)


def test_supported_python_passes(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(
        settings=settings, python_version=(3, 12, 7), ollama_factory=HealthyOllama
    )
    assert python_satisfies((3, 12, 7), ">=3.11")
    assert _check(report, "runtime.python").status is DoctorStatus.PASS


def test_unsupported_python_is_required_failure(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(
        settings=settings, python_version=(3, 10, 14), ollama_factory=HealthyOllama
    )
    assert not python_satisfies((3, 10, 14), ">=3.11")
    assert _check(report, "runtime.python").status is DoctorStatus.FAIL
    assert report.exit_code == 1


def test_config_profile_and_source_are_safe(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.config_path = tmp_path / "tieru.yaml"
    settings.config_path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-never-appear")
    report = run_doctor(
        settings=settings,
        selection={"config_explicit": True},
        ollama_factory=HealthyOllama,
    )
    payload = render_json(report)
    assert _check(report, "configuration.profile").detail.startswith("ollama-gemma4-e2b")
    assert "explicit YAML" in _check(report, "configuration.source").detail
    assert "must-never-appear" not in payload


def test_missing_explicit_config_is_actionable_failure(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.config_path = tmp_path / "missing.yaml"
    report = run_doctor(
        settings=settings,
        selection={"config_explicit": True},
        ollama_factory=HealthyOllama,
    )
    check = _check(report, "configuration.source")
    assert check.status is DoctorStatus.FAIL
    assert "remove the explicit config selection" in check.remediation


def test_clean_home_uses_first_run_schema_without_creating_state(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "state.home").status is DoctorStatus.PASS
    state = _check(report, "state.sqlite")
    assert state.status is DoctorStatus.PASS
    assert state.metadata["first_run"] is True
    assert not settings.home.exists()
    assert not (settings.home / "state.db").exists()
    assert not (settings.home / "capsules").exists()


def test_ollama_reachable_and_model_installed_is_ready(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "models.ollama").status is DoctorStatus.PASS
    assert _check(report, "models.role.main").status is DoctorStatus.PASS
    assert _check(report, "models.role.small").status is DoctorStatus.PASS
    assert _check(report, "models.role.judge").status is DoctorStatus.OPTIONAL
    assert report.ready


def test_ollama_unreachable_is_actionable_required_failure(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(settings=settings, ollama_factory=UnreachableOllama)
    check = _check(report, "models.ollama")
    assert check.status is DoctorStatus.FAIL
    assert "Start Ollama" in check.remediation
    assert report.exit_code == 1


def test_missing_ollama_model_recommends_pull(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(settings=settings, ollama_factory=MissingModelOllama)
    check = _check(report, "models.role.main")
    assert check.status is DoctorStatus.FAIL
    assert check.remediation == "Run: ollama pull gemma4:e2b"


def test_hosted_profile_does_not_require_ollama_or_judge(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, profile="default")
    main_key = settings.role("main").api_key_env
    monkeypatch.setenv(main_key, "configured-runtime-key")
    monkeypatch.delenv(settings.role("judge").api_key_env, raising=False)
    report = run_doctor(settings=settings)
    assert _check(report, "models.ollama").status is DoctorStatus.OPTIONAL
    assert _check(report, "models.role.main").status is DoctorStatus.PASS
    assert _check(report, "models.role.small").status is DoctorStatus.PASS
    assert _check(report, "models.role.judge").status is DoctorStatus.OPTIONAL
    assert report.ready


def test_remote_ollama_endpoint_is_not_probed(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.roles = {
        name: replace(role, base_url="https://private-ollama.example/v1")
        for name, role in settings.roles.items()
    }

    def forbidden_factory(_base_url):
        pytest.fail("Doctor must not probe non-loopback Ollama endpoints")

    report = run_doctor(settings=settings, ollama_factory=forbidden_factory)
    assert _check(report, "models.ollama").status is DoctorStatus.WARN
    assert _check(report, "models.role.main").status is DoctorStatus.SKIP
    assert report.ready


def test_fabric_local_only_accepts_local_and_keeps_cloud_optional(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.fabric_enabled = True
    settings.fabric_routing_policy = "local_only"
    settings.fabric_models = {
        "local-main": {
            "provider": "ollama",
            "model": "gemma4:e2b",
            "local": True,
            "roles": ["main", "small"],
        },
        "cloud-fast": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "local": False,
            "roles": ["main", "small"],
        },
    }
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "fabric.summary").status is DoctorStatus.PASS
    assert _check(report, "fabric.candidate.local-main").status is DoctorStatus.PASS
    assert _check(report, "fabric.candidate.cloud-fast").status is DoctorStatus.OPTIONAL
    assert report.ready


def test_trust_validates_without_executing_actions(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.trust_policy = {
        "capabilities": {"process_execution": {"mode": "confirm", "commands": ["python"]}}
    }
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "trust.policy").status is DoctorStatus.PASS


def test_malformed_trust_policy_is_reported(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.trust_policy = {"capabilities": {"unknown_power": "allow"}}
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "trust.policy").status is DoctorStatus.FAIL
    assert not report.ready


def test_malformed_trust_scope_is_reported(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.trust_policy = {
        "capabilities": {"process_execution": {"mode": "allow", "commands": "python"}}
    }
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "trust.policy").status is DoctorStatus.FAIL


def test_locked_gateways_warn_without_exposing_tokens(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.telegram_token = "telegram-secret-value"
    settings.whatsapp_token = "whatsapp-secret-value"
    settings.whatsapp_phone_number_id = "phone-id"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret-value")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret-value")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret-value")
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "gateway.telegram").status is DoctorStatus.WARN
    assert _check(report, "gateway.discord").status is DoctorStatus.WARN
    assert _check(report, "gateway.whatsapp").status is DoctorStatus.WARN
    payload = render_json(report)
    for secret in (
        "telegram-secret-value",
        "whatsapp-secret-value",
        "discord-secret-value",
        "app-secret-value",
        "verify-secret-value",
    ):
        assert secret not in payload
    assert report.exit_code == 0


def test_mcp_is_inspected_without_starting_or_spawning(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.home.mkdir()
    (settings.home / "mcp.json").write_text(
        json.dumps({"servers": [{
            "name": "demo",
            "command": "python",
            "args": ["server.py", "--token", "mcp-secret-value"],
            "env": {"API_KEY": "mcp-secret-value"},
        }]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Doctor must not spawn MCP processes"),
    )
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "mcp.config").status is DoctorStatus.PASS
    assert _check(report, "mcp.server.demo").status is DoctorStatus.PASS
    assert "mcp-secret-value" not in render_json(report)


def test_malformed_mcp_config_warns_without_process_side_effect(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.home.mkdir()
    (settings.home / "mcp.json").write_text("{not-json", encoding="utf-8")
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "mcp.config").status is DoctorStatus.WARN
    assert report.ready


def test_browser_disabled_is_optional_and_enabled_missing_is_actionable(
    monkeypatch, tmp_path
):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "browser.support").status is DoctorStatus.OPTIONAL

    settings.browser_enabled = True
    monkeypatch.setattr(DoctorRunner, "_playwright_dependency", staticmethod(lambda: False))
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    check = _check(report, "browser.support")
    assert check.status is DoctorStatus.WARN
    assert "playwright install chromium" in check.remediation


def test_invalid_user_skill_warns_without_deletion(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    skill = settings.home / "skills" / "broken" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("invalid skill", encoding="utf-8")
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    check = _check(report, "skills.system")
    assert check.status is DoctorStatus.WARN
    assert skill.read_text(encoding="utf-8") == "invalid skill"


def test_shadow_disabled_and_capsule_check_have_no_export_side_effects(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    assert _check(report, "shadow.system").status is DoctorStatus.OPTIONAL
    assert _check(report, "capsule.system").status is DoctorStatus.PASS
    assert not (settings.home / "capsules").exists()


def test_json_contract_is_stable_and_share_safe(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "json-secret-value")
    report = run_doctor(settings=settings, ollama_factory=HealthyOllama)
    payload = json.loads(render_json(report))
    assert set(payload) == {"overall_status", "ready", "checks"}
    args = _parser().parse_args(["doctor", "--json"])
    assert args.command == "doctor" and args.json is True
    assert payload["overall_status"] == "READY"
    assert payload["ready"] is True
    assert all(
        set(check) == {
            "id", "category", "status", "title", "detail", "remediation", "required", "metadata"
        }
        for check in payload["checks"]
    )
    assert "json-secret-value" not in json.dumps(payload)

"""Conservative first-run configuration for Tieru.

Init configures a small, validated YAML file. It never installs software,
downloads models, starts services, creates credentials, or grants authority.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from tieru.config import (
    BUILTIN_PROVIDERS,
    DEFAULT_OLLAMA_URL,
    VERIFIED_GEMMA_MODEL,
    ConfigError,
    Settings,
    load_settings,
)
from tieru.memory.personal import redact_secrets
from tieru.ops.doctor import _is_loopback_url
from tieru.providers.ollama import OllamaIntegration

DEFAULT_CONFIG_PATH = Path(".tieru/config.yaml")
PROFILE_NAMES = {
    "local-only": "tieru-local",
    "local-first": "tieru-local-first",
    "advanced": "tieru-advanced",
}
FABRIC_POLICIES = ("local_only", "local_first", "balanced", "quality_first")
_SAFE_MODEL = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SAFE_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class InitError(ConfigError):
    """A safe first-run configuration error."""


class InitCancelled(Exception):
    """The user cancelled before any configuration mutation."""


class InitMode(StrEnum):
    LOCAL_ONLY = "local-only"
    LOCAL_FIRST = "local-first"
    ADVANCED = "advanced"


@dataclass(frozen=True)
class CloudCandidate:
    provider: str
    model: str


@dataclass(frozen=True)
class SetupAnswers:
    mode: InitMode = InitMode.LOCAL_ONLY
    local_model: str = VERIFIED_GEMMA_MODEL
    fabric_policy: str = ""
    shadow_enabled: bool = False
    browser_enabled: bool = False
    browser_allowed_domains: tuple[str, ...] = ()
    cloud_candidates: tuple[CloudCandidate, ...] = ()

    @property
    def resolved_fabric_policy(self) -> str:
        if self.mode is InitMode.LOCAL_ONLY:
            return "local_only"
        if self.mode is InitMode.LOCAL_FIRST:
            return "local_first"
        return self.fabric_policy or "local_first"

    def validate(self) -> None:
        model = self.local_model.strip()
        if not _SAFE_MODEL.fullmatch(model):
            raise InitError("Local model must be a non-empty, single-line model identifier")
        policy = self.resolved_fabric_policy
        if policy not in FABRIC_POLICIES:
            raise InitError(
                "Fabric policy must be one of: " + ", ".join(FABRIC_POLICIES)
            )
        if self.mode is InitMode.LOCAL_ONLY and self.fabric_policy not in ("", "local_only"):
            raise InitError("Local-only mode requires the local_only Fabric policy")
        if self.mode is InitMode.LOCAL_FIRST and self.fabric_policy not in ("", "local_first"):
            raise InitError("Local-first mode requires the local_first Fabric policy")
        if self.mode is InitMode.LOCAL_ONLY and self.cloud_candidates:
            raise InitError("Local-only mode cannot contain cloud candidates")
        if self.browser_allowed_domains and not self.browser_enabled:
            raise InitError("Browser domains require browser automation to be enabled")
        for domain in self.browser_allowed_domains:
            if not _SAFE_DOMAIN.fullmatch(domain):
                raise InitError(
                    f"Browser domain {domain!r} must be a hostname without a URL or path"
                )
        for candidate in self.cloud_candidates:
            provider = BUILTIN_PROVIDERS.get(candidate.provider)
            if provider is None or provider.keyless:
                raise InitError(
                    f"Cloud candidate provider {candidate.provider!r} is not supported"
                )
            if not _SAFE_MODEL.fullmatch(candidate.model.strip()):
                raise InitError(
                    f"Cloud model for {candidate.provider!r} must be a single-line identifier"
                )


@dataclass(frozen=True)
class OllamaDiscovery:
    endpoint: str = DEFAULT_OLLAMA_URL
    reachable: bool = False
    version: str = ""
    installed_models: tuple[str, ...] = ()
    detail: str = "Ollama was not detected"

    def has_model(self, model: str) -> bool:
        return model in self.installed_models


@dataclass(frozen=True)
class InitPlan:
    target_path: Path
    mode: InitMode
    provider: str
    local_model: str
    fabric_policy: str
    shadow_enabled: bool
    browser_enabled: bool
    browser_allowed_domains: tuple[str, ...]
    cloud_candidates: tuple[CloudCandidate, ...]
    warnings: tuple[str, ...]
    existing_config: bool
    overwrite_required: bool
    config: dict[str, Any]
    yaml_text: str
    ollama: OllamaDiscovery
    mcp_detected: bool = False


@dataclass(frozen=True)
class InitWriteResult:
    path: Path
    backup_path: Path | None = None


def resolve_target_path(
    explicit: str | Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Follow the loader's explicit/Tieru/legacy/project-local precedence."""
    environment = os.environ if environ is None else environ
    configured = explicit or environment.get("TIERU_CONFIG") or environment.get("WAKU_CONFIG")
    if configured:
        return Path(configured).expanduser()
    for candidate in (DEFAULT_CONFIG_PATH, Path(".tieru/config.yml")):
        if candidate.exists():
            return candidate
    return DEFAULT_CONFIG_PATH


def discover_ollama(
    *,
    endpoint: str = DEFAULT_OLLAMA_URL,
    ollama_factory: Callable[[str], Any] = OllamaIntegration,
) -> OllamaDiscovery:
    """Perform only Doctor's bounded loopback Ollama health/model inspection."""
    if not _is_loopback_url(endpoint):
        return OllamaDiscovery(
            endpoint=endpoint,
            detail="Remote Ollama endpoints are not probed during Init",
        )
    try:
        client = ollama_factory(endpoint)
        health = client.health()
        raw_models = client.models()
        models = tuple(
            dict.fromkeys(
                str(getattr(item, "name", "")).strip()
                for item in raw_models[:100]
                if str(getattr(item, "name", "")).strip()
            )
        )
        version = str(health.get("version", "")).strip()[:100]
        return OllamaDiscovery(
            endpoint=endpoint,
            reachable=True,
            version=version,
            installed_models=models,
            detail="Ollama detected",
        )
    except Exception:  # local discovery must remain useful when Ollama is absent
        return OllamaDiscovery(endpoint=endpoint)


def _candidate_config(answers: SetupAnswers) -> dict[str, dict[str, Any]]:
    if not answers.cloud_candidates:
        return {}
    candidates: dict[str, dict[str, Any]] = {
        "local-main": {
            "provider": "ollama",
            "model": answers.local_model.strip(),
            "local": True,
            "roles": ["main", "small", "judge"],
            "capabilities": {"text": True, "tool_calling": True, "long_context": True},
            "cost_tier": "free",
            "preference": 1.0,
        }
    }
    for index, candidate in enumerate(answers.cloud_candidates, start=1):
        candidates[f"cloud-{index}-{candidate.provider}"] = {
            "provider": candidate.provider,
            "model": candidate.model.strip(),
            "local": False,
            "roles": ["main", "small"],
            "capabilities": {"text": True, "tool_calling": True},
            "enabled": True,
            "preference": 0.5,
        }
    return candidates


def build_config(answers: SetupAnswers) -> dict[str, Any]:
    """Create the minimal existing-schema config for the selected answers."""
    answers.validate()
    profile = PROFILE_NAMES[answers.mode.value]
    role = {"provider": "ollama", "model": answers.local_model.strip()}
    fabric: dict[str, Any] = {
        "enabled": True,
        "routing_policy": answers.resolved_fabric_policy,
    }
    candidates = _candidate_config(answers)
    if candidates:
        fabric["models"] = candidates
    config: dict[str, Any] = {
        "version": 1,
        "active_profile": profile,
        "profiles": {
            profile: {
                "main": dict(role),
                "small": dict(role),
                # The loader requires all roles; a local judge adds no credential.
                "judge": dict(role),
            }
        },
        "fabric": fabric,
        "shadow_enabled": answers.shadow_enabled,
        "browser_enabled": answers.browser_enabled,
    }
    if answers.browser_enabled:
        config["browser_allowed_domains"] = list(answers.browser_allowed_domains)
    return config


def serialize_config(config: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(config), sort_keys=False, allow_unicode=True)


@contextmanager
def _without_config_environment():
    """Make validation describe the generated YAML, then restore the environment."""
    removed = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(("TIERU_", "WAKU_"))
    }
    try:
        for name in removed:
            os.environ.pop(name, None)
        yield
    finally:
        os.environ.update(removed)


def validate_generated_config(yaml_text: str, expected: SetupAnswers) -> Settings:
    """Validate serialized output through Tieru's actual public loader."""
    with tempfile.TemporaryDirectory(prefix="tieru-init-validate-") as raw_dir:
        root = Path(raw_dir)
        path = root / "config.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        try:
            with _without_config_environment():
                settings = load_settings(
                    {
                        "config_path": path,
                        "profile": PROFILE_NAMES[expected.mode.value],
                        "home": root / "home",
                    }
                )
        except ConfigError as exc:
            raise InitError(f"Generated configuration failed Tieru validation: {exc}") from exc
    if settings.fabric_routing_policy != expected.resolved_fabric_policy:
        raise InitError("Generated Fabric policy did not survive config loading")
    if any(
        settings.role(role).provider != "ollama"
        or settings.role(role).model != expected.local_model.strip()
        for role in ("main", "small", "judge")
    ):
        raise InitError("Generated local model roles did not survive config loading")
    return settings


class InitPlanner:
    def __init__(
        self,
        target_path: Path = DEFAULT_CONFIG_PATH,
        *,
        ollama_factory: Callable[[str], Any] = OllamaIntegration,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.target_path = Path(target_path).expanduser()
        self.ollama_factory = ollama_factory
        self.environ = os.environ if environ is None else environ

    def plan(
        self,
        answers: SetupAnswers,
        *,
        discovery: OllamaDiscovery | None = None,
    ) -> InitPlan:
        answers.validate()
        config = build_config(answers)
        yaml_text = serialize_config(config)
        validate_generated_config(yaml_text, answers)
        ollama = discovery or discover_ollama(ollama_factory=self.ollama_factory)
        warnings: list[str] = []
        if not ollama.reachable:
            warnings.append(
                "Ollama was not detected. Install/start Ollama, then run: "
                f"ollama pull {answers.local_model.strip()}"
            )
        elif not ollama.has_model(answers.local_model.strip()):
            warnings.append(
                f"{answers.local_model.strip()} is not installed. Run: "
                f"ollama pull {answers.local_model.strip()}"
            )
        if answers.browser_enabled:
            warnings.append(
                "Browser automation requires the optional Playwright dependencies and "
                "browser binary; Init does not install them. Trust remains authoritative."
            )
            if not answers.browser_allowed_domains:
                warnings.append(
                    "Browser automation has an empty domain allowlist and cannot navigate yet."
                )
        mcp_home = Path(
            self.environ.get("TIERU_HOME") or self.environ.get("WAKU_HOME") or ".tieru"
        ).expanduser()
        mcp_detected = (mcp_home / "mcp.json").is_file()
        if mcp_detected:
            warnings.append(
                "Existing MCP configuration detected; Init leaves it untouched and MCP "
                "process startup remains Trust-controlled."
            )
        return InitPlan(
            target_path=self.target_path,
            mode=answers.mode,
            provider="ollama",
            local_model=answers.local_model.strip(),
            fabric_policy=answers.resolved_fabric_policy,
            shadow_enabled=answers.shadow_enabled,
            browser_enabled=answers.browser_enabled,
            browser_allowed_domains=answers.browser_allowed_domains,
            cloud_candidates=answers.cloud_candidates,
            warnings=tuple(warnings),
            existing_config=self.target_path.exists(),
            overwrite_required=self.target_path.exists(),
            config=config,
            yaml_text=yaml_text,
            ollama=ollama,
            mcp_detected=mcp_detected,
        )


def _backup_path(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{stamp}.{counter}")
        counter += 1
    return candidate


def write_plan(
    plan: InitPlan,
    *,
    replace_existing: bool = False,
    replace_fn: Callable[[str, str], Any] = os.replace,
    now: datetime | None = None,
) -> InitWriteResult:
    """Write a validated plan atomically, optionally backing up an existing file."""
    validate_generated_config(
        plan.yaml_text,
        SetupAnswers(
            mode=plan.mode,
            local_model=plan.local_model,
            fabric_policy=plan.fabric_policy,
            shadow_enabled=plan.shadow_enabled,
            browser_enabled=plan.browser_enabled,
            browser_allowed_domains=plan.browser_allowed_domains,
            cloud_candidates=plan.cloud_candidates,
        ),
    )
    target = plan.target_path
    if target.exists() and not target.is_file():
        raise InitError(f"Configuration target is not a file: {target}")
    if target.exists() and not replace_existing:
        raise InitError(
            f"Existing Tieru configuration was kept: {target}. Use --replace explicitly."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        backup = _backup_path(target, now)
        try:
            with target.open("rb") as source, backup.open("xb") as destination:
                while chunk := source.read(64 * 1024):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as exc:
            backup.unlink(missing_ok=True)
            raise InitError(f"Could not back up existing configuration: {exc}") from exc

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=".tieru-init-",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(plan.yaml_text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_fn(str(temporary), str(target))
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise InitError(f"Could not write configuration atomically: {exc}") from exc
    return InitWriteResult(path=target, backup_path=backup)


def _safe(value: object) -> str:
    return redact_secrets(str(value)).replace("\r", " ").replace("\n", " ")


def render_plan(plan: InitPlan, *, include_yaml: bool = True) -> str:
    cloud = "disabled"
    if plan.cloud_candidates:
        cloud = ", ".join(
            f"{item.provider}/{item.model}" for item in plan.cloud_candidates
        )
    lines = [
        "Configuration plan",
        "",
        f"Target:        {_safe(plan.target_path.resolve(strict=False))}",
        f"Mode:          {plan.mode.value}",
        f"Provider:      {plan.provider}",
        f"Local model:   {plan.local_model}",
        f"Model Fabric:  {plan.fabric_policy}",
        f"Shadow:        {'enabled' if plan.shadow_enabled else 'disabled'}",
        f"Browser:       {'enabled' if plan.browser_enabled else 'disabled'}",
        f"Cloud:         {cloud}",
        f"Existing:      {'yes (replacement required)' if plan.existing_config else 'no'}",
        "",
        f"Ollama:        {'PASS detected' if plan.ollama.reachable else 'WARN not detected'}",
    ]
    if plan.ollama.version:
        lines.append(f"Ollama version: {_safe(plan.ollama.version)}")
    if plan.ollama.installed_models:
        lines.append(
            "Installed local models: "
            + ", ".join(_safe(model) for model in plan.ollama.installed_models[:20])
        )
    for candidate in plan.cloud_candidates:
        provider = BUILTIN_PROVIDERS[candidate.provider]
        configured = bool(os.getenv(provider.api_key_env))
        lines.append(
            f"{candidate.provider} credential: "
            f"{'configured' if configured else 'not configured'} via {provider.api_key_env}"
        )
    for warning in plan.warnings:
        lines.append(f"Warning: {_safe(warning)}")
    if include_yaml:
        lines.extend(("", "Generated YAML:", plan.yaml_text.rstrip()))
    return "\n".join(lines)


def _answer(
    prompt: str,
    *,
    input_fn: Callable[[str], str],
    default: str = "",
) -> str:
    try:
        value = input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise InitCancelled from exc
    if value.lower() in {"cancel", "quit", "q"}:
        raise InitCancelled
    return value or default


def _yes_no(
    prompt: str,
    *,
    input_fn: Callable[[str], str],
    default: bool = False,
) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    value = _answer(prompt + suffix, input_fn=input_fn).lower()
    if not value:
        return default
    if value in {"y", "yes"}:
        return True
    if value in {"n", "no"}:
        return False
    raise InitError("Expected yes or no")


def _interactive_answers(
    *,
    args: Any,
    discovery: OllamaDiscovery,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> SetupAnswers:
    mode_value = args.mode
    if mode_value is None:
        output_fn("\nHow do you want Tieru to run?")
        output_fn("[1] Local only (recommended)\n[2] Local first\n[3] Advanced")
        selected = _answer("> ", input_fn=input_fn, default="1")
        modes = {"1": "local-only", "2": "local-first", "3": "advanced"}
        if selected not in modes:
            raise InitError("Setup mode must be 1, 2, or 3")
        mode_value = modes[selected]
    mode = InitMode(mode_value)

    model = args.model
    if model is None:
        model = _answer(
            f"Local model [{VERIFIED_GEMMA_MODEL}]: ",
            input_fn=input_fn,
            default=VERIFIED_GEMMA_MODEL,
        )
    if (
        (not discovery.reachable or not discovery.has_model(model))
        and not _yes_no(
            f"{model} is not currently confirmed installed. Configure it anyway?",
            input_fn=input_fn,
            default=True,
        )
    ):
        raise InitCancelled

    if bool(args.cloud_provider) != bool(args.cloud_model):
        raise InitError("--cloud-provider and --cloud-model must be provided together")
    cloud: tuple[CloudCandidate, ...] = ()
    configure_cloud = bool(args.cloud_provider)
    if mode is not InitMode.LOCAL_ONLY and not configure_cloud:
        configure_cloud = _yes_no(
            "Configure one optional cloud candidate now?", input_fn=input_fn
        )
    if configure_cloud:
        if mode is InitMode.LOCAL_ONLY:
            raise InitError("Local-only mode cannot contain cloud candidates")
        if args.cloud_provider:
            cloud = (CloudCandidate(args.cloud_provider, args.cloud_model),)
        else:
            names = tuple(
                name for name, provider in BUILTIN_PROVIDERS.items() if not provider.keyless
            )
            output_fn("Supported providers: " + ", ".join(names))
            provider_name = _answer("Cloud provider: ", input_fn=input_fn)
            provider = BUILTIN_PROVIDERS.get(provider_name)
            if provider is None or provider.keyless:
                raise InitError(f"Unsupported cloud provider: {provider_name}")
            cloud_model = _answer(
                f"Configured model ID [{provider.default_model}]: ",
                input_fn=input_fn,
                default=provider.default_model,
            )
            cloud = (CloudCandidate(provider_name, cloud_model),)
            output_fn(f"Credential stays in {provider.api_key_env}; no value is requested.")

    policy = args.fabric_policy or ""
    if mode is InitMode.ADVANCED and not policy:
        policy = _answer(
            "Fabric policy [local_first]: ", input_fn=input_fn, default="local_first"
        ).replace("-", "_")
    shadow = args.enable_shadow or _yes_no(
        "Enable Shadow (observe repeated successful workflows)?", input_fn=input_fn
    )
    browser = args.enable_browser or _yes_no(
        "Enable restricted browser automation?", input_fn=input_fn
    )
    domains = tuple(args.browser_domain or ())
    if browser and not domains:
        raw_domains = _answer(
            "Allowed browser domains (comma-separated, blank for none): ", input_fn=input_fn
        )
        domains = tuple(
            value.strip().lower() for value in raw_domains.split(",") if value.strip()
        )
    return SetupAnswers(
        mode=mode,
        local_model=model,
        fabric_policy=policy,
        shadow_enabled=shadow,
        browser_enabled=browser,
        browser_allowed_domains=domains,
        cloud_candidates=cloud,
    )


def _noninteractive_answers(args: Any) -> SetupAnswers:
    mode = InitMode(args.mode or InitMode.LOCAL_ONLY.value)
    cloud: tuple[CloudCandidate, ...] = ()
    if bool(args.cloud_provider) != bool(args.cloud_model):
        raise InitError("--cloud-provider and --cloud-model must be provided together")
    if args.cloud_provider:
        cloud = (CloudCandidate(args.cloud_provider, args.cloud_model),)
    return SetupAnswers(
        mode=mode,
        local_model=args.model or VERIFIED_GEMMA_MODEL,
        fabric_policy=(args.fabric_policy or "").replace("-", "_"),
        shadow_enabled=args.enable_shadow,
        browser_enabled=args.enable_browser,
        browser_allowed_domains=tuple(args.browser_domain or ()),
        cloud_candidates=cloud,
    )


def run_init_cli(
    args: Any,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    ollama_factory: Callable[[str], Any] = OllamaIntegration,
) -> int:
    """Run the shared interactive/non-interactive/dry-run Init flow."""
    explicit_target = getattr(args, "config_path", None)
    target = resolve_target_path(explicit_target)
    existing = target.exists()
    noninteractive = bool(args.non_interactive or args.yes or args.dry_run)
    replace_existing = bool(args.replace)
    preview_existing = False
    try:
        output_fn("Tieru Setup")
        if existing and not args.dry_run and not replace_existing:
            if noninteractive:
                raise InitError(
                    f"Existing Tieru configuration was kept: {target}. "
                    "Use --replace explicitly to replace it."
                )
            output_fn(f"\nExisting Tieru configuration found:\n  {_safe(target.resolve())}")
            output_fn(
                "\n[1] Keep existing configuration\n"
                "[2] Preview a new configuration\n"
                "[3] Replace configuration"
            )
            action = _answer("> ", input_fn=input_fn, default="1")
            if action == "1":
                output_fn("Existing configuration kept. No changes were made.")
                return 0
            if action == "2":
                preview_existing = True
            elif action == "3":
                replace_existing = True
            else:
                raise InitError("Existing-config action must be 1, 2, or 3")

        discovery = discover_ollama(ollama_factory=ollama_factory)
        output_fn(
            "\nOllama\n"
            + ("PASS detected" if discovery.reachable else "WARN not detected")
        )
        if discovery.installed_models:
            output_fn(
                "Installed local models: "
                + ", ".join(_safe(name) for name in discovery.installed_models[:20])
            )
            output_fn(f"Verified Tieru model: {VERIFIED_GEMMA_MODEL} (recommended)")

        if noninteractive:
            answers = _noninteractive_answers(args)
        else:
            answers = _interactive_answers(
                args=args,
                discovery=discovery,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        planner = InitPlanner(target, ollama_factory=ollama_factory)
        plan = planner.plan(answers, discovery=discovery)
        output_fn("\n" + render_plan(plan))

        if args.dry_run or preview_existing:
            output_fn("\nDry-run complete. No files or directories were changed.")
            return 0
        if existing and replace_existing and not args.yes:
            confirmation = _answer(
                "Type REPLACE to back up and replace the existing configuration: ",
                input_fn=input_fn,
            )
            if confirmation != "REPLACE":
                raise InitCancelled
        elif not noninteractive and not _yes_no(
            "Write configuration?", input_fn=input_fn, default=True
        ):
            raise InitCancelled

        result = write_plan(plan, replace_existing=replace_existing)
        output_fn(f"\nConfiguration created:\n  {_safe(result.path.resolve())}")
        if result.backup_path is not None:
            output_fn(f"Backup created:\n  {_safe(result.backup_path.resolve())}")
        default_targets = {
            DEFAULT_CONFIG_PATH.resolve(strict=False),
            Path(".tieru/config.yml").resolve(strict=False),
        }
        if result.path.resolve(strict=False) in default_targets or not explicit_target:
            doctor_command = "tieru doctor"
            run_command = "tieru"
        else:
            safe_target = _safe(result.path.resolve(strict=False)).replace('"', '\\"')
            doctor_command = f'tieru --config "{safe_target}" doctor'
            run_command = f'tieru --config "{safe_target}"'
        output_fn(f"\nNext steps:\n\n  {doctor_command}\n  {run_command}")
        return 0
    except InitCancelled:
        output_fn("\nTieru Init cancelled. No configuration changes were made.")
        return 130
    except InitError as exc:
        output_fn(f"\nTieru Init stopped: {_safe(exc)}")
        return 2

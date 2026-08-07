"""Tieru's single configuration boundary.

Precedence is explicit and testable:

    CLI override > TIERU_* > WAKU_* fallback > YAML > built-in defaults

YAML contains non-secret provider/profile metadata. Credentials are resolved
only from environment variables when a model client is created.
"""

from __future__ import annotations

import os
import shutil
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import find_dotenv, load_dotenv

ROLE_NAMES = ("main", "small", "judge")
PROTOCOLS = ("anthropic", "openai")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/v1"
VERIFIED_GEMMA_MODEL = "gemma4:e2b"


class ConfigError(ValueError):
    """A configuration error safe to show directly to the user."""


class TieruCompatibilityWarning(FutureWarning):
    """A deprecated Waku contract was selected."""


def _load_env() -> str:
    """Load the nearest .env by searching upward from the working directory."""
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path


DOTENV_PATH = _load_env()


@dataclass(frozen=True)
class ProviderConfig:
    protocol: str
    api_key_env: str
    base_url: str | None
    default_model: str
    default_small_model: str
    catalog_url: str | None = None
    flagship: str = ""
    fast: str = ""
    key_url: str = ""
    keyless: bool = False

    # Compatibility properties used by the existing dashboard/catalog surface.
    @property
    def kind(self) -> str:
        return self.protocol

    @property
    def key_env(self) -> str:
        return self.api_key_env

    @property
    def model(self) -> str:
        return self.default_model

    @property
    def small_model(self) -> str:
        return self.default_small_model

    def default_pair(self) -> list[str]:
        pair = [self.flagship or self.default_model, self.fast or self.default_small_model]
        return list(dict.fromkeys(model for model in pair if model))


BUILTIN_PROVIDERS: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(
        "anthropic", "ANTHROPIC_API_KEY", None,
        "claude-sonnet-5", "claude-haiku-4-5-20251001",
        "https://api.anthropic.com/v1/models", "claude-opus-4-8", "claude-sonnet-5",
        "https://console.anthropic.com/settings/keys",
    ),
    "openai": ProviderConfig(
        "openai", "OPENAI_API_KEY", None,
        "gpt-5.3-chat-latest", "gpt-4.1-mini",
        "https://api.openai.com/v1/models", key_url="https://platform.openai.com/api-keys",
    ),
    "openrouter": ProviderConfig(
        "openai", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
        "nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-26b-a4b-it:free",
        key_url="https://openrouter.ai/keys",
    ),
    "gemini": ProviderConfig(
        "openai", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-3.5-flash", "gemini-3.1-flash-lite",
        flagship="gemini-3.1-pro-preview", fast="gemini-3.5-flash",
        key_url="https://aistudio.google.com/apikey",
    ),
    "deepseek": ProviderConfig(
        "openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com",
        "deepseek-v4-pro", "deepseek-v4-pro",
        key_url="https://platform.deepseek.com/api_keys",
    ),
    "minimax": ProviderConfig(
        "anthropic", "MINIMAX_API_KEY", "https://api.minimaxi.com/anthropic",
        "MiniMax-M3", "MiniMax-M2",
        key_url="https://platform.minimaxi.com/user-center/basic-information",
    ),
    "kimi": ProviderConfig(
        "anthropic", "MOONSHOT_API_KEY", "https://api.moonshot.ai/anthropic",
        "kimi-k3", "kimi-k2.6", "https://api.moonshot.ai/v1/models",
        "kimi-k3", "kimi-k2.7-code-highspeed",
        "https://platform.moonshot.ai/console/api-keys",
    ),
    "glm": ProviderConfig(
        "anthropic", "ZHIPU_API_KEY", "https://api.z.ai/api/anthropic",
        "glm-5.2", "glm-5-turbo", key_url="https://z.ai/manage-apikey/apikey-list",
    ),
    "xai": ProviderConfig(
        "openai", "XAI_API_KEY", "https://api.x.ai/v1", "grok-4", "grok-4-fast",
        "https://api.x.ai/v1/models", key_url="https://console.x.ai",
    ),
    "opencode_zen": ProviderConfig(
        "openai", "OPENCODE_ZEN_API_KEY", "https://opencode.ai/zen/v1",
        "deepseek-v4-flash-free", "deepseek-v4-flash-free",
        key_url="https://opencode.ai/zen",
    ),
    "opencode_go": ProviderConfig(
        "openai", "OPENCODE_GO_API_KEY", "https://opencode.ai/zen/go/v1",
        "deepseek-v4-flash", "deepseek-v4-flash", key_url="https://opencode.ai/zen",
    ),
    "ollama": ProviderConfig(
        "openai", "", DEFAULT_OLLAMA_URL, VERIFIED_GEMMA_MODEL, VERIFIED_GEMMA_MODEL,
        flagship=VERIFIED_GEMMA_MODEL, fast=VERIFIED_GEMMA_MODEL, keyless=True,
    ),
}


@dataclass(frozen=True)
class ModelRole:
    provider: str
    protocol: str
    model: str
    base_url: str | None = None
    api_key_env: str = ""
    options: dict[str, Any] = field(default_factory=dict)


def _builtin_profiles() -> dict[str, dict[str, dict[str, Any]]]:
    anthropic = BUILTIN_PROVIDERS["anthropic"]
    openai = BUILTIN_PROVIDERS["openai"]
    ollama = BUILTIN_PROVIDERS["ollama"]
    return {
        "default": {
            "main": {"provider": "anthropic", "model": anthropic.default_model},
            "small": {"provider": "anthropic", "model": anthropic.default_small_model},
            "judge": {"provider": "openai", "model": openai.default_model},
        },
        "ollama-gemma4-e2b": {
            role: {"provider": "ollama", "model": ollama.default_model}
            for role in ROLE_NAMES
        },
    }


@dataclass
class Settings:
    # Legacy constructor fields remain accepted for downstream compatibility.
    provider: str = "anthropic"
    api_key: str = field(default="", repr=False)
    base_url: str | None = None
    model: str = ""
    small_model: str = ""
    home: Path = field(default_factory=lambda: Path(".tieru"))
    max_iterations: int = 10
    max_tokens: int = 8192
    history_turns: int = 12
    consolidate_every: int = 6
    retrieval_top_k: int = 4
    semantic_store: str = "sqlite"
    episodic_store: str = "sqlite"
    embedding_model: str = ""
    memory_write_policy: str = "explicit"
    memory_max_records: int = 10000
    tool_permissions: dict[str, Any] = field(default_factory=dict)
    browser_enabled: bool = False
    browser_allowed_domains: tuple[str, ...] = ()
    browser_allow_local_fixture: bool = False
    browser_timeout_seconds: int = 15
    browser_max_actions: int = 20
    apple_calendar: bool = False
    google_calendar: bool = False
    google_calendar_id: str = "primary"
    apple_tools: bool = False
    gh_tool: bool = False
    gh_repo: str = ""
    experimental: bool = False
    graph_workflows: bool = False
    telegram_token: str = field(default="", repr=False)
    whatsapp_token: str = field(default="", repr=False)
    whatsapp_phone_number_id: str = ""
    otel_endpoint: str = ""
    profile: str = "default"
    roles: dict[str, ModelRole] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(
        default_factory=lambda: dict(BUILTIN_PROVIDERS)
    )
    config_path: Path | None = None
    compatibility_mode: bool = False
    compatibility_sources: tuple[str, ...] = ()
    llm_timeout: float = 120.0
    judge_concurrency: int = 2
    role_api_keys: dict[str, str] = field(default_factory=dict, repr=False)

    def role(self, name: str) -> ModelRole:
        if name not in ROLE_NAMES:
            raise ConfigError(f"Unknown model role '{name}'; expected one of: {', '.join(ROLE_NAMES)}")
        if name in self.roles:
            return self.roles[name]
        provider_name = self.provider
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ConfigError(f"Unknown provider '{provider_name}'")
        model = self.model if name == "main" else self.small_model
        if name == "judge":
            provider_name = "openai"
            provider = self.providers[provider_name]
            model = provider.default_model
        model = model or (
            provider.default_model if name != "small" else provider.default_small_model
        )
        return ModelRole(
            provider_name, provider.protocol, model, self.base_url or provider.base_url,
            provider.api_key_env,
        )

    def secret_for(self, role_name: str) -> str:
        role = self.role(role_name)
        if self.role_api_keys.get(role_name):
            return self.role_api_keys[role_name].strip()
        if self.api_key and (role_name == "main" or role.provider == self.provider):
            return self.api_key.strip()
        return (os.getenv(role.api_key_env, "") if role.api_key_env else "").strip()

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "traces").mkdir(exist_ok=True)
        (self.home / "outbox").mkdir(exist_ok=True)
        return self.home

    def redacted(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "home": str(self.home),
            "config_path": str(self.config_path) if self.config_path else "",
            "compatibility_mode": self.compatibility_mode,
            "memory": {
                "write_policy": self.memory_write_policy,
                "max_records": self.memory_max_records,
                "semantic_store": self.semantic_store,
                "embedding_model": self.embedding_model,
            },
            "tool_permissions": self.tool_permissions,
            "browser": {
                "enabled": self.browser_enabled,
                "allowed_domains": list(self.browser_allowed_domains),
                "allow_local_fixture": self.browser_allow_local_fixture,
                "timeout_seconds": self.browser_timeout_seconds,
                "max_actions": self.browser_max_actions,
            },
            "roles": {
                name: {
                    "provider": role.provider,
                    "protocol": role.protocol,
                    "model": role.model,
                    "base_url": role.base_url or "",
                    "key_set": self.providers[role.provider].keyless
                    or bool(self.secret_for(name)),
                }
                for name, role in self.roles.items()
            },
        }


def _warn_legacy(message: str) -> None:
    warnings.warn(message, TieruCompatibilityWarning, stacklevel=3)


def tieru_env(name: str, default: str | None = None) -> str | None:
    """Read one canonical Tieru option with a deprecated Waku fallback."""
    suffix = name.removeprefix("TIERU_").removeprefix("WAKU_")
    canonical = f"TIERU_{suffix}"
    legacy = f"WAKU_{suffix}"
    if canonical in os.environ:
        return os.environ[canonical]
    if legacy in os.environ:
        _warn_legacy(
            f"{legacy} is deprecated; use {canonical}. No data was moved or merged."
        )
        return os.environ[legacy]
    return default


def _first_env(primary: tuple[str, ...], legacy: tuple[str, ...], used: set[str]) -> str | None:
    for name in primary:
        if name in os.environ:
            return os.environ[name]
    for name in legacy:
        if name in os.environ:
            used.add(name)
            return os.environ[name]
    return None


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("", "0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {value!r}")


def _as_int(value: Any, name: str, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if number < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {number}")
    return number


def _as_str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name} must be a list or comma-separated string")
    return tuple(str(item).lower().strip() for item in value if str(item).strip())


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return dict(value)


def _validate_url(value: str | None, label: str) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{label} must be an http(s) URL, got {value!r}")
    return value.rstrip("/")


def _reject_secrets(data: Any, path: str = "config") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"api_key", "token", "secret", "password", "credentials"}:
                raise ConfigError(
                    f"{path}.{key} must not contain a secret; use an environment variable"
                )
            _reject_secrets(value, f"{path}.{key}")
    elif isinstance(data, list):
        for index, value in enumerate(data):
            _reject_secrets(value, f"{path}[{index}]")


def _read_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read YAML config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"YAML config {path} must contain a mapping at the top level")
    if data.get("version", 1) != 1:
        raise ConfigError(f"Unsupported config version {data.get('version')!r}; expected 1")
    _reject_secrets(data)
    return data


def _config_path(overrides: dict[str, Any], used: set[str]) -> Path | None:
    if overrides.get("config_path"):
        return Path(overrides["config_path"]).expanduser()
    configured = _first_env(("TIERU_CONFIG",), ("WAKU_CONFIG",), used)
    if configured:
        return Path(configured).expanduser()
    for candidate in (Path(".tieru/config.yaml"), Path(".tieru/config.yml")):
        if candidate.exists():
            return candidate
    if not Path(".tieru").exists():
        for candidate in (Path(".waku/config.yaml"), Path(".waku/config.yml")):
            if candidate.exists():
                used.add(str(candidate))
                return candidate
    return None


def _providers(data: dict[str, Any]) -> dict[str, ProviderConfig]:
    providers = dict(BUILTIN_PROVIDERS)
    raw = data.get("providers") or {}
    if not isinstance(raw, dict):
        raise ConfigError("providers must be a mapping")
    for name, values in raw.items():
        if not isinstance(values, dict):
            raise ConfigError(f"providers.{name} must be a mapping")
        base = providers.get(name)
        protocol = values.get("protocol", base.protocol if base else "")
        if protocol not in PROTOCOLS:
            raise ConfigError(
                f"providers.{name}.protocol must be one of: {', '.join(PROTOCOLS)}"
            )
        providers[name] = ProviderConfig(
            protocol=protocol,
            api_key_env=str(values.get("api_key_env", base.api_key_env if base else "")),
            base_url=_validate_url(
                values.get("base_url", base.base_url if base else None),
                f"providers.{name}.base_url",
            ),
            default_model=str(values.get("default_model", base.default_model if base else "")),
            default_small_model=str(
                values.get("default_small_model", base.default_small_model if base else "")
            ),
            catalog_url=_validate_url(
                values.get("catalog_url", base.catalog_url if base else None),
                f"providers.{name}.catalog_url",
            ),
            flagship=str(values.get("flagship", base.flagship if base else "")),
            fast=str(values.get("fast", base.fast if base else "")),
            key_url=str(values.get("key_url", base.key_url if base else "")),
            keyless=_as_bool(values.get("keyless", base.keyless if base else False),
                             f"providers.{name}.keyless"),
        )
    return providers


def _role_from_mapping(
    name: str, values: dict[str, Any], providers: dict[str, ProviderConfig]
) -> ModelRole:
    if not isinstance(values, dict):
        raise ConfigError(f"profiles.*.{name} must be a mapping")
    provider_name = str(values.get("provider", ""))
    provider = providers.get(provider_name)
    if provider is None:
        raise ConfigError(f"Role '{name}' references unknown provider '{provider_name}'")
    protocol = str(values.get("protocol", provider.protocol))
    if protocol not in PROTOCOLS:
        raise ConfigError(f"Role '{name}' has unsupported protocol '{protocol}'")
    default_model = (
        provider.default_small_model if name == "small" else provider.default_model
    )
    model = str(values.get("model", default_model)).strip()
    if not model:
        raise ConfigError(f"Role '{name}' requires a model")
    options = values.get("options") or {}
    if not isinstance(options, dict):
        raise ConfigError(f"profiles.*.{name}.options must be a mapping")
    return ModelRole(
        provider=provider_name,
        protocol=protocol,
        model=model,
        base_url=_validate_url(values.get("base_url", provider.base_url), f"roles.{name}.base_url"),
        api_key_env=str(values.get("api_key_env", provider.api_key_env)),
        options=dict(options),
    )


def _resolved_roles(
    data: dict[str, Any], profile_name: str, providers: dict[str, ProviderConfig],
    used: set[str], overrides: dict[str, Any],
) -> dict[str, ModelRole]:
    profiles = _builtin_profiles()
    custom = data.get("profiles") or {}
    if not isinstance(custom, dict):
        raise ConfigError("profiles must be a mapping")
    profiles.update(custom)
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        raise ConfigError(
            f"Unknown profile '{profile_name}'. Available: {', '.join(sorted(profiles))}"
        )
    missing = [role for role in ROLE_NAMES if role not in profile]
    if missing:
        raise ConfigError(f"Profile '{profile_name}' is missing roles: {', '.join(missing)}")
    roles = {
        name: _role_from_mapping(name, profile[name], providers)
        for name in ROLE_NAMES
    }
    generic_provider = _first_env(("TIERU_PROVIDER",), ("WAKU_PROVIDER",), used)
    generic_base_url = _first_env(("TIERU_BASE_URL",), ("WAKU_BASE_URL",), used)
    for name in ROLE_NAMES:
        current = roles[name]
        provider_name = overrides.get(f"{name}_provider")
        if provider_name is None:
            provider_name = _first_env(
                (f"TIERU_{name.upper()}_PROVIDER",),
                (("WAKU_JUDGE_PROVIDER",) if name == "judge" else ()),
                used,
            )
        if provider_name is None and name in ("main", "small"):
            provider_name = generic_provider
        provider_name = str(provider_name or current.provider)
        provider = providers.get(provider_name)
        if provider is None:
            raise ConfigError(f"Role '{name}' references unknown provider '{provider_name}'")

        model = overrides.get(f"{name}_model")
        if model is None:
            primary = (f"TIERU_{name.upper()}_MODEL",)
            if name == "main":
                primary += ("TIERU_MODEL",)
            legacy = (
                ("WAKU_MODEL",) if name == "main" else
                ("WAKU_SMALL_MODEL",) if name == "small" else
                ("WAKU_JUDGE_MODEL",)
            )
            model = _first_env(primary, legacy, used)
        if model is None and provider_name != current.provider:
            model = provider.default_small_model if name == "small" else provider.default_model

        protocol = overrides.get(f"{name}_protocol")
        if protocol is None:
            protocol = _first_env((f"TIERU_{name.upper()}_PROTOCOL",), (), used)
        protocol = str(protocol or provider.protocol)
        if protocol not in PROTOCOLS:
            raise ConfigError(f"Role '{name}' has unsupported protocol '{protocol}'")

        base_url = overrides.get(f"{name}_base_url")
        if base_url is None:
            base_url = _first_env((f"TIERU_{name.upper()}_BASE_URL",), (), used)
        if base_url is None and name in ("main", "small"):
            base_url = generic_base_url
        if base_url is None:
            base_url = provider.base_url
        roles[name] = ModelRole(
            provider_name, protocol, str(model or current.model),
            _validate_url(base_url, f"roles.{name}.base_url"), provider.api_key_env,
            current.options,
        )
    return roles


def _home(
    data: dict[str, Any],
    overrides: dict[str, Any],
    used: set[str],
    config_path: Path | None,
) -> tuple[Path, bool]:
    explicit = overrides.get("home")
    if explicit is None:
        explicit = _first_env(("TIERU_HOME",), ("WAKU_HOME",), used)
    if explicit is not None:
        home = Path(explicit).expanduser()
        legacy = "WAKU_HOME" in used
        return home, legacy
    if data.get("home"):
        return Path(str(data["home"])).expanduser(), False
    if Path(".tieru").exists():
        return Path(".tieru"), False
    legacy_config = "WAKU_CONFIG" in used or (
        config_path is not None and str(config_path) in used
    )
    canonical_env = any(name.startswith("TIERU_") for name in os.environ)
    canonical_override = any(
        key not in {"home", "config_path"} and value is not None
        for key, value in overrides.items()
    )
    if (config_path is not None and not legacy_config) or canonical_env or canonical_override:
        return Path(".tieru"), False
    if Path(".waku").exists():
        used.add(".waku")
        return Path(".waku"), True
    return Path(".tieru"), False


def load_settings(overrides: dict[str, Any] | None = None) -> Settings:
    """Load, validate, and resolve one immutable-by-convention settings snapshot."""
    overrides = dict(overrides or {})
    used: set[str] = {
        name
        for name in os.environ
        if name.startswith("WAKU_") and f"TIERU_{name[5:]}" not in os.environ
    }
    config_path = _config_path(overrides, used)
    data = _read_yaml(config_path)
    providers = _providers(data)
    profile = overrides.get("profile")
    if profile is None:
        profile = _first_env(("TIERU_PROFILE",), ("WAKU_PROFILE",), used)
    profile = str(profile or data.get("active_profile") or "default")
    roles = _resolved_roles(data, profile, providers, used, overrides)
    home, legacy_home = _home(data, overrides, used, config_path)

    def value(name: str, default: Any, legacy_name: str | None = None) -> Any:
        if name in overrides:
            return overrides[name]
        primary = (f"TIERU_{name.upper()}",)
        legacy = (legacy_name or f"WAKU_{name.upper()}",)
        env = _first_env(primary, legacy, used)
        return data.get(name, default) if env is None else env

    main, small = roles["main"], roles["small"]
    role_api_keys = {}
    for role_name in ROLE_NAMES:
        secret = _first_env((f"TIERU_{role_name.upper()}_API_KEY",), (), used)
        if secret:
            role_api_keys[role_name] = secret
    generic_key = _first_env(("TIERU_API_KEY",), ("WAKU_API_KEY",), used)
    if generic_key:
        role_api_keys.setdefault("main", generic_key)
        if small.provider == main.provider:
            role_api_keys.setdefault("small", generic_key)

    settings = Settings(
        provider=main.provider,
        api_key=role_api_keys.get("main", ""),
        base_url=main.base_url,
        model=main.model,
        small_model=small.model,
        home=home,
        max_iterations=_as_int(value("max_iterations", 10), "max_iterations", 1),
        max_tokens=_as_int(value("max_tokens", 8192), "max_tokens", 1),
        history_turns=_as_int(value("history_turns", 12), "history_turns", 1),
        consolidate_every=_as_int(value("consolidate_every", 6), "consolidate_every", 1),
        retrieval_top_k=_as_int(value("retrieval_top_k", 4), "retrieval_top_k", 1),
        semantic_store=str(value("semantic_store", "sqlite")),
        episodic_store=str(value("episodic_store", "sqlite")),
        embedding_model=str(value("embedding_model", "")),
        memory_write_policy=str(value("memory_write_policy", "explicit")),
        memory_max_records=_as_int(value("memory_max_records", 10000),
                                   "memory_max_records", 1),
        tool_permissions=_as_mapping(value("tool_permissions", {}), "tool_permissions"),
        browser_enabled=_as_bool(value("browser_enabled", False), "browser_enabled"),
        browser_allowed_domains=_as_str_tuple(
            value("browser_allowed_domains", []), "browser_allowed_domains"
        ),
        browser_allow_local_fixture=_as_bool(
            value("browser_allow_local_fixture", False), "browser_allow_local_fixture"
        ),
        browser_timeout_seconds=_as_int(
            value("browser_timeout_seconds", 15), "browser_timeout_seconds", 1
        ),
        browser_max_actions=_as_int(
            value("browser_max_actions", 20), "browser_max_actions", 1
        ),
        apple_calendar=_as_bool(value("apple_calendar", False), "apple_calendar"),
        google_calendar=_as_bool(value("google_calendar", False), "google_calendar"),
        google_calendar_id=str(value("google_calendar_id", "primary")),
        apple_tools=_as_bool(value("apple_tools", False), "apple_tools"),
        gh_tool=_as_bool(value("gh_tool", False), "gh_tool"),
        gh_repo=str(value("gh_repo", "")),
        experimental=_as_bool(value("experimental", False), "experimental"),
        graph_workflows=_as_bool(value("graph_workflows", False), "graph_workflows"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        whatsapp_token=os.getenv("WHATSAPP_TOKEN", ""),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        profile=profile,
        roles=roles,
        providers=providers,
        config_path=config_path,
        compatibility_mode=legacy_home or bool(used),
        compatibility_sources=tuple(sorted(used)),
        llm_timeout=float(value("llm_timeout", 120.0, "WAKU_LLM_TIMEOUT")),
        judge_concurrency=_as_int(value("judge_concurrency", 2, "WAKU_JUDGE_CONCURRENCY"),
                                  "judge_concurrency", 1),
        role_api_keys=role_api_keys,
    )
    if settings.semantic_store not in ("sqlite", "supabase"):
        raise ConfigError("semantic_store must be 'sqlite' or 'supabase'")
    if settings.semantic_store == "supabase" and not settings.embedding_model:
        raise ConfigError(
            "embedding_model is required when semantic_store is 'supabase'; "
            "Tieru never hard-codes an embedding model"
        )
    if settings.episodic_store not in ("sqlite", "notion"):
        raise ConfigError("episodic_store must be 'sqlite' or 'notion'")
    if settings.memory_write_policy not in ("explicit", "consolidate"):
        raise ConfigError("memory_write_policy must be 'explicit' or 'consolidate'")
    if not isinstance(settings.tool_permissions, dict):
        raise ConfigError("tool_permissions must be a mapping")
    if used:
        _warn_legacy(
            "Using deprecated Waku compatibility settings: "
            f"{', '.join(sorted(used))}. Prefer TIERU_* and .tieru; data was not moved or merged."
        )
    return settings


def available_profiles(config_path: Path | None = None) -> list[str]:
    data = _read_yaml(config_path)
    profiles = set(_builtin_profiles())
    custom = data.get("profiles") or {}
    if isinstance(custom, dict):
        profiles.update(custom)
    return sorted(profiles)


def save_active_profile(name: str, config_path: Path | None = None) -> Path:
    """Persist the selected profile without writing credentials or touching databases."""
    path = config_path or Path(".tieru/config.yaml")
    data = _read_yaml(path)
    profiles = set(_builtin_profiles()) | set((data.get("profiles") or {}).keys())
    if name not in profiles:
        raise ConfigError(f"Unknown profile '{name}'. Available: {', '.join(sorted(profiles))}")
    data["version"] = 1
    data["active_profile"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def migrate_legacy_home(
    source: Path = Path(".waku"), target: Path = Path(".tieru"), *, confirmed: bool = False
) -> dict[str, Any]:
    """Copy legacy state only after explicit confirmation; never modify the source."""
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_dir():
        raise ConfigError(f"Legacy home does not exist: {source}")
    if target.exists():
        raise ConfigError(f"Target already exists; refusing to merge or overwrite: {target}")
    if source == target or source in target.parents:
        raise ConfigError("Migration target must be separate from the legacy source")
    if not confirmed:
        return {"copied": False, "source": str(source), "target": str(target)}
    shutil.copytree(source, target)
    return {"copied": True, "source": str(source), "target": str(target)}


def with_role(settings: Settings, role_name: str, **changes: Any) -> Settings:
    """Return a settings copy with one role overridden (used by arena/judge)."""
    role = replace(settings.role(role_name), **changes)
    roles = dict(settings.roles)
    roles[role_name] = role
    return replace(settings, roles=roles)

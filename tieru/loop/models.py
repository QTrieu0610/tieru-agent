"""Role-based model routing over two protocol adapters."""

from __future__ import annotations

import os
from typing import Any

from tieru.config import (
    BUILTIN_PROVIDERS,
    DOTENV_PATH,
    ConfigError,
    ProviderConfig,
    Settings,
)
from tieru.loop.adapters import AnthropicMessagesAdapter, OpenAIChatAdapter

Provider = ProviderConfig
PROVIDERS = BUILTIN_PROVIDERS
KEY_URLS = {name: provider.key_url for name, provider in PROVIDERS.items()}

# Backward-compatible class name for downstream imports.
OpenAICompatClient = OpenAIChatAdapter


def _no_key_message(name: str, key_env: str) -> str:
    where = (
        f"Add it to {DOTENV_PATH}:\n    {key_env}=your-key-here"
        if DOTENV_PATH
        else (
            f"No .env found from {os.getcwd()} upward — create one here:\n"
            f"    echo '{key_env}=your-key-here' >> .env"
        )
    )
    url = KEY_URLS.get(name, "")
    lead = f"No API key for provider '{name}'.\n\n"
    if url:
        lead += f"  1. Get a key: {url}\n"
    return lead + (
        f"  2. {where}\n\n"
        f"Other providers: {', '.join(sorted(PROVIDERS))}\n"
        "Switch with TIERU_PROFILE or TIERU_MAIN_PROVIDER/TIERU_MAIN_MODEL "
        "(WAKU_PROVIDER remains a deprecated fallback)."
    )


def _validate_key(key: str, key_env: str) -> None:
    try:
        key.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise SystemExit(
            f"{key_env} contains a non-ASCII character. Re-paste the key "
            "with no spaces or line breaks."
        ) from exc


def get_client(settings: Settings, role: str = "main"):
    """Build one role's client from validated settings.

    The loop sees only ``client.messages.create/stream``; provider-specific
    construction and protocol conversion stop here.
    """
    role_config = settings.role(role)
    provider = settings.providers.get(role_config.provider)
    if provider is None:
        raise ConfigError(f"Unknown provider '{role_config.provider}' for role '{role}'")
    key = settings.secret_for(role)
    if not provider.keyless and not key:
        raise SystemExit(_no_key_message(role_config.provider, role_config.api_key_env))
    if key:
        _validate_key(key, role_config.api_key_env or f"TIERU_{role.upper()}_API_KEY")
    if role == "main":
        settings.provider = role_config.provider
        settings.model = role_config.model
        settings.base_url = role_config.base_url
        if not settings.small_model:
            settings.small_model = settings.role("small").model
    elif role == "small":
        settings.small_model = role_config.model
    api_key = key or "ollama-local"
    if role_config.protocol == "anthropic":
        return AnthropicMessagesAdapter(
            api_key=api_key,
            base_url=role_config.base_url,
            timeout=settings.llm_timeout,
        )
    if role_config.protocol == "openai":
        return OpenAICompatClient(
            api_key=api_key,
            base_url=role_config.base_url,
            timeout=settings.llm_timeout,
        )
    raise ConfigError(
        f"Role '{role}' uses unsupported protocol '{role_config.protocol}'"
    )


class ModelRouter:
    """Lazily construct independent main, small, and judge clients."""

    def __init__(
        self,
        settings: Settings,
        *,
        clients: dict[str, Any] | None = None,
        shared_client: Any | None = None,
    ):
        self.settings = settings
        self._clients = dict(clients or {})
        if shared_client is not None:
            for role in ("main", "small", "judge"):
                self._clients.setdefault(role, shared_client)

    def role(self, name: str):
        return self.settings.role(name)

    def client(self, name: str):
        if name not in self._clients:
            self._clients[name] = get_client(self.settings, name)
        return self._clients[name]

    def model(self, name: str) -> str:
        return self.role(name).model

    def provider(self, name: str) -> str:
        return self.role(name).provider

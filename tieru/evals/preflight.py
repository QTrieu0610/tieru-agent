"""Provider pre-flight probe and readiness diagnostics for live evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from tieru.config import Settings
from tieru.loop.adapters import ModelError
from tieru.loop.models import get_client
from tieru.memory.personal import redact_secrets


@dataclass(frozen=True)
class ProviderProbeResult:
    provider: str
    model: str
    endpoint: str
    reachable: bool
    credentials_status: str
    status: str
    usage_telemetry: bool
    error: str | None = None
    models_available: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_endpoint(base_url: str | None) -> str:
    if not base_url:
        return "default"
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")


def probe_provider(
    settings: Settings,
    *,
    role: str = "main",
    client: Any = None,
    timeout: float = 10.0,
) -> ProviderProbeResult:
    """Send a lightweight request to verify that the configured provider is reachable.

    Never prints or leaks credentials. Uses Tieru's normal provider abstraction.
    """
    role_config = settings.role(role)
    provider_name = role_config.provider
    model_name = role_config.model
    endpoint = _safe_endpoint(role_config.base_url)
    provider_def = settings.providers.get(provider_name)
    keyless = provider_def.keyless if provider_def is not None else False
    has_key = bool(settings.secret_for(role))

    credentials_status = "not_required" if keyless else ("configured" if has_key else "missing")

    models_available: tuple[str, ...] = ()
    details: dict[str, Any] = {}

    # Inspect Ollama local endpoints if configured
    if provider_name == "ollama" and role_config.base_url:
        try:
            from tieru.providers.ollama import OllamaIntegration

            ollama = OllamaIntegration(role_config.base_url, timeout=timeout)
            health = ollama.health()
            details["ollama_version"] = health.get("version")
            raw_models = ollama.models()
            models_available = tuple(sorted(m.name for m in raw_models))
            details["models_installed"] = list(models_available)
        except Exception as exc:
            details["ollama_discovery_error"] = redact_secrets(str(exc))

    # If missing required credentials and no injected client was provided, block immediately
    if client is None and not keyless and not has_key:
        return ProviderProbeResult(
            provider=provider_name,
            model=model_name,
            endpoint=endpoint,
            reachable=False,
            credentials_status="missing",
            status="BLOCKED_AUTH_ERROR",
            usage_telemetry=False,
            error=f"No API key configured for provider '{provider_name}'",
            models_available=models_available,
            details=details,
        )

    # Resolve or use injected client
    try:
        if client is None:
            client = get_client(settings, role)
    except Exception as exc:
        msg = redact_secrets(str(exc))
        return ProviderProbeResult(
            provider=provider_name,
            model=model_name,
            endpoint=endpoint,
            reachable=False,
            credentials_status=credentials_status,
            status="BLOCKED_PROVIDER_UNAVAILABLE",
            usage_telemetry=False,
            error=msg,
            models_available=models_available,
            details=details,
        )

    # Perform lightweight probe call
    try:
        response = client.messages.create(
            model=model_name,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        usage_present = bool(
            hasattr(response, "usage")
            and response.usage is not None
            and (
                getattr(response.usage, "input_tokens", None) is not None
                or getattr(response.usage, "output_tokens", None) is not None
            )
        )
        return ProviderProbeResult(
            provider=provider_name,
            model=model_name,
            endpoint=endpoint,
            reachable=True,
            credentials_status=credentials_status,
            status="READY",
            usage_telemetry=usage_present,
            models_available=models_available,
            details=details,
        )
    except TimeoutError as exc:
        return ProviderProbeResult(
            provider=provider_name,
            model=model_name,
            endpoint=endpoint,
            reachable=False,
            credentials_status=credentials_status,
            status="BLOCKED_TIMEOUT",
            usage_telemetry=False,
            error=redact_secrets(str(exc)),
            models_available=models_available,
            details=details,
        )
    except ModelError as exc:
        msg = redact_secrets(str(exc))
        lowered = msg.lower()
        if any(w in lowered for w in ("timeout", "timed out")):
            status = "BLOCKED_TIMEOUT"
        elif any(w in lowered for w in ("auth", "401", "403", "unauthorized", "api_key", "forbidden")):
            status = "BLOCKED_AUTH_ERROR"
        else:
            status = "BLOCKED_PROVIDER_UNAVAILABLE"
        return ProviderProbeResult(
            provider=provider_name,
            model=model_name,
            endpoint=endpoint,
            reachable=False,
            credentials_status=credentials_status,
            status=status,
            usage_telemetry=False,
            error=msg,
            models_available=models_available,
            details=details,
        )
    except Exception as exc:
        msg = redact_secrets(str(exc))
        lowered = msg.lower()
        if "timeout" in lowered:
            status = "BLOCKED_TIMEOUT"
        elif any(w in lowered for w in ("auth", "401", "403", "unauthorized")):
            status = "BLOCKED_AUTH_ERROR"
        else:
            status = "BLOCKED_PROVIDER_UNAVAILABLE"
        return ProviderProbeResult(
            provider=provider_name,
            model=model_name,
            endpoint=endpoint,
            reachable=False,
            credentials_status=credentials_status,
            status=status,
            usage_telemetry=False,
            error=msg,
            models_available=models_available,
            details=details,
        )

"""Configuration-driven model candidate registry."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tieru.config import ConfigError
from tieru.fabric.models import ModelCandidate

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_CAPABILITIES = {"text", "tool_calling", "structured_output", "long_context"}
_COST_TIERS = {"free", "low", "medium", "high", "unknown"}
_LATENCY_TIERS = {"fast", "medium", "slow", "unknown"}


class CandidateRegistry:
    """Build candidates without doing credential or network checks."""

    def __init__(self, settings):
        self.settings = settings
        self._candidates = self._load()

    def all(self) -> tuple[ModelCandidate, ...]:
        return self._candidates

    def get(self, candidate_id: str) -> ModelCandidate:
        for candidate in self._candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        raise KeyError(candidate_id)

    def _load(self) -> tuple[ModelCandidate, ...]:
        raw = self.settings.fabric_models
        if raw:
            return tuple(self._configured(alias, values) for alias, values in raw.items())
        # M11 compatibility: preserve the role targets already selected by the
        # active profile. This is not cloud auto-opt-in; those targets were the
        # installation's existing explicit execution configuration.
        found: list[ModelCandidate] = []
        identities: dict[tuple[str, str], set[str]] = {}
        for role_name in ("main", "small"):
            role = self.settings.role(role_name)
            identities.setdefault((role.provider, role.model), set()).add(role_name)
        for index, ((provider_name, model), roles) in enumerate(identities.items()):
            provider = self.settings.providers[provider_name]
            candidate_id = "role-" + "-".join(sorted(roles))
            found.append(ModelCandidate(
                candidate_id=candidate_id,
                provider=provider_name,
                model=model,
                protocol=provider.protocol,
                role_compatibility=tuple(sorted(roles)),
                local=provider.keyless,
                capabilities={"text": True, "tool_calling": True, "long_context": True},
                cost_tier="free" if provider.keyless else "unknown",
                preference_weight=max(0.0, 1.0 - index * 0.1),
                base_url=role.base_url or provider.base_url,
                api_key_env=role.api_key_env or provider.api_key_env,
                explicit=False,
            ))
        return tuple(found)

    def _configured(self, alias: str, values: Mapping[str, Any]) -> ModelCandidate:
        if not _ID.fullmatch(str(alias)):
            raise ConfigError(
                f"fabric.models alias {alias!r} must match {_ID.pattern}"
            )
        if not isinstance(values, Mapping):
            raise ConfigError(f"fabric.models.{alias} must be a mapping")
        provider_name = str(values.get("provider", "")).strip()
        provider = self.settings.providers.get(provider_name)
        if provider is None:
            raise ConfigError(
                f"fabric.models.{alias} references unknown provider {provider_name!r}"
            )
        model = str(values.get("model", "")).strip()
        if not model:
            raise ConfigError(f"fabric.models.{alias}.model is required")
        raw_capabilities = values.get("capabilities", {})
        if not isinstance(raw_capabilities, Mapping):
            raise ConfigError(f"fabric.models.{alias}.capabilities must be a mapping")
        unknown = set(raw_capabilities) - _CAPABILITIES
        if unknown:
            raise ConfigError(
                f"fabric.models.{alias}.capabilities contains unsupported values: "
                + ", ".join(sorted(unknown))
            )
        capabilities: dict[str, bool | None] = {}
        for name, value in raw_capabilities.items():
            if value not in (True, False, None, "unknown"):
                raise ConfigError(
                    f"fabric.models.{alias}.capabilities.{name} must be true, false, or unknown"
                )
            capabilities[str(name)] = None if value == "unknown" else value
        roles = values.get("roles", ("main", "small"))
        if not isinstance(roles, (list, tuple)) or not roles:
            raise ConfigError(f"fabric.models.{alias}.roles must be a non-empty list")
        role_compatibility = tuple(str(role) for role in roles)
        invalid_roles = set(role_compatibility) - {"main", "small", "judge"}
        if invalid_roles:
            raise ConfigError(
                f"fabric.models.{alias}.roles contains invalid roles: "
                + ", ".join(sorted(invalid_roles))
            )
        cost_tier = str(values.get("cost_tier", "unknown")).lower()
        latency_tier = str(values.get("latency_tier", "unknown")).lower()
        if cost_tier not in _COST_TIERS:
            raise ConfigError(f"fabric.models.{alias}.cost_tier is invalid")
        if latency_tier not in _LATENCY_TIERS:
            raise ConfigError(f"fabric.models.{alias}.latency_tier is invalid")
        preference = _number(values.get("preference", 0.5), f"fabric.models.{alias}.preference")
        if not 0 <= preference <= 1:
            raise ConfigError(f"fabric.models.{alias}.preference must be between 0 and 1")
        local = values.get("local", provider.keyless)
        if not isinstance(local, bool):
            raise ConfigError(f"fabric.models.{alias}.local must be true or false")
        enabled = values.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"fabric.models.{alias}.enabled must be true or false")
        protocol = str(values.get("protocol", provider.protocol))
        if protocol not in {"anthropic", "openai"}:
            raise ConfigError(f"fabric.models.{alias}.protocol is unsupported")
        if local and not provider.keyless:
            raise ConfigError(
                f"fabric.models.{alias}.local requires a keyless provider configuration"
            )
        return ModelCandidate(
            candidate_id=str(alias), provider=provider_name, model=model,
            protocol=protocol,
            role_compatibility=role_compatibility, local=local, enabled=enabled,
            capabilities=capabilities,
            context_limit=_optional_positive_int(
                values.get("context_limit"), f"fabric.models.{alias}.context_limit"
            ),
            output_limit=_optional_positive_int(
                values.get("output_limit"), f"fabric.models.{alias}.output_limit"
            ),
            cost_tier=cost_tier, latency_tier=latency_tier,
            privacy_class=str(values.get("privacy_class", "standard")),
            preference_weight=preference,
            base_url=str(values.get("base_url") or provider.base_url or "") or None,
            api_key_env=str(values.get("api_key_env") or provider.api_key_env),
            explicit=True,
        )


def _number(value: Any, path: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be a number") from exc


def _optional_positive_int(value: Any, path: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be a positive integer") from exc
    if result <= 0:
        raise ConfigError(f"{path} must be a positive integer")
    return result

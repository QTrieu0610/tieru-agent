"""Lazy, bounded and credential-safe candidate availability checks."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tieru.fabric.models import AvailabilityStatus, ModelCandidate

Probe = Callable[[ModelCandidate], Any]


class AvailabilityService:
    def __init__(self, settings, *, probe: Probe | None = None, clock=None,
                 credential_override: bool = False):
        self.settings = settings
        self.probe = probe or self._probe_local
        self.clock = clock or time.monotonic
        self.credential_override = credential_override
        self.ttl = max(1, int(settings.fabric_availability_ttl_seconds))
        self._cache: dict[str, tuple[float, AvailabilityStatus]] = {}

    def check(self, candidate: ModelCandidate, *, refresh: bool = False) -> AvailabilityStatus:
        now = self.clock()
        cached = self._cache.get(candidate.candidate_id)
        if not refresh and cached and now - cached[0] < self.ttl:
            return replace(cached[1], cached=True)
        status = self._evaluate(candidate)
        self._cache[candidate.candidate_id] = (now, status)
        return status

    def refresh(self, candidates) -> list[AvailabilityStatus]:
        return [self.check(candidate, refresh=True) for candidate in candidates]

    def invalidate(self, candidate_id: str | None = None) -> None:
        if candidate_id is None:
            self._cache.clear()
        else:
            self._cache.pop(candidate_id, None)

    def mark_unavailable(self, candidate: ModelCandidate, reason: str) -> AvailabilityStatus:
        status = self._status(
            candidate, credential=self._credential(candidate), endpoint=False,
            model=False, available=False, reason=reason,
        )
        self._cache[candidate.candidate_id] = (self.clock(), status)
        return status

    def _evaluate(self, candidate: ModelCandidate) -> AvailabilityStatus:
        if not candidate.configured:
            return self._status(candidate, None, None, None, False, "not_configured")
        if not candidate.enabled:
            return self._status(candidate, None, None, None, False, "candidate_disabled")
        credential = self._credential(candidate)
        if not candidate.local and not credential:
            return self._status(
                candidate, False, None, None, False, "unavailable_credentials"
            )
        if not candidate.local:
            # A credential is a bounded, local check. Cloud health is deferred to
            # the actual request; M12 never sends probe completions.
            return self._status(candidate, True, None, None, True, "credential_available")
        try:
            result = self.probe(candidate)
            endpoint, model, reason = self._normalize_probe(result, candidate)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            endpoint, model, reason = False, None, "endpoint_unavailable"
        available = endpoint is True and model is not False
        if endpoint is True and model is False:
            reason = "model_not_installed"
        elif available and not reason:
            reason = "available"
        return self._status(candidate, True, endpoint, model, available, reason)

    def _credential(self, candidate: ModelCandidate) -> bool:
        if self.credential_override:
            return True
        if candidate.local or self.settings.providers[candidate.provider].keyless:
            return True
        for role_name, key in self.settings.role_api_keys.items():
            if role_name not in {"main", "small", "judge"}:
                continue
            role = self.settings.role(role_name)
            if key and role.provider == candidate.provider:
                return True
        if self.settings.api_key and self.settings.provider == candidate.provider:
            return True
        return bool(os.getenv(candidate.api_key_env, "") if candidate.api_key_env else "")

    @staticmethod
    def _normalize_probe(result: Any, candidate: ModelCandidate) -> tuple[bool, bool | None, str]:
        if isinstance(result, bool):
            return result, None, "available" if result else "endpoint_unavailable"
        if isinstance(result, dict):
            endpoint = bool(result.get("endpoint_available", result.get("available", False)))
            model = result.get("model_available")
            return endpoint, None if model is None else bool(model), str(result.get("reason", ""))
        if isinstance(result, (list, tuple, set)):
            models = {str(item) for item in result}
            return True, candidate.model in models, ""
        raise ValueError("availability probe returned an unsupported result")

    def _probe_local(self, candidate: ModelCandidate):
        if candidate.provider != "ollama":
            # Custom keyless providers may opt into a health-only endpoint; no
            # catalog format is assumed.
            request = urllib.request.Request(candidate.base_url or "", method="GET")
            with urllib.request.urlopen(request, timeout=min(2.0, self.settings.llm_timeout)):
                return True
        parts = urlsplit(candidate.base_url or "http://127.0.0.1:11434/v1")
        path = parts.path.rstrip("/").removesuffix("/v1")
        url = urlunsplit((parts.scheme, parts.netloc, path + "/api/tags", "", ""))
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=min(2.0, self.settings.llm_timeout)) as response:
            payload = json.loads(response.read(1_048_576).decode("utf-8"))
        return [item.get("name", "") for item in payload.get("models", [])]

    @staticmethod
    def _status(
        candidate: ModelCandidate,
        credential: bool | None,
        endpoint: bool | None,
        model: bool | None,
        available: bool,
        reason: str,
    ) -> AvailabilityStatus:
        return AvailabilityStatus(
            candidate_id=candidate.candidate_id,
            configured=candidate.configured,
            credential_available=credential,
            endpoint_available=endpoint,
            model_available=model,
            available=available,
            reason=reason,
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

"""Fast, offline-first, share-safe Tieru installation diagnostics."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tieru import __version__
from tieru.config import ConfigError, Settings, load_settings, parse_telegram_allowed_users
from tieru.db import SCHEMA, _migrate, connect
from tieru.fabric.availability import AvailabilityService
from tieru.fabric.candidates import CandidateRegistry
from tieru.memory import bundled_skill_dirs
from tieru.memory.personal import redact_secrets
from tieru.memory.procedural.loader import _parse_text
from tieru.providers.ollama import OllamaIntegration
from tieru.trust import TrustKernel

PACKAGE_NAME = "tieru-agent"
DEFAULT_REQUIRES_PYTHON = ">=3.11"
MAX_MCP_CONFIG_BYTES = 1024 * 1024
MAX_SKILL_BYTES = 1024 * 1024
MAX_SKILL_FILES = 1000
WHATSAPP_BODY_MIN = 1024
WHATSAPP_BODY_MAX = 10 * 1024 * 1024
_MCP_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DoctorStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    OPTIONAL = "OPTIONAL"
    SKIP = "SKIP"


def _safe_text(value: object, limit: int = 500) -> str:
    text = redact_secrets(str(value)).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_value(value: Any, key: str = "") -> Any:
    if re.search(
        r"api.?key|authorization|cookie|credential|password|secret|token",
        key,
        re.IGNORECASE,
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    category: str
    status: DoctorStatus
    title: str
    detail: str
    remediation: str = ""
    required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "status": self.status.value,
            "title": _safe_text(self.title),
            "detail": _safe_text(self.detail),
            "remediation": _safe_text(self.remediation),
            "required": self.required,
            "metadata": _safe_value(self.metadata),
        }


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ready(self) -> bool:
        return not any(
            check.required and check.status is DoctorStatus.FAIL for check in self.checks
        )

    @property
    def overall_status(self) -> str:
        if not self.ready:
            return "NOT_READY"
        if any(check.status is DoctorStatus.WARN for check in self.checks):
            return "READY_WITH_WARNINGS"
        return "READY"

    @property
    def exit_code(self) -> int:
        return 0 if self.ready else 1

    def public(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "ready": self.ready,
            "checks": [check.public() for check in self.checks],
        }


@dataclass
class _StateHealth:
    ok: bool
    tables: set[str] = field(default_factory=set)
    first_run: bool = False
    detail: str = ""


def _package_details() -> tuple[str, str, str]:
    try:
        package_version = importlib.metadata.version(PACKAGE_NAME)
        package_metadata = importlib.metadata.metadata(PACKAGE_NAME)
        requires_python = package_metadata.get("Requires-Python") or DEFAULT_REQUIRES_PYTHON
        return package_version, requires_python, "installed package metadata"
    except importlib.metadata.PackageNotFoundError:
        return __version__, DEFAULT_REQUIRES_PYTHON, "importable source checkout"


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def python_satisfies(version: tuple[int, ...], requires_python: str) -> bool:
    """Evaluate the comparison clauses used by the package's Requires-Python metadata."""
    clauses = re.findall(r"(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)", requires_python)
    if not clauses:
        return False
    current = tuple(version[:3])
    for operator, raw_target in clauses:
        target = _version_tuple(raw_target)
        width = max(len(current), len(target))
        left = current + (0,) * (width - len(current))
        right = target + (0,) * (width - len(target))
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "==" and left[: len(target)] != right[: len(target)]:
            return False
    return True


def _safe_url(value: str | None) -> str:
    parsed = urlsplit(value or "")
    if not parsed.scheme or not parsed.hostname:
        return "configured endpoint"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _is_loopback_url(value: str | None) -> bool:
    host = (urlsplit(value or "").hostname or "").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _writeable_path(path: Path, *, probe_existing: bool) -> tuple[bool, str]:
    target = path.resolve(strict=False)
    if target.exists() and not target.is_dir():
        return False, "path exists but is not a directory"
    existing = target
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir() or not os.access(existing, os.R_OK | os.W_OK):
        return False, "path is not accessible and writable"
    if not target.exists() or not probe_existing:
        return True, "will be created on first use" if not target.exists() else "accessible"
    probe: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target, prefix=".tieru-doctor-", suffix=".tmp", delete=False
        ) as handle:
            probe = Path(handle.name)
    except OSError as exc:
        return False, f"write test failed ({type(exc).__name__})"
    try:
        probe.unlink(missing_ok=True)
    except OSError:
        return False, "write test cleanup failed"
    return True, "accessible and writable"


class DoctorRunner:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        selection: dict[str, bool] | None = None,
        python_version: tuple[int, ...] | None = None,
        ollama_factory: Callable[[str], Any] = OllamaIntegration,
    ) -> None:
        self.settings = settings
        self.selection = dict(selection or {})
        self.python_version = python_version or tuple(sys.version_info[:3])
        self.ollama_factory = ollama_factory
        self.checks: list[DoctorCheck] = []
        self._ollama: dict[str, dict[str, Any]] = {}

    def add(
        self,
        check_id: str,
        category: str,
        status: DoctorStatus,
        title: str,
        detail: str,
        *,
        remediation: str = "",
        required: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.checks.append(
            DoctorCheck(
                check_id,
                category,
                status,
                title,
                _safe_text(detail),
                _safe_text(remediation),
                required,
                dict(metadata or {}),
            )
        )

    def run(self) -> DoctorReport:
        package_version, requires_python, package_source = _package_details()
        supported = python_satisfies(self.python_version, requires_python)
        version = ".".join(str(part) for part in self.python_version)
        self.add(
            "runtime.python",
            "Runtime",
            DoctorStatus.PASS if supported else DoctorStatus.FAIL,
            "Python",
            f"{version} ({requires_python})" if supported else f"{version} is unsupported",
            remediation=(f"Install a Python version satisfying {requires_python}." if not supported else ""),
            required=True,
            metadata={"version": version, "requires_python": requires_python},
        )
        self.add(
            "runtime.package",
            "Runtime",
            DoctorStatus.PASS,
            "Tieru",
            f"{package_version} ({package_source})",
            required=True,
            metadata={"version": package_version},
        )
        try:
            settings = self.settings or load_settings()
        except ConfigError:
            self.add(
                "configuration.load",
                "Configuration",
                DoctorStatus.FAIL,
                "Configuration",
                "configuration could not be loaded safely",
                remediation="Correct the selected Tieru YAML or environment override and rerun doctor.",
                required=True,
            )
            return DoctorReport(tuple(self.checks))

        self._configuration(settings)
        home_ok = self._home(settings)
        state = self._state(settings, home_ok)
        self._models(settings)
        fabric_uses_ollama = self._fabric(settings)
        active_ollama = any(
            settings.role(name).provider == "ollama" for name in ("main", "small")
        )
        if not active_ollama and not fabric_uses_ollama:
            self.add(
                "models.ollama",
                "Models",
                DoctorStatus.OPTIONAL,
                "Ollama",
                "not required by the active profile or Fabric policy",
            )
        self._trust(settings)
        self._gateways(settings)
        self._mcp(settings)
        self._browser(settings)
        self._memory(settings, state)
        self._skills(settings)
        self._replay(settings, state)
        self._shadow(settings)
        self._capsule(settings, home_ok)
        return DoctorReport(tuple(self.checks))

    def _configuration(self, settings: Settings) -> None:
        if self.selection.get("profile_explicit"):
            profile_source = "explicit CLI"
        elif os.getenv("TIERU_PROFILE"):
            profile_source = "environment"
        elif settings.config_path:
            profile_source = "YAML/default resolution"
        else:
            profile_source = "built-in default"
        if settings.config_path:
            if self.selection.get("config_explicit"):
                source = f"explicit YAML: {settings.config_path}"
            elif os.getenv("TIERU_CONFIG"):
                source = f"environment-selected YAML: {settings.config_path}"
            else:
                source = f"project YAML: {settings.config_path}"
        else:
            source = "built-in profile"
        ignored = {"TIERU_CONFIG", "TIERU_PROFILE", "TIERU_HOME"}
        env_overrides = any(
            name.startswith("TIERU_") and name not in ignored for name in os.environ
        )
        if env_overrides:
            source += " + environment overrides"
        missing_config = bool(settings.config_path and not settings.config_path.exists())
        self.add(
            "configuration.profile",
            "Configuration",
            DoctorStatus.PASS,
            "Profile",
            f"{settings.profile} ({profile_source})",
            required=True,
            metadata={"profile": settings.profile, "selection": profile_source},
        )
        self.add(
            "configuration.source",
            "Configuration",
            (
                DoctorStatus.FAIL
                if missing_config
                else DoctorStatus.WARN if settings.compatibility_mode else DoctorStatus.PASS
            ),
            "Config source",
            (
                source + "; selected YAML does not exist"
                if missing_config
                else source
                + ("; deprecated Waku compatibility active" if settings.compatibility_mode else "")
            ),
            remediation=(
                "Create the selected YAML file or remove the explicit config selection."
                if missing_config
                else (
                    "Move active WAKU_* compatibility settings to their TIERU_* equivalents."
                    if settings.compatibility_mode
                    else ""
                )
            ),
            required=True,
            metadata={
                "source": source,
                "compatibility_mode": settings.compatibility_mode,
                "config_exists": not missing_config,
            },
        )

    def _home(self, settings: Settings) -> bool:
        ok, detail = _writeable_path(settings.home, probe_existing=True)
        self.add(
            "state.home",
            "Runtime State",
            DoctorStatus.PASS if ok else DoctorStatus.FAIL,
            "Tieru home",
            f"{settings.home.resolve(strict=False)} — {detail}",
            remediation="Choose a readable, writable directory with TIERU_HOME." if not ok else "",
            required=True,
            metadata={"path": str(settings.home.resolve(strict=False)), "exists": settings.home.exists()},
        )
        return ok

    def _state(self, settings: Settings, home_ok: bool) -> _StateHealth:
        if not home_ok:
            health = _StateHealth(False, detail="Tieru home is unavailable")
        else:
            database = settings.home / "state.db"
            connection: sqlite3.Connection | None = None
            try:
                if database.exists():
                    connection = connect(settings.home)
                    first_run = False
                else:
                    connection = sqlite3.connect(":memory:")
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.executescript(SCHEMA)
                    _migrate(connection)
                    first_run = True
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if quick_check != "ok":
                    raise sqlite3.DatabaseError("SQLite quick_check failed")
                health = _StateHealth(
                    True,
                    tables,
                    first_run,
                    "first-run schema is ready; state.db will be created on first use"
                    if first_run
                    else "state.db opened; schema and additive migrations are healthy",
                )
            except (OSError, sqlite3.Error):
                health = _StateHealth(False, detail="state.db could not be opened or validated")
            finally:
                if connection is not None:
                    connection.close()
        self.add(
            "state.sqlite",
            "Runtime State",
            DoctorStatus.PASS if health.ok else DoctorStatus.FAIL,
            "SQLite",
            health.detail,
            remediation=(
                "Check TIERU_HOME permissions and preserve state.db before investigating corruption."
                if not health.ok
                else ""
            ),
            required=True,
            metadata={"first_run": health.first_run},
        )
        return health

    def _inspect_ollama(self, base_url: str) -> dict[str, Any]:
        if base_url in self._ollama:
            return self._ollama[base_url]
        result: dict[str, Any] = {"health": None, "models": None, "error": ""}
        if not _is_loopback_url(base_url):
            result["error"] = "remote_endpoint_not_probed"
            self._ollama[base_url] = result
            return result
        try:
            integration = self.ollama_factory(base_url)
            result["health"] = integration.health()
            result["models"] = {item.name for item in integration.models()}
        except Exception as exc:
            result["error"] = type(exc).__name__
        self._ollama[base_url] = result
        return result

    @staticmethod
    def _model_present(model: str, installed: set[str]) -> bool:
        return model in installed or f"{model}:latest" in installed

    def _models(self, settings: Settings) -> None:
        ollama_roles: dict[str, list[tuple[str, bool]]] = {}
        for name in ("main", "small", "judge"):
            role = settings.role(name)
            required = name in {"main", "small"}
            if role.provider == "ollama":
                endpoint = role.base_url or settings.providers["ollama"].base_url or ""
                ollama_roles.setdefault(endpoint, []).append((name, required))
                continue
            configured = settings.providers[role.provider].keyless or bool(settings.secret_for(name))
            if required and not configured:
                key_name = role.api_key_env or "provider credential"
                self.add(
                    f"models.role.{name}",
                    "Models",
                    DoctorStatus.FAIL,
                    f"{name.title()} Model",
                    f"{role.provider}/{role.model} requires {key_name}",
                    remediation=f"Configure {key_name} or select the keyless Ollama profile.",
                    required=True,
                    metadata={"role": name, "provider": role.provider, "model": role.model},
                )
            elif required:
                self.add(
                    f"models.role.{name}", "Models", DoctorStatus.PASS,
                    f"{name.title()} Model", f"{role.provider}/{role.model}; credential configured",
                    required=True,
                    metadata={"role": name, "provider": role.provider, "model": role.model},
                )
            else:
                self.add(
                    "models.role.judge", "Models", DoctorStatus.OPTIONAL, "Judge",
                    f"{role.provider}/{role.model}; "
                    + ("configured but unused by the current runtime" if configured else "not configured"),
                    metadata={"role": name, "provider": role.provider, "model": role.model},
                )
        for index, (endpoint, roles) in enumerate(ollama_roles.items(), start=1):
            required_endpoint = any(required for _name, required in roles)
            result = self._inspect_ollama(endpoint)
            endpoint_id = "models.ollama" if index == 1 else f"models.ollama.{index}"
            if result["error"]:
                remote = result["error"] == "remote_endpoint_not_probed"
                self.add(
                    endpoint_id, "Models",
                    (DoctorStatus.WARN if required_endpoint else DoctorStatus.OPTIONAL)
                    if remote
                    else (DoctorStatus.FAIL if required_endpoint else DoctorStatus.OPTIONAL),
                    "Ollama",
                    (
                        f"remote endpoint {_safe_url(endpoint)} was not probed"
                        if remote
                        else f"cannot connect to {_safe_url(endpoint)}"
                    ),
                    remediation=(
                        "Use a loopback Ollama endpoint for local Doctor probing or verify the remote service separately."
                        if remote
                        else "Start Ollama, then rerun tieru --profile ollama-gemma4-e2b doctor."
                    ),
                    required=required_endpoint,
                    metadata={"endpoint": _safe_url(endpoint)},
                )
            else:
                version = str((result["health"] or {}).get("version", "unknown"))
                self.add(
                    endpoint_id, "Models", DoctorStatus.PASS, "Ollama",
                    f"reachable at {_safe_url(endpoint)} (version {version})",
                    required=required_endpoint,
                    metadata={"endpoint": _safe_url(endpoint), "version": version},
                )
            installed = result.get("models") or set()
            for name, required in roles:
                role = settings.role(name)
                present = not result["error"] and self._model_present(role.model, installed)
                if present:
                    status = DoctorStatus.PASS if required else DoctorStatus.OPTIONAL
                    detail = f"ollama/{role.model} installed"
                elif result["error"]:
                    remote = result["error"] == "remote_endpoint_not_probed"
                    status = (
                        DoctorStatus.SKIP
                        if required and remote
                        else DoctorStatus.FAIL if required else DoctorStatus.OPTIONAL
                    )
                    detail = f"ollama/{role.model} was not probed" if remote else f"ollama/{role.model} could not be checked"
                else:
                    status = DoctorStatus.FAIL if required else DoctorStatus.OPTIONAL
                    detail = f"ollama/{role.model} is not installed"
                self.add(
                    f"models.role.{name}", "Models", status,
                    f"{name.title()} Model" if required else "Judge", detail,
                    remediation=(f"Run: ollama pull {role.model}" if not present and not result["error"] else ""),
                    required=required,
                    metadata={"role": name, "provider": "ollama", "model": role.model},
                )

    def _fabric(self, settings: Settings) -> bool:
        try:
            registry = CandidateRegistry(settings)
            candidates = registry.all()
            base_availability = AvailabilityService(settings)

            def probe(candidate):
                if candidate.provider == "ollama":
                    cached = self._inspect_ollama(candidate.base_url or "")
                    if cached["error"]:
                        raise OSError("Ollama unavailable")
                    return cached["models"] or set()
                if candidate.local and not _is_loopback_url(candidate.base_url):
                    raise OSError("non-loopback local candidate was not probed")
                return base_availability.probe(candidate)

            availability = AvailabilityService(settings, probe=probe)
            statuses = [(candidate, availability.check(candidate)) for candidate in candidates]
        except (ConfigError, ValueError):
            self.add(
                "fabric.summary", "Capabilities",
                DoctorStatus.FAIL if settings.fabric_enabled else DoctorStatus.WARN,
                "Model Fabric", "candidate configuration is invalid",
                remediation="Correct the Fabric model and routing configuration.",
                required=settings.fabric_enabled,
            )
            return False
        available = [candidate for candidate, status in statuses if status.available]
        local_available = [candidate for candidate in available if candidate.local]
        required_failure = settings.fabric_enabled and (
            not available
            or (settings.fabric_routing_policy == "local_only" and not local_available)
        )
        if required_failure:
            status = DoctorStatus.FAIL
            detail = f"{settings.fabric_routing_policy}; no eligible required candidate"
        elif settings.fabric_enabled:
            status = DoctorStatus.PASS
            detail = (
                f"enabled; {settings.fabric_routing_policy}; "
                f"{len(available)} eligible ({len(local_available)} local)"
            )
        else:
            status = DoctorStatus.OPTIONAL
            detail = f"disabled; routing policy {settings.fabric_routing_policy}"
        self.add(
            "fabric.summary", "Capabilities", status, "Model Fabric", detail,
            remediation=("Configure an available local candidate or change Fabric policy."
                         if required_failure else ""),
            required=settings.fabric_enabled,
            metadata={
                "enabled": settings.fabric_enabled,
                "routing_policy": settings.fabric_routing_policy,
                "eligible_candidates": len(available),
                "eligible_local_candidates": len(local_available),
            },
        )
        for candidate, candidate_status in statuses:
            candidate_required = settings.fabric_enabled and (
                settings.fabric_routing_policy == "local_only" and candidate.local
            )
            if candidate_status.available:
                item_status = DoctorStatus.PASS
                reason = "available"
            elif candidate_required:
                item_status = DoctorStatus.FAIL
                reason = candidate_status.reason
            else:
                item_status = DoctorStatus.OPTIONAL
                reason = candidate_status.reason
            self.add(
                f"fabric.candidate.{candidate.candidate_id}", "Capabilities", item_status,
                f"Fabric candidate {candidate.candidate_id}",
                f"{candidate.provider}/{candidate.model}; {reason}",
                remediation=(
                    f"Start the local provider and ensure {candidate.model} is installed."
                    if candidate_required and not candidate_status.available
                    else ""
                ),
                required=candidate_required,
                metadata={
                    "candidate_id": candidate.candidate_id,
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "local": candidate.local,
                    "available": candidate_status.available,
                    "reason": candidate_status.reason,
                },
            )
        return settings.fabric_enabled and any(
            candidate.provider == "ollama" for candidate in candidates
        )

    def _trust(self, settings: Settings) -> None:
        try:
            kernel = TrustKernel(settings.trust_policy, settings.tool_permissions)
            kernel.policy.validate()
        except (TypeError, ValueError):
            self.add(
                "trust.policy", "Capabilities", DoctorStatus.FAIL, "Trust Kernel",
                "policy structure is invalid",
                remediation="Correct invalid Trust modes, capabilities, or scope lists.",
                required=True,
            )
            return
        self.add(
            "trust.policy", "Capabilities", DoctorStatus.PASS, "Trust Kernel",
            "policy loaded; actions were not executed", required=True,
        )

    def _gateways(self, settings: Settings) -> None:
        gateway_specs = (
            (
                "telegram", "Telegram", bool(settings.telegram_token),
                sorted(settings.telegram_allowed_users or parse_telegram_allowed_users()),
                "TELEGRAM_ALLOWED_USER or TELEGRAM_ALLOWED_USERS",
            ),
            (
                "discord", "Discord", bool(os.getenv("DISCORD_BOT_TOKEN")),
                [item.strip() for item in os.getenv("DISCORD_ALLOWED_USER", "").split(",")
                 if item.strip()],
                "DISCORD_ALLOWED_USER",
            ),
        )
        for key, title, configured, allowed, allow_env in gateway_specs:
            if not configured:
                status, detail = DoctorStatus.OPTIONAL, "not configured"
            elif not allowed:
                status, detail = DoctorStatus.WARN, "configured but safely locked; sender allowlist is empty"
            else:
                status, detail = DoctorStatus.PASS, f"configured with {len(allowed)} allowed sender(s)"
            self.add(
                f"gateway.{key}", "Integrations", status, title, detail,
                remediation=f"Set {allow_env} to the permitted sender IDs." if status is DoctorStatus.WARN else "",
                metadata={"configured": configured, "allowed_sender_count": len(allowed)},
            )
        required_whatsapp = {
            "access token": bool(settings.whatsapp_token),
            "phone number ID": bool(settings.whatsapp_phone_number_id),
            "app secret": bool(os.getenv("WHATSAPP_APP_SECRET")),
            "verification token": bool(os.getenv("WHATSAPP_VERIFY_TOKEN")),
        }
        whatsapp_configured = any(required_whatsapp.values())
        allowed_phone = bool(os.getenv("WHATSAPP_ALLOWED_PHONE", "").strip())
        raw_bound = os.getenv("WHATSAPP_MAX_BODY_BYTES", str(1024 * 1024)).strip()
        try:
            body_bound = int(raw_bound)
            bound_valid = WHATSAPP_BODY_MIN <= body_bound <= WHATSAPP_BODY_MAX
        except ValueError:
            body_bound, bound_valid = 0, False
        missing = [name for name, present in required_whatsapp.items() if not present]
        if not whatsapp_configured:
            status, detail = DoctorStatus.OPTIONAL, "not configured"
        elif missing:
            status, detail = DoctorStatus.WARN, "partially configured; missing " + ", ".join(missing)
        elif not allowed_phone:
            status, detail = DoctorStatus.WARN, "configured but safely locked; sender allowlist is empty"
        elif not bound_valid:
            status, detail = DoctorStatus.WARN, "configured but request-body limit is invalid"
        else:
            status, detail = DoctorStatus.PASS, f"configured; restricted sender; body limit {body_bound} bytes"
        self.add(
            "gateway.whatsapp", "Integrations", status, "WhatsApp", detail,
            remediation=(
                "Complete the missing settings, set WHATSAPP_ALLOWED_PHONE, and use a body limit from 1024 to 10485760 bytes."
                if status is DoctorStatus.WARN
                else ""
            ),
            metadata={
                "configured": whatsapp_configured,
                "allowed_sender_configured": allowed_phone,
                "body_limit_valid": bound_valid,
                "body_limit_bytes": body_bound if bound_valid else None,
            },
        )

    def _mcp(self, settings: Settings) -> None:
        config_path = settings.home / "mcp.json"
        if not config_path.exists():
            self.add(
                "mcp.config", "Integrations", DoctorStatus.OPTIONAL, "MCP",
                "not configured; no process was launched",
            )
            return
        try:
            with config_path.open("rb") as handle:
                raw_config = handle.read(MAX_MCP_CONFIG_BYTES + 1)
            if len(raw_config) > MAX_MCP_CONFIG_BYTES:
                raise ValueError("configuration exceeds the 1 MiB inspection limit")
            data = json.loads(raw_config.decode("utf-8"))
            servers = data.get("servers") if isinstance(data, dict) else None
            if not isinstance(servers, list):
                raise TypeError("servers must be a list")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self.add(
                "mcp.config", "Integrations", DoctorStatus.WARN, "MCP",
                "mcp.json is unreadable or invalid; no process was launched",
                remediation="Correct TIERU_HOME/mcp.json and rerun doctor.",
            )
            return
        valid: list[tuple[str, bool]] = []
        seen: set[str] = set()
        invalid = 0
        for item in servers:
            if not isinstance(item, dict):
                invalid += 1
                continue
            name = str(item.get("name", "")).strip()
            command = str(item.get("command", "")).strip()
            args = item.get("args", [])
            environment = item.get("env", {})
            if (
                not name
                or _MCP_SERVER_NAME.fullmatch(name) is None
                or name in seen
                or not command
                or not isinstance(args, list)
                or any(not isinstance(arg, str) for arg in args)
                or not isinstance(environment, dict)
            ):
                invalid += 1
                continue
            seen.add(name)
            if Path(command).is_absolute() or "/" in command or "\\" in command:
                executable = Path(command)
                if not executable.is_absolute():
                    executable = config_path.parent / executable
                found = executable.is_file()
            else:
                found = shutil.which(command) is not None
            valid.append((name, found))
        summary_status = DoctorStatus.PASS if valid and not invalid else DoctorStatus.WARN
        self.add(
            "mcp.config", "Integrations", summary_status, "MCP",
            f"{len(valid)} server(s) configured; {invalid} invalid; no process was launched",
            remediation="Correct invalid MCP server entries." if invalid or not valid else "",
            metadata={"server_count": len(valid), "invalid_server_count": invalid},
        )
        for name, found in valid:
            self.add(
                f"mcp.server.{name}", "Integrations",
                DoctorStatus.PASS if found else DoctorStatus.WARN,
                f"MCP server {name}",
                f"executable {'found' if found else 'not found'}; startup requires Trust approval",
                remediation="Install or correct the configured executable." if not found else "",
                metadata={"server_name": name, "executable_found": found},
            )

    @staticmethod
    def _playwright_dependency() -> bool:
        try:
            return importlib.util.find_spec("playwright.sync_api") is not None
        except (ImportError, ModuleNotFoundError):
            return False

    @staticmethod
    def _playwright_browser() -> bool:
        roots = []
        configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        if configured and configured != "0":
            roots.append(Path(configured).expanduser())
        if sys.platform == "win32" and os.getenv("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
        elif sys.platform == "darwin":
            roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
        else:
            roots.append(Path.home() / ".cache" / "ms-playwright")
        return any(
            root.is_dir()
            and any(root.glob("chromium-*"))
            or root.is_dir()
            and any(root.glob("chromium_headless_shell-*"))
            for root in roots
        )

    def _browser(self, settings: Settings) -> None:
        if not settings.browser_enabled:
            self.add(
                "browser.support", "Capabilities", DoctorStatus.OPTIONAL, "Browser",
                "disabled",
            )
            return
        dependency = self._playwright_dependency()
        browser = dependency and self._playwright_browser()
        if not dependency:
            detail = "enabled but the Playwright Python dependency is missing"
        elif not browser:
            detail = "enabled but a Playwright Chromium binary was not detected"
        else:
            detail = "enabled; Playwright dependency and Chromium binary detected"
        self.add(
            "browser.support", "Capabilities",
            DoctorStatus.PASS if browser else DoctorStatus.WARN,
            "Browser", detail,
            remediation=(
                'Run: python -m pip install "tieru-agent[browser]"; then: playwright install chromium'
                if not browser
                else ""
            ),
            metadata={"enabled": True, "dependency_available": dependency, "browser_detected": browser},
        )

    def _memory(self, settings: Settings, state: _StateHealth) -> None:
        required = {"facts", "episodes", "graph_entities", "graph_relations"}
        missing = required - state.tables
        detail = (
            f"semantic={settings.semantic_store}; episodic={settings.episodic_store}; procedural and graph schemas available"
            if state.ok and not missing
            else "required memory schemas are unavailable"
        )
        ok = state.ok and not missing
        credential_names = []
        if settings.semantic_store == "supabase":
            credential_names = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "OPENAI_API_KEY"]
            ok = ok and importlib.util.find_spec("supabase") is not None and all(
                os.getenv(name) for name in credential_names
            )
        if settings.episodic_store == "notion":
            credential_names += ["NOTION_TOKEN", "NOTION_EPISODES_DATABASE_ID"]
            ok = ok and importlib.util.find_spec("notion_client") is not None and all(
                os.getenv(name) for name in ("NOTION_TOKEN", "NOTION_EPISODES_DATABASE_ID")
            )
        self.add(
            "memory.system", "Runtime State", DoctorStatus.PASS if ok else DoctorStatus.FAIL,
            "Memory", detail if ok else "configured memory storage is unavailable",
            remediation=(
                "Install the configured memory extra and provide its required environment variables."
                if not ok and credential_names
                else "Check the SQLite schema and TIERU_HOME permissions." if not ok else ""
            ),
            required=True,
            metadata={"semantic_store": settings.semantic_store, "episodic_store": settings.episodic_store},
        )

    def _skills(self, settings: Settings) -> None:
        built_in_roots = bundled_skill_dirs()
        user_root = settings.home / "skills"

        def files_in(roots: list[Path]) -> tuple[list[Path], bool]:
            files: list[Path] = []
            scan_error = False
            for root in roots:
                if not root.is_dir():
                    continue
                if not os.access(root, os.R_OK):
                    scan_error = True
                    continue
                try:
                    remaining = MAX_SKILL_FILES + 1 - len(files)
                    files.extend(islice(root.rglob("SKILL.md"), max(0, remaining)))
                except OSError:
                    scan_error = True
                if len(files) > MAX_SKILL_FILES:
                    scan_error = True
                    files = files[:MAX_SKILL_FILES]
                    break
            return files, scan_error

        built_in_files, built_in_scan_error = files_in(built_in_roots)
        user_files, user_scan_error = files_in([user_root])

        def valid(file: Path) -> bool:
            try:
                return (
                    file.stat().st_size <= MAX_SKILL_BYTES
                    and _parse_text(file.read_text(encoding="utf-8"), file) is not None
                )
            except (OSError, UnicodeError):
                return False

        invalid_built_in = sum(not valid(file) for file in built_in_files) + int(
            built_in_scan_error
        )
        invalid_user = sum(not valid(file) for file in user_files) + int(user_scan_error)
        if invalid_built_in:
            status, required = DoctorStatus.FAIL, True
            detail = f"{invalid_built_in} packaged skill(s) failed validation"
        elif invalid_user:
            status, required = DoctorStatus.WARN, False
            detail = (
                f"{len(built_in_files)} built-in, {len(user_files)} user; "
                f"{invalid_user} user skill(s) failed validation"
            )
        else:
            status, required = DoctorStatus.PASS, True
            detail = f"{len(built_in_files)} built-in, {len(user_files)} user; all readable"
        self.add(
            "skills.system", "Runtime State", status, "Skills", detail,
            remediation="Review invalid SKILL.md files; Doctor did not modify or delete them."
            if status is not DoctorStatus.PASS else "",
            required=required,
            metadata={
                "built_in_count": len(built_in_files),
                "user_count": len(user_files),
                "invalid_built_in_count": invalid_built_in,
                "invalid_user_count": invalid_user,
            },
        )

    def _replay(self, settings: Settings, state: _StateHealth) -> None:
        ok = state.ok and {"replay_runs", "replay_events"} <= state.tables
        valid_retention = settings.replay_max_runs > 0 and settings.replay_max_age_days > 0
        ok = ok and valid_retention
        self.add(
            "replay.system", "Runtime State", DoctorStatus.PASS if ok else DoctorStatus.FAIL,
            "Replay",
            (
                f"local structured storage available; retention {settings.replay_max_runs} runs/{settings.replay_max_age_days} days"
                if ok
                else "schema or retention configuration is invalid"
            ),
            remediation="Correct Replay retention bounds or inspect the local SQLite schema." if not ok else "",
            required=True,
            metadata={
                "max_runs": settings.replay_max_runs,
                "max_age_days": settings.replay_max_age_days,
            },
        )

    def _shadow(self, settings: Settings) -> None:
        status = DoctorStatus.PASS if settings.shadow_enabled else DoctorStatus.OPTIONAL
        detail = (
            f"enabled; minimum {settings.shadow_min_occurrences} occurrences"
            if settings.shadow_enabled
            else "disabled"
        )
        self.add(
            "shadow.system", "Capabilities", status, "Shadow", detail,
            metadata={"enabled": settings.shadow_enabled},
        )

    def _capsule(self, settings: Settings, home_ok: bool) -> None:
        bounds_ok = (
            settings.capsule_max_archive_bytes > 0
            and settings.capsule_max_files > 0
            and settings.capsule_max_file_bytes > 0
            and settings.capsule_max_uncompressed_bytes >= settings.capsule_max_file_bytes
            and settings.capsule_max_compression_ratio > 0
        )
        path = settings.home / "capsules"
        writable, _detail = _writeable_path(path, probe_existing=True) if home_ok else (False, "")
        ok = bounds_ok and writable
        self.add(
            "capsule.system", "Runtime State", DoctorStatus.PASS if ok else DoctorStatus.WARN,
            "Capsule",
            f"{path.resolve(strict=False)}; bounds valid" if ok else "path or bounds are unavailable",
            remediation="Check TIERU_HOME permissions and Capsule size bounds." if not ok else "",
            metadata={"path": str(path.resolve(strict=False)), "bounds_valid": bounds_ok},
        )


def run_doctor(
    *,
    settings: Settings | None = None,
    selection: dict[str, bool] | None = None,
    python_version: tuple[int, ...] | None = None,
    ollama_factory: Callable[[str], Any] = OllamaIntegration,
) -> DoctorReport:
    return DoctorRunner(
        settings=settings,
        selection=selection,
        python_version=python_version,
        ollama_factory=ollama_factory,
    ).run()


_CATEGORY_ORDER = (
    "Runtime",
    "Configuration",
    "Models",
    "Runtime State",
    "Capabilities",
    "Integrations",
)


def render_human(report: DoctorReport) -> str:
    lines = ["Tieru Doctor"]
    for category in _CATEGORY_ORDER:
        items = [check for check in report.checks if check.category == category]
        if not items:
            continue
        lines.extend(("", category))
        for check in items:
            lines.append(f"{check.status.value:<8} {check.title}: {check.detail}")
            if check.remediation and check.status in {DoctorStatus.FAIL, DoctorStatus.WARN}:
                lines.append(f"         Suggested action: {check.remediation}")
    lines.extend(("", "Overall", report.overall_status.replace("_", " ")))
    lines.append("Tieru doctor: OK" if report.ready else "Tieru doctor: NOT READY")
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    return json.dumps(report.public(), indent=2, sort_keys=True)

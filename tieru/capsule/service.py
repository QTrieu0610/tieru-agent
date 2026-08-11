"""Offline Capsule export, preview, and transactional additive import."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from tieru import __version__
from tieru.capsule.manifest import (
    FORMAT,
    FORMAT_VERSION,
    SCHEMA_VERSION,
    encode_json,
    read_verified,
    sha256,
)
from tieru.capsule.models import CapsuleLimits, ImportPlan
from tieru.capsule.security import CapsuleError, reject_secret_keys, scan_payloads
from tieru.memory.personal import contains_secret
from tieru.memory.procedural.loader import _parse_text
from tieru.trust import ActionRequest, Capability

PORTABLE_SCOPES = ("identity", "memory", "skills", "preferences", "trust", "fabric")
HISTORY_SCOPES = (*PORTABLE_SCOPES, "replay", "forge", "shadow")
ALL_SCOPES = frozenset(HISTORY_SCOPES)
IDENTITY_FILES = ("SOUL.md", "IDENTITY.md", "PREFERENCES.md")
DB_EXPORTS = {
    "memory": ("facts", "episodes", "graph_entities", "graph_relations"),
    "replay": ("replay_runs", "replay_events"),
    "shadow": ("shadow_patterns", "shadow_suggestions", "shadow_observations", "shadow_settings"),
}
TABLE_COLUMNS = {
    "facts": {"id", "subject", "content", "source", "created_at", "updated_at", "provenance", "importance", "trusted", "fingerprint"},
    "episodes": {"id", "happened_at", "summary", "created_at", "updated_at", "source", "provenance", "importance", "trusted", "fingerprint"},
    "graph_entities": {"id", "entity_type", "canonical_name", "normalized_name", "metadata_json", "created_at", "updated_at"},
    "graph_relations": {"id", "subject_id", "predicate", "object_id", "confidence", "importance", "source_type", "source_ref", "valid_from", "valid_to", "status", "superseded_by", "created_at", "updated_at"},
    "replay_runs": {"id", "session_id", "source", "started_at", "completed_at", "status", "role", "model", "provider", "iterations", "latency_ms", "event_count", "tool_count", "trust_decision_count", "input_preview", "output_preview", "error_code", "error_summary"},
    "replay_events": {"id", "run_id", "sequence", "timestamp", "category", "event_type", "node", "tool", "role", "model", "provider", "duration_ms", "payload_json"},
    "shadow_patterns": {"id", "workflow_signature", "status", "occurrence_count", "successful_count", "verification_count", "first_seen_at", "last_seen_at", "evidence_json", "tools_json", "operations_json", "capabilities_json", "confidence", "suppress_until_count", "snoozed_until", "forge_draft_id", "metadata_json"},
    "shadow_suggestions": {"id", "pattern_id", "workflow_signature", "status", "occurrence_count", "confidence", "representative_runs_json", "suggested_name", "summary", "tools_json", "capabilities_json", "explanation_json", "created_at", "updated_at", "snoozed_until", "forge_draft_id"},
    "shadow_observations": {"run_id", "pattern_id", "result", "observed_at"},
    "shadow_settings": {"key", "value", "updated_at"},
}
TABLE_REQUIRED = {
    "facts": {"subject", "content"}, "episodes": {"happened_at", "summary"},
    "graph_entities": {"id", "entity_type", "canonical_name", "normalized_name"},
    "graph_relations": {"id", "subject_id", "predicate", "object_id"},
    "replay_runs": {"id", "started_at"},
    "replay_events": {"id", "run_id", "sequence", "timestamp", "category", "event_type"},
    "shadow_patterns": {"id", "workflow_signature"},
    "shadow_suggestions": {"id", "pattern_id", "workflow_signature"},
    "shadow_observations": {"run_id"}, "shadow_settings": {"key", "value"},
}
_SKILL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _capsule_id() -> str:
    return "capsule_" + uuid.uuid4().hex


def _import_id() -> str:
    return "import_" + uuid.uuid4().hex


def _json(payload: bytes, name: str) -> Any:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError(f"malformed JSON in {name}: {exc}") from exc
    return value


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _machine_paths(value: object, prefix: str = "") -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_machine_paths(child, f"{prefix}.{key}".strip(".")))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_machine_paths(child, f"{prefix}[{index}]"))
    elif isinstance(value, str) and (
        Path(value).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        found.append({"field": prefix, "source": value, "status": "requires-remap"})
    return found


def _merge_defaults(imported: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Current installation wins; imported values fill only missing keys."""
    result = dict(imported)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_defaults(result[key], value)
        else:
            result[key] = value
    return result


def _has_private_reasoning(value: object) -> bool:
    forbidden = {"reasoning", "thinking", "scratchpad", "chain_of_thought", "chain-of-thought"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or _has_private_reasoning(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_has_private_reasoning(child) for child in value)
    return False


class CapsuleService:
    def __init__(self, settings, conn: sqlite3.Connection, *, limits: CapsuleLimits | None = None,
                 trust_kernel=None):
        self.settings = settings
        self.conn = conn
        self.limits = limits or CapsuleLimits(
            max_archive_bytes=settings.capsule_max_archive_bytes,
            max_files=settings.capsule_max_files,
            max_file_bytes=settings.capsule_max_file_bytes,
            max_uncompressed_bytes=settings.capsule_max_uncompressed_bytes,
            max_compression_ratio=float(settings.capsule_max_compression_ratio),
        )
        self.trust_kernel = trust_kernel

    def _authorize(self, operation: str, target: str, *, write: bool) -> None:
        if self.trust_kernel is None:
            return
        capability = Capability.LOCAL_WRITE if write else Capability.LOCAL_READ
        decision = self.trust_kernel.authorize(
            ActionRequest(
                tool_name="capsule", capabilities=(capability,), operation=operation,
                target=target, scope="capsule", resource_type="portable_snapshot",
                local=True, read_only=not write, reversible=True,
            ), default_policy="prompt", approval_args={"target": Path(target).name},
        )
        if not decision.allowed:
            raise PermissionError(f"Trust denied {operation}: {decision.explanation}")

    @staticmethod
    def scopes(profile: str, include: tuple[str, ...] = (), exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
        if profile not in ("portable", "full-local-history"):
            raise CapsuleError("export profile must be portable or full-local-history")
        selected = set(PORTABLE_SCOPES if profile == "portable" else HISTORY_SCOPES)
        unknown = (set(include) | set(exclude)) - ALL_SCOPES
        if unknown:
            raise CapsuleError("unknown Capsule scope(s): " + ", ".join(sorted(unknown)))
        selected.update(include)
        selected.difference_update(exclude)
        return tuple(scope for scope in HISTORY_SCOPES if scope in selected)

    def _export_files(self, scopes: tuple[str, ...]) -> tuple[dict[str, bytes], dict[str, int]]:
        files: dict[str, bytes] = {}
        counts: dict[str, int] = {}
        home = self.settings.home
        if "identity" in scopes:
            count = 0
            for name in IDENTITY_FILES:
                source = home / name
                if source.is_file() and not source.is_symlink():
                    payload = source.read_bytes()
                    if len(payload) > self.limits.max_file_bytes:
                        raise CapsuleError(f"identity file exceeds size limit: {name}")
                    files[f"identity/{name}"] = payload
                    count += 1
            counts["identity_files"] = count
        for scope, tables in DB_EXPORTS.items():
            if scope not in scopes:
                continue
            for table in tables:
                records = _rows(self.conn, table)
                validation = ImportPlan()
                if not self._valid_records(table, records, validation):
                    raise CapsuleError("unsafe local structured state: " + "; ".join(validation.errors))
                # Replay exports only the already-bounded normalized store. It
                # never contains prompts, hidden reasoning, or chain-of-thought.
                files[f"{scope}/{table}.json"] = encode_json(records)
                counts[table] = len(records)
        if "memory" in scopes:
            memory_md = home / "MEMORY.md"
            if memory_md.is_file() and not memory_md.is_symlink():
                files["memory/MEMORY.md"] = memory_md.read_bytes()
        if "skills" in scopes:
            index: list[dict[str, Any]] = []
            root = home / "skills"
            if root.is_dir():
                for source in sorted(root.glob("*/SKILL.md")):
                    if source.is_symlink() or source.parent.is_symlink() or source.parent.name.startswith("_"):
                        continue
                    text = source.read_text(encoding="utf-8")
                    skill = _parse_text(text, source)
                    if skill is None or not _SKILL_ID.fullmatch(skill.name) or skill.name != source.parent.name:
                        continue
                    payload = text.encode()
                    if contains_secret(text):
                        raise CapsuleError(f"user skill appears to contain a secret: {skill.name}")
                    files[f"skills/{skill.name}/SKILL.md"] = payload
                    index.append({
                        "skill_id": skill.name, "version": "unversioned",
                        "sha256": sha256(payload), "provenance": "user-owned",
                    })
            files["skills/index.json"] = encode_json(index)
            counts["skills"] = len(index)
        redacted = self.settings.redacted()
        if "preferences" in scopes:
            preferences = {
                "memory_write_policy": self.settings.memory_write_policy,
                "memory_max_records": self.settings.memory_max_records,
                "semantic_store": self.settings.semantic_store,
                "episodic_store": self.settings.episodic_store,
                "embedding_model": self.settings.embedding_model,
                "retrieval_top_k": self.settings.retrieval_top_k,
                "consolidate_every": self.settings.consolidate_every,
                "browser_enabled": self.settings.browser_enabled,
                "browser_allowed_domains": list(self.settings.browser_allowed_domains),
                "browser_allow_local_fixture": self.settings.browser_allow_local_fixture,
                "browser_timeout_seconds": self.settings.browser_timeout_seconds,
                "browser_max_actions": self.settings.browser_max_actions,
            }
            reject_secret_keys(preferences, "preferences")
            files["config/preferences.json"] = encode_json(preferences)
            counts["preferences"] = 1
        if "fabric" in scopes:
            safe_fabric = redacted["fabric"]
            candidate_fields = {
                "provider", "model", "protocol", "roles", "local", "enabled",
                "capabilities", "context_limit", "output_limit", "cost_tier",
                "latency_tier", "privacy_class", "preference", "base_url",
            }
            fabric = {
                "enabled": safe_fabric["enabled"],
                "default_mode": safe_fabric["default_mode"],
                "use_small_classifier": safe_fabric["use_small_classifier"],
                "quick_max_tokens": safe_fabric["quick"]["max_tokens"],
                "quick_max_iterations": safe_fabric["quick"]["max_iterations"],
                "quick_history_turns": safe_fabric["quick"]["history_turns"],
                "agent_max_tokens": safe_fabric["agent"]["max_tokens"],
                "agent_max_iterations": safe_fabric["agent"]["max_iterations"],
                "deep_max_tokens": safe_fabric["deep"]["max_tokens"],
                "deep_max_iterations": safe_fabric["deep"]["max_iterations"],
                "deep_history_turns": safe_fabric["deep"]["history_turns"],
                "deep_verification": safe_fabric["deep"]["verification"],
                "routing_policy": safe_fabric["routing_policy"],
                "min_history_samples": safe_fabric["min_history_samples"],
                "availability_ttl_seconds": safe_fabric["availability_ttl_seconds"],
                "max_fallbacks": safe_fabric["max_fallbacks"],
                "weights": safe_fabric["weights"],
                "models": {
                    alias: {key: value for key, value in model.items() if key in candidate_fields}
                    for alias, model in safe_fabric["models"].items()
                },
            }
            reject_secret_keys(fabric, "fabric")
            files["config/fabric.json"] = encode_json(fabric)
            counts["fabric_config"] = 1
        if "trust" in scopes:
            trust = {
                "active": False,
                "policy": redacted["trust"],
                "machine_specific_paths": _machine_paths(redacted["trust"]),
                "import_requirement": "explicit-review",
            }
            reject_secret_keys(trust, "trust")
            files["trust/policy.json"] = encode_json(trust)
            counts["trust_policy"] = 1
        if "forge" in scopes:
            root = home / "forge" / "drafts"
            drafts = 0
            if root.is_dir():
                for directory in sorted(root.glob("draft_*")):
                    if not directory.is_dir() or directory.is_symlink():
                        continue
                    for name in ("SKILL.md", "metadata.json", "workflow.json", "evaluation.json"):
                        source = directory / name
                        if source.is_file() and not source.is_symlink():
                            files[f"forge/drafts/{directory.name}/{name}"] = source.read_bytes()
                    drafts += 1
            counts["forge_drafts"] = drafts
            validation = ImportPlan()
            draft_ids = {
                PurePosixPath(name).parts[2]
                for name in files if name.startswith("forge/drafts/")
            }
            self._validate_forge(files, draft_ids, validation)
            if validation.errors:
                raise CapsuleError("unsafe local Forge state: " + "; ".join(validation.errors))
        scan_payloads(files)
        return files, counts

    def export(self, target: Path, *, profile: str = "portable", include: tuple[str, ...] = (),
               exclude: tuple[str, ...] = (), dry_run: bool = False) -> dict[str, Any]:
        selected = self.scopes(profile, include, exclude)
        if not dry_run:
            self._authorize("capsule_export", str(target), write=True)
        self.conn.execute("SAVEPOINT capsule_export")
        try:
            files, counts = self._export_files(selected)
        finally:
            self.conn.execute("ROLLBACK TO capsule_export")
            self.conn.execute("RELEASE capsule_export")
        capsule_id = _capsule_id()
        manifest = {
            "format": FORMAT, "format_version": FORMAT_VERSION,
            "capsule_id": capsule_id, "created_at": _now(),
            "tieru_version": __version__, "schema_version": SCHEMA_VERSION,
            "export_profile": profile, "included_scopes": list(selected),
            "item_counts": counts, "source_platform": platform.system().lower(),
            "source_python_version": platform.python_version(), "encrypted": False,
            "compression": "deflate", "integrity_algorithm": "sha256",
            "files": [
                {"path": name, "size": len(payload), "sha256": sha256(payload)}
                for name, payload in sorted(files.items())
            ],
            "compatibility": {"min_tieru_version": "0.2.0", "max_format_version": 1},
        }
        if dry_run:
            return {"dry_run": True, "manifest": manifest, "target": str(target)}
        if target.suffix.lower() != ".tieru":
            target = target.with_suffix(target.suffix + ".tieru") if target.suffix else target.with_suffix(".tieru")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, payload in [("manifest.json", encode_json(manifest)), *sorted(files.items())]:
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100600 << 16
                    archive.writestr(info, payload)
            read_verified(temp, self.limits)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        self._audit("export", capsule_id, selected, target.name, warnings=[])
        self.conn.commit()
        return {"dry_run": False, "manifest": manifest, "target": str(target)}

    def inspect(self, source: Path) -> dict[str, Any]:
        self._authorize("capsule_inspect", str(source), write=False)
        manifest, _ = read_verified(source, self.limits)
        return {
            "format": manifest["format"], "format_version": manifest["format_version"],
            "capsule_id": manifest["capsule_id"], "created_at": manifest["created_at"],
            "scopes": manifest["included_scopes"], "counts": manifest["item_counts"],
            "integrity": "verified", "encrypted": manifest["encrypted"],
            "compatibility": manifest["compatibility"],
            "warnings": ["Integrity is verified; archive origin is not authenticated."],
        }

    def plan_import(self, source: Path) -> ImportPlan:
        self._authorize("capsule_import_preview", str(source), write=False)
        try:
            manifest, files = read_verified(source, self.limits)
            scan_payloads(files)
            plan = ImportPlan(
                capsule_id=manifest["capsule_id"], format_version=manifest["format_version"],
                valid=True, selected_scopes=list(manifest["included_scopes"]),
                integrity_verified=True, archive_sha256=sha256(source.read_bytes()),
                source_path=str(source),
            )
            self._build_plan(plan, files)
            plan.valid = not plan.errors
            return plan
        except CapsuleError as exc:
            message = str(exc)
            return ImportPlan(
                valid=False, errors=[message],
                secrets_detected="secret" in message.lower() or "credential" in message.lower(),
                source_path=str(source),
            )

    def _build_plan(self, plan: ImportPlan, files: dict[str, bytes]) -> None:
        for table in DB_EXPORTS["memory"]:
            name = f"memory/{table}.json"
            if name not in files:
                continue
            records = _json(files[name], name)
            if not isinstance(records, list):
                plan.errors.append(f"{name} must contain a list")
                continue
            if not self._valid_records(table, records, plan):
                continue
            creates = skips = 0
            conflicts: list[dict[str, Any]] = []
            for record in records:
                state, detail = self._record_state(table, record)
                if state == "new": creates += 1
                elif state == "same": skips += 1
                else: conflicts.append(detail)
            plan.creates[table] = creates
            plan.skips[table] = skips
            if conflicts:
                plan.conflicts[table] = conflicts
        memory_creates = plan.creates.get("facts", 0) + plan.creates.get("episodes", 0)
        current_memory = sum(
            int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("facts", "episodes")
        )
        if current_memory + memory_creates > self.settings.memory_max_records:
            plan.errors.append("import would exceed the configured personal-memory record limit")
        index = _json(files.get("skills/index.json", b"[]"), "skills/index.json")
        skill_creates = skill_skips = 0
        for entry in index if isinstance(index, list) else []:
            skill_id = str(entry.get("skill_id", ""))
            member = f"skills/{skill_id}/SKILL.md"
            try:
                text = files[member].decode()
                parsed = _parse_text(text, Path("SKILL.md"))
            except (KeyError, UnicodeDecodeError):
                parsed = None
            if (not _SKILL_ID.fullmatch(skill_id) or parsed is None or parsed.name != skill_id
                    or len(skill_id) > 64 or len(files.get(member, b"")) > 65_536
                    or contains_secret(text if parsed else "")
                    or entry.get("sha256") != sha256(files.get(member, b""))):
                plan.errors.append(f"invalid imported skill: {skill_id or '<missing>'}")
                continue
            destination = self.settings.home / "skills" / skill_id / "SKILL.md"
            if not destination.exists():
                skill_creates += 1
            elif (
                sha256(destination.read_text(encoding="utf-8").encode())
                == entry.get("sha256")
                == sha256(files[member])
            ):
                skill_skips += 1
            else:
                plan.conflicts.setdefault("skills", []).append({"id": skill_id, "action": "keep-existing"})
        if index:
            plan.creates["skills"] = skill_creates
            plan.skips["skills"] = skill_skips
        if "trust/policy.json" in files:
            trust = _json(files["trust/policy.json"], "trust/policy.json")
            try:
                reject_secret_keys(trust, "trust")
            except CapsuleError as exc:
                plan.errors.append(str(exc))
            if not isinstance(trust, dict) or not isinstance(trust.get("policy"), dict):
                plan.errors.append("imported Trust policy must be an inactive structured policy")
            elif trust.get("active") is not False:
                plan.errors.append("imported Trust policy must declare active=false")
            plan.trust_review_required = True
            plan.path_remaps.extend(trust.get("machine_specific_paths", []) if isinstance(trust, dict) else [])
            plan.creates["trust_pending_review"] = 1
        for name, label in (("config/preferences.json", "preferences"), ("config/fabric.json", "fabric")):
            if name in files:
                value = _json(files[name], name)
                try:
                    reject_secret_keys(value, label)
                except CapsuleError as exc:
                    plan.errors.append(str(exc))
                if not isinstance(value, dict):
                    plan.errors.append(f"{name} must contain an object")
                plan.updates[label] = 1
        forge_ids = {PurePosixPath(name).parts[2] for name in files if name.startswith("forge/drafts/")}
        if forge_ids:
            self._validate_forge(files, forge_ids, plan)
            plan.creates["forge_drafts"] = sum(not (self.settings.home / "forge" / "drafts" / item).exists() for item in forge_ids)
            for item in sorted(forge_ids):
                if (self.settings.home / "forge" / "drafts" / item).exists():
                    plan.conflicts.setdefault("forge_drafts", []).append({"id": item, "action": "keep-existing"})
        for scope in ("replay", "shadow"):
            for table in DB_EXPORTS[scope]:
                name = f"{scope}/{table}.json"
                if name in files:
                    records = _json(files[name], name)
                    if not isinstance(records, list):
                        plan.errors.append(f"{name} must contain a list")
                    elif not self._valid_records(table, records, plan):
                        continue
                    else:
                        plan.creates[table] = sum(self._pk_missing(table, record) for record in records)
                        plan.skips[table] = len(records) - plan.creates[table]
        for name in IDENTITY_FILES:
            member = f"identity/{name}"
            if member in files:
                destination = self.settings.home / name
                if not destination.exists():
                    plan.creates["identity_files"] = plan.creates.get("identity_files", 0) + 1
                elif destination.read_bytes() == files[member]:
                    plan.skips["identity_files"] = plan.skips.get("identity_files", 0) + 1
                else:
                    plan.conflicts.setdefault("identity_files", []).append({"path": name, "action": "keep-existing"})
        if plan.conflicts:
            plan.warnings.append("Conflicts are conservative: existing state is retained.")
        if plan.trust_review_required:
            plan.warnings.append("Imported Trust policy will remain inactive pending explicit review.")

    def _valid_records(self, table: str, records: list[Any], plan: ImportPlan) -> bool:
        valid = True
        allowed = TABLE_COLUMNS[table]
        required = TABLE_REQUIRED[table]
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                plan.errors.append(f"{table}[{index}] must be an object")
                valid = False
                continue
            keys = set(record)
            if not required <= keys or keys - allowed:
                plan.errors.append(f"{table}[{index}] has missing or unknown fields")
                valid = False
            for field in required:
                if record.get(field) in (None, "") or isinstance(
                    record.get(field), (dict, list, tuple)
                ):
                    plan.errors.append(f"{table}[{index}].{field} must be a scalar value")
                    valid = False
            for field, value in record.items():
                if field.endswith("_json"):
                    try:
                        inner_json = json.loads(str(value))
                        reject_secret_keys(inner_json, f"{table}[{index}].{field}")
                    except (json.JSONDecodeError, CapsuleError) as exc:
                        plan.errors.append(f"unsafe structured field {table}[{index}].{field}: {exc}")
                        valid = False
            if table == "shadow_settings":
                try:
                    reject_secret_keys(
                        {str(record.get("key")): record.get("value")},
                        f"shadow_settings[{index}]",
                    )
                except CapsuleError as exc:
                    plan.errors.append(str(exc))
                    valid = False
            if table == "graph_relations":
                try:
                    confidence = float(record.get("confidence", 0.8))
                    importance = float(record.get("importance", 0.5))
                except (TypeError, ValueError):
                    confidence = importance = -1
                if not (0 <= confidence <= 1 and 0 <= importance <= 1):
                    plan.errors.append(f"graph_relations[{index}] has invalid confidence/importance")
                    valid = False
                if record.get("status", "active") not in {"active", "superseded", "contradicted", "archived"}:
                    plan.errors.append(f"graph_relations[{index}] has invalid status")
                    valid = False
            if table == "replay_events":
                payload = str(record.get("payload_json", "{}"))
                if len(payload.encode()) > self.settings.replay_max_event_payload_bytes:
                    plan.errors.append(f"replay_events[{index}] payload exceeds local Replay bounds")
                    valid = False
                else:
                    try:
                        inner = json.loads(payload)
                        reject_secret_keys(inner, f"replay_events[{index}].payload")
                        if _has_private_reasoning(inner):
                            raise CapsuleError("private reasoning fields are not portable")
                    except (json.JSONDecodeError, CapsuleError) as exc:
                        plan.errors.append(f"unsafe Replay payload: {exc}")
                        valid = False
        return valid

    @staticmethod
    def _validate_forge(files: dict[str, bytes], draft_ids: set[str], plan: ImportPlan) -> None:
        expected = {"SKILL.md", "metadata.json", "workflow.json", "evaluation.json"}
        for draft_id in draft_ids:
            prefix = f"forge/drafts/{draft_id}/"
            names = {name.removeprefix(prefix) for name in files if name.startswith(prefix)}
            if names != expected:
                plan.errors.append(f"Forge draft {draft_id} has incomplete structure")
                continue
            try:
                content = files[prefix + "SKILL.md"].decode().replace("\r\n", "\n")
                metadata = _json(files[prefix + "metadata.json"], prefix + "metadata.json")
                workflow = _json(files[prefix + "workflow.json"], prefix + "workflow.json")
                evaluation = _json(files[prefix + "evaluation.json"], prefix + "evaluation.json")
                parsed = _parse_text(content, Path("SKILL.md"))
                if parsed is None or contains_secret(content) or not all(
                    isinstance(item, dict) for item in (metadata, workflow, evaluation)
                ):
                    raise CapsuleError("invalid draft content")
                reject_secret_keys(metadata, "forge.metadata")
                reject_secret_keys(workflow, "forge.workflow")
                reject_secret_keys(evaluation, "forge.evaluation")
            except (UnicodeDecodeError, CapsuleError) as exc:
                plan.errors.append(f"invalid Forge draft {draft_id}: {exc}")

    def _record_state(self, table: str, record: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(record, dict):
            return "conflict", {"reason": "record-not-object"}
        if table == "facts":
            rows = self.conn.execute("SELECT * FROM facts WHERE subject=? AND content=?", (record.get("subject"), record.get("content"))).fetchall()
        elif table == "episodes":
            rows = self.conn.execute("SELECT * FROM episodes WHERE happened_at=? AND summary=?", (record.get("happened_at"), record.get("summary"))).fetchall()
        elif table == "graph_entities":
            rows = self.conn.execute("SELECT * FROM graph_entities WHERE entity_type=? AND normalized_name=?", (record.get("entity_type"), record.get("normalized_name"))).fetchall()
        else:
            # Relations are resolved through imported entity identities during execution.
            rows = []
            for row in self.conn.execute("SELECT * FROM graph_relations WHERE predicate=? AND source_ref=?", (record.get("predicate"), record.get("source_ref", ""))):
                rows.append(row)
        if not rows:
            return "new", {}
        comparable = {k: v for k, v in record.items() if k != "id"}
        for row in rows:
            existing = dict(row)
            if all(existing.get(k) == v for k, v in comparable.items()):
                return "same", {}
        return "conflict", {"id": record.get("id"), "action": "preserve-both-or-reuse", "table": table}

    def _pk_missing(self, table: str, record: dict[str, Any]) -> bool:
        key = "key" if table == "shadow_settings" else "run_id" if table == "shadow_observations" else "id"
        if key not in record:
            return False
        return self.conn.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (record[key],)).fetchone() is None

    def import_capsule(self, source: Path, *, plan: ImportPlan | None = None,
                       dry_run: bool = False) -> dict[str, Any]:
        approved = plan or self.plan_import(source)
        if dry_run:
            return approved.to_dict()
        if not approved.valid or not approved.integrity_verified:
            raise CapsuleError("cannot execute an invalid or unverified ImportPlan")
        # Re-read so a file swapped after preview cannot bypass integrity.
        manifest, files = read_verified(source, self.limits)
        if approved.capsule_id != manifest["capsule_id"]:
            raise CapsuleError("ImportPlan does not match Capsule")
        if approved.selected_scopes != manifest["included_scopes"]:
            raise CapsuleError("ImportPlan scopes do not match Capsule")
        if approved.archive_sha256 != sha256(source.read_bytes()):
            raise CapsuleError("Capsule changed after ImportPlan approval")
        self._authorize("capsule_import", str(source), write=True)
        import_id = _import_id()
        backup = self._backup(import_id)
        staged = self.settings.home / "capsule" / ".staging" / import_id
        staged.mkdir(parents=True, exist_ok=False)
        created_paths: list[Path] = []
        config_changed = False
        try:
            self._stage_files(staged, files, approved)
            self.conn.execute("BEGIN IMMEDIATE")
            self._import_db(files, approved, manifest["capsule_id"])
            for staged_path, destination, overwrite in self._staged_destinations(staged):
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and not overwrite:
                    continue
                replacement = destination.with_name(
                    f".{destination.name}.capsule-{uuid.uuid4().hex}.tmp"
                )
                os.replace(staged_path, replacement)
                os.replace(replacement, destination)
                if overwrite:
                    config_changed = True
                else:
                    created_paths.append(destination)
            self.conn.execute(
                "INSERT INTO capsule_audits VALUES (?, 'import', ?, ?, ?, ?, ?, ?, ?, ?)",
                (import_id, manifest["capsule_id"], _now(), json.dumps(approved.selected_scopes),
                 source.name, sum(approved.creates.values()), sum(approved.skips.values()),
                 sum(len(v) for v in approved.conflicts.values()), json.dumps(approved.warnings)),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            if config_changed and (backup / "config.yaml").exists():
                config_target = self.settings.config_path or self.settings.home / "config.yaml"
                shutil.copy2(backup / "config.yaml", config_target)
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        return {"import_id": import_id, "capsule_id": approved.capsule_id,
                "backup": str(backup), "plan": approved.to_dict(), "status": "imported"}

    def _backup(self, import_id: str) -> Path:
        backup = self.settings.home / "backups" / import_id
        backup.mkdir(parents=True, exist_ok=False)
        db_path = backup / "state.db"
        dest = sqlite3.connect(db_path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        config = self.settings.config_path or self.settings.home / "config.yaml"
        if config.is_file():
            shutil.copy2(config, backup / "config.yaml")
        self._prune_backups(keep=5)
        return backup

    def _prune_backups(self, *, keep: int) -> None:
        root = (self.settings.home / "backups").resolve()
        candidates = sorted(
            (path for path in root.glob("import_*") if path.is_dir() and not path.is_symlink()),
            key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
        )
        for path in candidates[keep:]:
            target = path.resolve()
            if root not in target.parents:
                raise CapsuleError("backup cleanup target escaped TIERU_HOME/backups")
            shutil.rmtree(target)

    def _stage_files(self, staged: Path, files: dict[str, bytes], plan: ImportPlan) -> None:
        for name in IDENTITY_FILES:
            member = f"identity/{name}"
            destination = self.settings.home / name
            if member in files and not destination.exists():
                target = staged / "home" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[member])
        index = _json(files.get("skills/index.json", b"[]"), "skills/index.json")
        for entry in index if isinstance(index, list) else []:
            skill_id = entry["skill_id"]
            destination = self.settings.home / "skills" / skill_id / "SKILL.md"
            if not destination.exists():
                target = staged / "home" / "skills" / skill_id / "SKILL.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[f"skills/{skill_id}/SKILL.md"])
        forge_ids = sorted({PurePosixPath(name).parts[2] for name in files if name.startswith("forge/drafts/")})
        for draft_id in forge_ids:
            if not re.fullmatch(r"draft_[a-z0-9]{8,64}", draft_id):
                raise CapsuleError(f"invalid Forge draft ID: {draft_id}")
            destination = self.settings.home / "forge" / "drafts" / draft_id
            if destination.exists():
                continue
            for member, payload in files.items():
                prefix = f"forge/drafts/{draft_id}/"
                if member.startswith(prefix):
                    filename = member.removeprefix(prefix)
                    if filename not in ("SKILL.md", "metadata.json", "workflow.json", "evaluation.json"):
                        raise CapsuleError("unexpected Forge draft member")
                    target = staged / "home" / "forge" / "drafts" / draft_id / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
        if "trust/policy.json" in files:
            target = staged / "home" / "capsule" / "pending-trust" / f"{plan.capsule_id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files["trust/policy.json"])
        config_path = self.settings.config_path or self.settings.home / "config.yaml"
        imported: dict[str, Any] = {"version": 1}
        if "config/preferences.json" in files:
            imported.update(_json(files["config/preferences.json"], "config/preferences.json"))
        if "config/fabric.json" in files:
            imported["fabric"] = _json(files["config/fabric.json"], "config/fabric.json")
        if len(imported) > 1:
            current = {}
            if config_path.is_file():
                current = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            merged = _merge_defaults(imported, current)
            target = staged / "config.yaml"
            target.write_text(yaml.safe_dump(merged, sort_keys=True), encoding="utf-8")
            (staged / "config-target.txt").write_text(str(config_path.resolve()), encoding="utf-8")

    def _staged_destinations(self, staged: Path):
        home_stage = staged / "home"
        if home_stage.is_dir():
            for path in sorted(home_stage.rglob("*")):
                if path.is_file():
                    yield path, self.settings.home / path.relative_to(home_stage), False
        config = staged / "config.yaml"
        target_marker = staged / "config-target.txt"
        if config.exists() and target_marker.exists():
            target = Path(target_marker.read_text(encoding="utf-8"))
            yield config, target, target.exists()

    def _import_db(self, files: dict[str, bytes], plan: ImportPlan, capsule_id: str) -> None:
        source_ref = f"capsule:{capsule_id}"
        entities: dict[int, int] = {}
        records = _json(files.get("memory/graph_entities.json", b"[]"), "memory/graph_entities.json")
        for record in records:
            existing = self.conn.execute(
                "SELECT id FROM graph_entities WHERE entity_type=? AND normalized_name=?",
                (record["entity_type"], record["normalized_name"]),
            ).fetchone()
            if existing:
                entities[int(record["id"])] = int(existing[0])
            else:
                cursor = self.conn.execute(
                    "INSERT INTO graph_entities(entity_type,canonical_name,normalized_name,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    tuple(record.get(k, "") for k in ("entity_type", "canonical_name", "normalized_name", "metadata_json", "created_at", "updated_at")),
                )
                entities[int(record["id"])] = int(cursor.lastrowid)
        for table in ("facts", "episodes"):
            for record in _json(files.get(f"memory/{table}.json", b"[]"), f"memory/{table}.json"):
                state, _ = self._record_state(table, record)
                if state == "same":
                    continue
                if table == "facts":
                    self.conn.execute(
                        "INSERT INTO facts(subject,content,source,created_at,updated_at,provenance,importance,trusted,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
                        (record["subject"], record["content"], "capsule_import", record.get("created_at", _now()),
                         record.get("updated_at", _now()), json.dumps({"original": record.get("provenance", ""), "import": source_ref}),
                         record.get("importance", .5), record.get("trusted", 1), record.get("fingerprint", "")),
                    )
                else:
                    self.conn.execute(
                        "INSERT INTO episodes(happened_at,summary,created_at,updated_at,source,provenance,importance,trusted,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
                        (record["happened_at"], record["summary"], record.get("created_at", _now()), record.get("updated_at", _now()),
                         "capsule_import", json.dumps({"original": record.get("provenance", ""), "import": source_ref}),
                         record.get("importance", .5), record.get("trusted", 1), record.get("fingerprint", "")),
                    )
        relations = _json(files.get("memory/graph_relations.json", b"[]"), "memory/graph_relations.json")
        relation_map: dict[int, int] = {}
        pending_supersession: list[tuple[int, int]] = []
        for record in relations:
            subject = entities.get(int(record["subject_id"]))
            obj = entities.get(int(record["object_id"]))
            if subject is None or obj is None:
                raise CapsuleError("graph relation references a missing entity")
            existing = self.conn.execute(
                "SELECT id FROM graph_relations WHERE subject_id=? AND predicate=? AND object_id=? AND status=?",
                (subject, record["predicate"], obj, record.get("status", "active")),
            ).fetchone()
            if existing:
                relation_map[int(record["id"])] = int(existing[0])
                continue
            wrapped_ref = json.dumps({"original_source_type": record.get("source_type", ""),
                                      "original_source_ref": record.get("source_ref", ""),
                                      "import": source_ref}, sort_keys=True)
            cursor = self.conn.execute(
                "INSERT INTO graph_relations(subject_id,predicate,object_id,confidence,importance,source_type,source_ref,valid_from,valid_to,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (subject, record["predicate"], obj, record.get("confidence", .8), record.get("importance", .5),
                 "capsule_import", wrapped_ref, record.get("valid_from", ""), record.get("valid_to", ""),
                 record.get("status", "active"), record.get("created_at", _now()), record.get("updated_at", _now())),
            )
            relation_map[int(record["id"])] = int(cursor.lastrowid)
            if record.get("superseded_by") is not None:
                pending_supersession.append((int(cursor.lastrowid), int(record["superseded_by"])))
        for new_id, old_target in pending_supersession:
            if old_target in relation_map:
                self.conn.execute("UPDATE graph_relations SET superseded_by=? WHERE id=?", (relation_map[old_target], new_id))
        for scope in ("replay", "shadow"):
            for table in DB_EXPORTS[scope]:
                for record in _json(files.get(f"{scope}/{table}.json", b"[]"), f"{scope}/{table}.json"):
                    columns = [str(k) for k in record if str(k) != "rowid"]
                    placeholders = ",".join("?" for _ in columns)
                    self.conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                        tuple(record[column] for column in columns),
                    )

    def _audit(self, operation: str, capsule_id: str, scopes: tuple[str, ...], target: str,
               warnings: list[str]) -> None:
        self.conn.execute(
            "INSERT INTO capsule_audits VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?)",
            (f"{operation}_{uuid.uuid4().hex}", operation, capsule_id, _now(),
             json.dumps(scopes), Path(target).name, json.dumps(warnings)),
        )

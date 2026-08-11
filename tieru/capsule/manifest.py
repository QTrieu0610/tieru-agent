"""Capsule v1 manifest and integrity verification."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from tieru.capsule.models import CapsuleInspection, CapsuleLimits
from tieru.capsule.security import CapsuleError, safe_member_path, validate_archive

FORMAT = "tieru-capsule"
FORMAT_VERSION = 1
SCHEMA_VERSION = 1
_CAPSULE_ID = re.compile(r"^capsule_[a-f0-9]{32}$")


def _member_scope(name: str) -> str | None:
    exact = {
        "identity/SOUL.md": "identity", "identity/IDENTITY.md": "identity",
        "identity/PREFERENCES.md": "identity", "memory/facts.json": "memory",
        "memory/episodes.json": "memory", "memory/graph_entities.json": "memory",
        "memory/graph_relations.json": "memory", "memory/MEMORY.md": "memory",
        "skills/index.json": "skills", "config/preferences.json": "preferences",
        "config/fabric.json": "fabric", "trust/policy.json": "trust",
        "replay/replay_runs.json": "replay", "replay/replay_events.json": "replay",
        "shadow/shadow_patterns.json": "shadow", "shadow/shadow_suggestions.json": "shadow",
        "shadow/shadow_observations.json": "shadow", "shadow/shadow_settings.json": "shadow",
    }
    if name in exact:
        return exact[name]
    if re.fullmatch(r"skills/[a-z0-9]+(?:-[a-z0-9]+)*/SKILL\.md", name):
        return "skills"
    if re.fullmatch(
        r"forge/drafts/draft_[a-z0-9]{8,64}/(?:SKILL\.md|metadata\.json|workflow\.json|evaluation\.json)",
        name,
    ):
        return "forge"
    return None


def encode_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_verified(path: Path, limits: CapsuleLimits | None = None) -> tuple[dict, dict[str, bytes]]:
    limits = limits or CapsuleLimits()
    infos = validate_archive(path, limits)
    if not any(item.filename == "manifest.json" for item in infos):
        raise CapsuleError("Capsule has no manifest.json")
    with zipfile.ZipFile(path) as archive:
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapsuleError(f"invalid manifest.json: {exc}") from exc
        if not isinstance(manifest, dict):
            raise CapsuleError("manifest.json must contain an object")
        if manifest.get("format") != FORMAT:
            raise CapsuleError("unsupported Capsule format")
        version = manifest.get("format_version")
        if version != FORMAT_VERSION:
            raise CapsuleError(f"unsupported Capsule format version {version!r}; supported: 1")
        required = {
            "capsule_id", "created_at", "tieru_version", "schema_version",
            "export_profile", "included_scopes", "item_counts", "source_platform",
            "source_python_version", "encrypted", "compression", "integrity_algorithm",
            "files", "compatibility",
        }
        missing = sorted(required - manifest.keys())
        if missing:
            raise CapsuleError("manifest missing fields: " + ", ".join(missing))
        if manifest["encrypted"] is not False:
            raise CapsuleError("encrypted Capsules are not supported in format v1")
        if manifest["integrity_algorithm"] != "sha256":
            raise CapsuleError("unsupported integrity algorithm")
        if not _CAPSULE_ID.fullmatch(str(manifest["capsule_id"])):
            raise CapsuleError("invalid Capsule ID")
        scopes = manifest.get("included_scopes")
        known_scopes = {"identity", "memory", "skills", "preferences", "trust", "fabric", "replay", "forge", "shadow"}
        if not isinstance(scopes, list) or len(scopes) != len(set(scopes)) or set(scopes) - known_scopes:
            raise CapsuleError("manifest contains invalid or duplicate scopes")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise CapsuleError("manifest files must be a list")
        declared: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise CapsuleError("invalid manifest file entry")
            name = safe_member_path(str(entry.get("path", "")))
            if name == "manifest.json" or name.casefold() in {p.casefold() for p in declared}:
                raise CapsuleError(f"duplicate manifest path: {name}")
            digest = entry.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                raise CapsuleError(f"invalid SHA-256 for {name}")
            declared[name] = entry
            member_scope = _member_scope(name)
            if member_scope is None or member_scope not in scopes:
                raise CapsuleError(f"member is not allowed by Capsule layout/scopes: {name}")
        actual = {item.filename for item in infos if item.filename != "manifest.json"}
        if actual != set(declared):
            missing_files = sorted(set(declared) - actual)
            extra_files = sorted(actual - set(declared))
            raise CapsuleError(f"manifest/archive mismatch; missing={missing_files}, extra={extra_files}")
        payloads: dict[str, bytes] = {}
        for name, entry in declared.items():
            payload = archive.read(name)
            if len(payload) != entry.get("size") or sha256(payload) != entry["sha256"]:
                raise CapsuleError(f"integrity verification failed: {name}")
            payloads[name] = payload
    return manifest, payloads


def inspect_capsule(path: Path, limits: CapsuleLimits | None = None) -> CapsuleInspection:
    manifest, _ = read_verified(path, limits)
    warnings = ["SHA-256 detects corruption but does not authenticate the archive origin."]
    return CapsuleInspection(manifest, True, warnings)

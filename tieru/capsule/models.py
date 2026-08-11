"""Public, serializable Capsule models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CapsuleLimits:
    max_archive_bytes: int = 100 * 1024 * 1024
    max_files: int = 1_000
    max_file_bytes: int = 20 * 1024 * 1024
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_compression_ratio: float = 100.0


@dataclass
class ImportPlan:
    capsule_id: str = ""
    format_version: int = 0
    valid: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    creates: dict[str, int] = field(default_factory=dict)
    updates: dict[str, int] = field(default_factory=dict)
    skips: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    path_remaps: list[dict[str, str]] = field(default_factory=list)
    trust_review_required: bool = False
    selected_scopes: list[str] = field(default_factory=list)
    integrity_verified: bool = False
    archive_sha256: str = ""
    secrets_detected: bool = False
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapsuleInspection:
    manifest: dict[str, Any]
    integrity_valid: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "integrity_valid": self.integrity_valid,
            "warnings": self.warnings,
        }

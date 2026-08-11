"""Untrusted archive validation and structural secret defense."""

from __future__ import annotations

import re
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from tieru.capsule.models import CapsuleLimits


class CapsuleError(ValueError):
    """A capsule is malformed, unsafe, incompatible, or corrupt."""


_SECRET_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|password|"
    r"secret|private[_-]?key|authorization|cookie|credentials?)($|[_-])", re.IGNORECASE
)
_SECRET_VALUE = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"(?:authorization|proxy-authorization)\s*:\s*(?:bearer\s+)?[^\s\"']+|"
    rb"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]\s*[^\s\"']+)",
    re.IGNORECASE,
)


def safe_member_path(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise CapsuleError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).is_absolute():
        raise CapsuleError(f"absolute archive path: {name!r}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise CapsuleError(f"non-normalized archive path: {name!r}")
    normalized = path.as_posix()
    if normalized != name:
        raise CapsuleError(f"non-normalized archive path: {name!r}")
    return normalized


def validate_archive(path: Path, limits: CapsuleLimits) -> list[zipfile.ZipInfo]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CapsuleError(f"cannot read Capsule: {exc}") from exc
    if size > limits.max_archive_bytes:
        raise CapsuleError("Capsule exceeds maximum archive size")
    if not zipfile.is_zipfile(path):
        raise CapsuleError("Capsule is not a valid ZIP-compatible container")
    try:
        archive = zipfile.ZipFile(path)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise CapsuleError(f"invalid Capsule archive: {exc}") from exc
    finally:
        if "archive" in locals():
            archive.close()
    if len(infos) > limits.max_files:
        raise CapsuleError("Capsule exceeds maximum file count")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = safe_member_path(info.filename)
        folded = name.casefold()
        if folded in seen:
            raise CapsuleError(f"duplicate archive path: {name}")
        seen.add(folded)
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise CapsuleError(f"symlink archive member rejected: {name}")
        if info.is_dir():
            raise CapsuleError(f"directory entries are not allowed: {name}")
        if info.file_size > limits.max_file_bytes:
            raise CapsuleError(f"archive member exceeds size limit: {name}")
        total += info.file_size
        if total > limits.max_uncompressed_bytes:
            raise CapsuleError("Capsule exceeds total uncompressed size limit")
        if info.file_size and info.compress_size == 0:
            raise CapsuleError(f"suspicious compression metadata: {name}")
        if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
            raise CapsuleError(f"suspicious compression ratio: {name}")
    return infos


def reject_secret_keys(value: object, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key).replace(" ", "_")):
                raise CapsuleError(f"secret-bearing field rejected at {path}.{key}")
            reject_secret_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_keys(child, f"{path}[{index}]")


def scan_payloads(files: dict[str, bytes]) -> None:
    for name, payload in files.items():
        if _SECRET_VALUE.search(payload):
            raise CapsuleError(f"possible credential material detected in {name}")

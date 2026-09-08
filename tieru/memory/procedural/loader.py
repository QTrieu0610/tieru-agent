"""Procedural SKILL.md parsing, review classification, and live retrieval."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from tieru.memory.procedural.retrieval import (
    EmbeddingBackend,
    SkillEmbeddingCache,
    SkillMatch,
    SkillRetrievalConfig,
    SkillRetriever,
    normalize_text,
)

MAX_FRONTMATTER_BYTES = 8192
MAX_NAME_BYTES = 128
MAX_DESCRIPTION_BYTES = 1024
MAX_METADATA_ITEMS = 16
MAX_METADATA_ITEM_BYTES = 128


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    reviewed: bool = False
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    @property
    def skill_id(self) -> str:
        identity = f"{normalize_text(self.name)}\n{self.path.resolve()}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _bounded(value: str, limit: int) -> str:
    encoded = value.strip().encode("utf-8")
    if len(encoded) <= limit:
        return value.strip()
    return encoded[:limit].decode("utf-8", errors="ignore").strip()


def _string_list(value: object) -> tuple[str, ...]:
    """Accept only bounded YAML lists; malformed optional fields are ignored."""
    if not isinstance(value, list):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for item in value[:MAX_METADATA_ITEMS]:
        if not isinstance(item, str):
            continue
        bounded = _bounded(item, MAX_METADATA_ITEM_BYTES)
        normalized = normalize_text(bounded)
        if bounded and normalized and normalized not in seen:
            seen.add(normalized)
            output.append(bounded)
    return tuple(output)


def _parse_text(text: str, path: Path) -> Skill | None:
    """Validate old and M20 SKILL.md frontmatter without loading unsafe YAML types."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", normalized, re.DOTALL)
    if not match:
        return None
    front, body = match.groups()
    if len(front.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        return None
    try:
        fields = yaml.safe_load(front)
    except yaml.YAMLError:
        return None
    if not isinstance(fields, dict):
        return None
    name = fields.get("name")
    description = fields.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    name = _bounded(name, MAX_NAME_BYTES)
    description = _bounded(description, MAX_DESCRIPTION_BYTES)
    if not name or not description:
        return None
    return Skill(
        name,
        description,
        body.strip(),
        path,
        aliases=_string_list(fields.get("aliases")),
        keywords=_string_list(fields.get("keywords")),
        domains=_string_list(fields.get("domains")),
    )


def _parse(path: Path) -> Skill | None:
    try:
        return _parse_text(path.read_text(encoding="utf-8"), path)
    except (OSError, UnicodeError):
        return None


class SkillLoader:
    """Live loader with deterministic hybrid retrieval and compatibility APIs."""

    def __init__(
        self,
        dirs: list[Path],
        *,
        reviewed_dirs: list[Path] | None = None,
        retrieval_config: SkillRetrievalConfig | None = None,
        embedding_backend: EmbeddingBackend | None = None,
        embedding_cache: SkillEmbeddingCache | None = None,
    ):
        self.dirs = dirs
        self.reviewed_dirs = [path.resolve() for path in (reviewed_dirs or [])]
        self.skills: list[Skill] = []
        self._sig: tuple = ()
        self.retriever = SkillRetriever(
            lambda: tuple(self.skills),
            config=retrieval_config,
            embedding_backend=embedding_backend,
            cache=embedding_cache,
        )
        self.refresh()

    def _scan_sig(self) -> tuple:
        signature = []
        for directory in self.dirs:
            if directory.is_dir():
                for path in sorted(directory.rglob("SKILL.md")):
                    stat = path.stat()
                    signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def refresh(self) -> None:
        self.skills = []
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("SKILL.md")):
                skill = _parse(path)
                if skill:
                    resolved = path.resolve()
                    skill.reviewed = any(
                        root == resolved.parent or root in resolved.parents
                        for root in self.reviewed_dirs
                    ) or self._forge_reviewed(path)
                    self.skills.append(skill)
        self._sig = self._scan_sig()

    @staticmethod
    def _forge_reviewed(path: Path) -> bool:
        metadata = path.parent / "forge-metadata.json"
        if not metadata.is_file():
            return False
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(
            value.get("status") == "installed"
            and value.get("approved_at")
            and value.get("installed_at")
        )

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[SkillMatch]:
        if self._scan_sig() != self._sig:
            self.refresh()
        return self.retriever.retrieve(query, top_k=top_k)

    def match(self, message: str, max_skills: int = 2) -> list[Skill]:
        """Legacy list[Skill] API delegates to the explained retriever."""
        return [match.skill for match in self.retrieve(message, top_k=max_skills)]

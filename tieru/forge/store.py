"""Bounded local file persistence for inactive Forge drafts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tieru.forge.models import SkillDraft, WorkflowCandidate

_ID = re.compile(r"^draft_[a-z0-9]{8,64}$")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ForgeStore:
    def __init__(self, home: Path):
        self.root = home / "forge" / "drafts"

    def path(self, draft_id: str) -> Path:
        if not _ID.fullmatch(str(draft_id)):
            raise ValueError("invalid draft ID")
        target = (self.root / draft_id).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("draft path escapes Forge storage")
        return target

    def save(self, draft: SkillDraft) -> SkillDraft:
        target = self.path(draft.draft_id)
        target.mkdir(parents=True, exist_ok=True)
        draft.metadata["content_hash"] = content_hash(draft.content)
        self._write(target / "SKILL.md", draft.content.rstrip() + "\n")
        self._json(target / "metadata.json", draft.metadata)
        self._json(target / "workflow.json", draft.workflow.to_dict())
        self._json(target / "evaluation.json", {
            "validation": draft.validation, "evaluation": draft.evaluation,
        })
        return draft

    def load(self, draft_id: str) -> SkillDraft:
        target = self.path(draft_id)
        if not target.is_dir():
            raise KeyError(draft_id)
        metadata = self._read_json(target / "metadata.json")
        workflow = WorkflowCandidate.from_dict(self._read_json(target / "workflow.json"))
        results = self._read_json(target / "evaluation.json", missing={})
        return SkillDraft(
            draft_id, metadata, workflow, (target / "SKILL.md").read_text(encoding="utf-8"),
            results.get("validation") or {}, results.get("evaluation") or {},
        )

    def list(self) -> list[SkillDraft]:
        if not self.root.is_dir():
            return []
        drafts = []
        for path in sorted(self.root.glob("draft_*"), reverse=True):
            try:
                drafts.append(self.load(path.name))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return drafts

    @staticmethod
    def _write(path: Path, text: str) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)

    def _json(self, path: Path, value: Any) -> None:
        self._write(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _read_json(path: Path, missing: Any = None) -> Any:
        if missing is not None and not path.exists():
            return missing
        return json.loads(path.read_text(encoding="utf-8"))

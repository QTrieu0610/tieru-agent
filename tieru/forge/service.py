"""User-triggered orchestration for Skill Forge."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from tieru.forge.evaluator import DraftEvaluator
from tieru.forge.extractor import WorkflowExtractor
from tieru.forge.generator import SkillGenerator, slugify
from tieru.forge.models import SkillDraft
from tieru.forge.store import ForgeStore, content_hash
from tieru.forge.validator import DraftValidator
from tieru.memory import bundled_skill_dirs
from tieru.memory.procedural.loader import SkillLoader
from tieru.trust import ActionRequest, Capability, TrustKernel


class ForgeLifecycleError(RuntimeError):
    pass


class ForgeTrustDenied(PermissionError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ForgeService:
    def __init__(
        self, conn, settings, *, model_client: Any | None = None,
        model: str = "", provider: str = "", approval_handler: Callable | None = None,
        available_tools: set[str] | None = None, judge: Callable | None = None,
        observer: Callable[[str, dict], None] | None = None,
    ):
        from tieru.replay import ReplayService

        self.settings = settings
        settings.ensure_home()
        self.replay = ReplayService(conn, settings)
        self.store = ForgeStore(settings.home)
        self.extractor = WorkflowExtractor(self.replay)
        self.generator = SkillGenerator(model_client, model=model, provider=provider)
        self.validator = DraftValidator(self.replay, available_tools=available_tools)
        self.evaluator = DraftEvaluator(judge)
        self.trust = TrustKernel(
            settings.trust_policy, settings.tool_permissions, approval_handler,
            context={"source": "skill_forge"},
        )
        self.observer = observer

    def forge(self, run_ids: list[str]) -> SkillDraft:
        self._emit("forge_started", {"source_run_ids": run_ids})
        candidate = self.extractor.extract(run_ids)
        self._emit("workflow_extracted", {
            "candidate_id": candidate.candidate_id,
            "workflow_signature": candidate.workflow_signature,
        })
        draft_id = f"draft_{uuid4().hex[:16]}"
        target = self.store.path(draft_id)
        self._authorize("forge_write_draft", "create_forge_draft", target)
        content, generation, warnings = self.generator.generate(candidate)
        from tieru.memory.procedural.loader import _parse_text

        parsed = _parse_text(content, Path("SKILL.md"))
        skill_id = parsed.name if parsed and slugify(parsed.name) == parsed.name else slugify(candidate.title)
        metadata = {
            "draft_id": draft_id, "skill_id": skill_id, "name": candidate.title,
            "description": f"Reusable procedure derived from {len(run_ids)} reviewed Replay run(s).",
            "status": "draft", "version": 1, "parent_version": None,
            "created_at": _now(), "updated_at": _now(), "content_hash": content_hash(content),
            "workflow_signature": candidate.workflow_signature,
            "required_tools": candidate.tools, "optional_tools": [],
            "required_capabilities": candidate.capabilities,
            "trust_requirements": candidate.trust_requirements,
            "generation": generation, "edit_state": "generated", "warnings": warnings,
            "provenance": {
                "source_run_ids": candidate.source_run_ids,
                "event_refs": candidate.evidence,
                "workflow_candidate_id": candidate.candidate_id,
            },
        }
        draft = SkillDraft(draft_id, metadata, candidate, content)
        self.store.save(draft)
        self._emit("draft_created", {"draft_id": draft_id, "source_run_ids": run_ids})
        return draft

    def get(self, draft_id: str) -> SkillDraft:
        return self.store.load(draft_id)

    def list(self) -> list[dict]:
        return [self._summary(draft) for draft in self.store.list()]

    def validate(self, draft_id: str) -> SkillDraft:
        draft = self.get(draft_id)
        self._require_mutable(draft)
        self._authorize("forge_write_draft", "record_forge_validation", self.store.path(draft_id))
        current_hash = content_hash(draft.content)
        if current_hash != draft.metadata.get("content_hash"):
            draft.metadata.update({"edit_state": "modified", "content_hash": current_hash})
            draft.evaluation = {}
        draft.validation = self.validator.validate(draft)
        draft.validation["content_hash"] = current_hash
        draft.metadata["status"] = "validated" if draft.validation["passed"] else "draft"
        draft.metadata["updated_at"] = _now()
        self.store.save(draft)
        self._emit("validation_completed", {"draft_id": draft_id, "passed": draft.validation["passed"]})
        return draft

    def evaluate(self, draft_id: str) -> SkillDraft:
        draft = self.get(draft_id)
        self._require_mutable(draft)
        if not draft.validation.get("passed") or draft.validation.get("content_hash") != content_hash(draft.content):
            raise ForgeLifecycleError("draft must pass current deterministic validation before evaluation")
        self._authorize("forge_write_draft", "record_forge_evaluation", self.store.path(draft_id))
        draft.evaluation = self.evaluator.evaluate(draft)
        draft.evaluation["content_hash"] = content_hash(draft.content)
        draft.metadata["status"] = (
            "ready_for_review" if draft.evaluation["deterministic_pass"] else "evaluation_failed"
        )
        draft.metadata["updated_at"] = _now()
        self.store.save(draft)
        self._emit("evaluation_completed", {
            "draft_id": draft_id, "passed": draft.evaluation["deterministic_pass"]
        })
        return draft

    def edit(self, draft_id: str, content: str) -> SkillDraft:
        draft = self.get(draft_id)
        self._require_mutable(draft)
        self._authorize("forge_write_draft", "edit_forge_draft", self.store.path(draft_id))
        draft.content = content.rstrip() + "\n"
        draft.metadata.update({
            "status": "draft", "edit_state": "modified", "content_hash": content_hash(draft.content),
            "updated_at": _now(),
        })
        draft.validation = {}
        draft.evaluation = {}
        return self.store.save(draft)

    def reject(self, draft_id: str) -> SkillDraft:
        draft = self.get(draft_id)
        self._authorize("forge_write_draft", "reject_forge_draft", self.store.path(draft_id))
        if draft.metadata.get("status") == "installed":
            raise ForgeLifecycleError("an installed draft cannot be rejected")
        draft.metadata.update({"status": "rejected", "updated_at": _now()})
        self.store.save(draft)
        self._emit("forge_rejected", {"draft_id": draft_id})
        return draft

    def install(self, draft_id: str, *, approved: bool = False) -> SkillDraft:
        if not approved:
            raise ForgeLifecycleError("explicit user approval is required before installation")
        draft = self.get(draft_id)
        if draft.metadata.get("status") != "ready_for_review":
            raise ForgeLifecycleError("draft must be validated and evaluated before installation")
        digest = content_hash(draft.content)
        if draft.validation.get("content_hash") != digest or draft.evaluation.get("content_hash") != digest:
            raise ForgeLifecycleError("draft changed; revalidation and reevaluation are required")
        skill_id = str(draft.metadata.get("skill_id") or "")
        destination = self.settings.home / "skills" / skill_id
        duplicates = self.duplicates(draft)
        if destination.exists() or any(item["same_name"] for item in duplicates):
            raise FileExistsError(f"skill collision: '{skill_id}' already exists; no files were overwritten")
        self._emit("install_requested", {"draft_id": draft_id, "skill_id": skill_id})
        self._authorize("forge_install_skill", "install_user_skill", destination)
        skills_root = self.settings.home / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        incoming = skills_root / f".forge-incoming-{draft_id}"
        if incoming.exists():
            raise FileExistsError("a previous Forge installation staging path still exists")
        incoming.mkdir()
        installed_metadata = {
            **draft.metadata,
            "status": "installed", "approved_at": _now(), "installed_at": _now(),
            "installed_path": str(destination), "updated_at": _now(),
        }
        try:
            (incoming / "SKILL.md").write_text(draft.content, encoding="utf-8")
            (incoming / "forge-metadata.json").write_text(
                json.dumps(installed_metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            incoming.rename(destination)
        except Exception:
            if incoming.is_dir():
                shutil.rmtree(incoming)
            raise
        draft.metadata = installed_metadata
        self.store.save(draft)
        self._emit("skill_installed", {"draft_id": draft_id, "skill_id": skill_id})
        return draft

    def duplicates(self, draft: SkillDraft) -> list[dict]:
        loader = SkillLoader([*bundled_skill_dirs(), self.settings.home / "skills"])
        output = []
        for skill in loader.skills:
            meta_path = skill.path.parent / "forge-metadata.json"
            signature = ""
            if meta_path.exists():
                try:
                    import json
                    signature = json.loads(meta_path.read_text(encoding="utf-8")).get("workflow_signature", "")
                except (OSError, ValueError):
                    pass
            same_name = skill.name.lower() == str(draft.metadata.get("skill_id", "")).lower()
            same_signature = bool(signature and signature == draft.workflow.workflow_signature)
            same_tools = all(f"`{tool}`" in skill.body for tool in draft.workflow.tools)
            if same_name or same_signature or same_tools:
                output.append({
                    "name": skill.name, "path": str(skill.path), "same_name": same_name,
                    "same_signature": same_signature, "same_tool_sequence": same_tools,
                })
        return output

    def _authorize(self, tool: str, operation: str, target: Path) -> None:
        action = ActionRequest(
            tool_name=tool, capabilities=(Capability.LOCAL_WRITE,), operation=operation,
            target=str(target.resolve()), scope=str(self.settings.home.resolve()),
            resource_type="forge_draft" if tool == "forge_write_draft" else "procedural_skill",
            local=True, reversible=True, read_only=False,
        )
        decision = self.trust.authorize(
            action, default_policy="confirm", legacy_capabilities=("filesystem.write",),
            approval_args={"target": str(target), "operation": operation}, observer=self.observer,
        )
        if not decision.allowed:
            raise ForgeTrustDenied(decision.explanation)

    def _emit(self, kind: str, payload: dict) -> None:
        if self.observer:
            self.observer(kind, payload)

    @staticmethod
    def _require_mutable(draft: SkillDraft) -> None:
        if draft.metadata.get("status") in {"installed", "rejected", "archived"}:
            raise ForgeLifecycleError(
                f"draft in state {draft.metadata.get('status')} cannot be modified"
            )

    @staticmethod
    def _summary(draft: SkillDraft) -> dict:
        return {
            "draft_id": draft.draft_id, "skill_id": draft.metadata.get("skill_id"),
            "name": draft.metadata.get("name"), "status": draft.metadata.get("status"),
            "version": draft.metadata.get("version"),
            "source_run_ids": draft.metadata.get("provenance", {}).get("source_run_ids", []),
            "workflow_signature": draft.metadata.get("workflow_signature"),
            "warnings": draft.metadata.get("warnings", []),
        }

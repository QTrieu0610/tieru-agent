"""Passive, deterministic Replay pattern aggregation and Forge suggestions."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from tieru.forge.extractor import ForgeSourceError, WorkflowExtractor
from tieru.forge.generator import slugify
from tieru.forge.store import ForgeStore
from tieru.memory import bundled_skill_dirs
from tieru.memory.procedural.loader import SkillLoader
from tieru.replay import ReplayService
from tieru.shadow.models import ShadowPattern, ShadowSuggestion
from tieru.shadow.store import ShadowStore, now_utc

Observer = Callable[[str, dict[str, Any]], None]


class ShadowLifecycleError(RuntimeError):
    pass


class ShadowService:
    """Advisory metadata only: no tools, model calls, policy writes, or skills."""

    def __init__(self, conn, settings, *, observer: Observer | None = None):
        self.conn = conn
        self.settings = settings
        self.replay = ReplayService(conn, settings)
        self.extractor = WorkflowExtractor(self.replay)
        self.store = ShadowStore(conn)
        self.observer = observer

    @property
    def enabled(self) -> bool:
        override = self.store.enabled_override()
        return self.settings.shadow_enabled if override is None else override

    def set_enabled(self, enabled: bool) -> dict:
        self.store.set_enabled(enabled)
        self._emit("shadow_enabled" if enabled else "shadow_disabled", {"enabled": enabled})
        return self.status()

    def observe(self, run_id: str) -> dict:
        """Analyze an already-persisted run; never execute or replay it."""
        if not self.enabled:
            return {"status": "disabled", "run_id": run_id}
        self.store.mark_stale(self.settings.shadow_stale_days)
        previous = self.store.observation(run_id)
        if previous:
            return {"status": "already_observed", **previous}
        try:
            candidate = self.extractor.extract([run_id])
        except (ForgeSourceError, KeyError, ValueError) as exc:
            self.store.record_ignored(run_id, f"ineligible:{type(exc).__name__}")
            return {"status": "ineligible", "run_id": run_id, "reason": type(exc).__name__}
        # A completed conversation can contain a recovered tool failure or denial.
        # Shadow never promotes such a workflow as the normal successful path.
        if any(step.status != "completed" for step in candidate.steps):
            self.store.record_ignored(run_id, "non_suggestible_tool_outcome")
            return {"status": "ineligible", "run_id": run_id,
                    "reason": "non_suggestible_tool_outcome"}

        pattern, added = self.store.aggregate(
            candidate, run_id, max_evidence=self.settings.shadow_max_evidence_runs
        )
        if not added:
            return {"status": "already_observed", "pattern": pattern.public()}
        confidence = self._confidence(pattern)
        pattern = self.store.update_pattern(pattern.pattern_id, confidence=confidence)
        self._emit("shadow_run_observed", {
            "run_id": run_id, "pattern_id": pattern.pattern_id,
            "workflow_signature": pattern.workflow_signature,
        })
        self._emit("shadow_pattern_updated", {
            "pattern_id": pattern.pattern_id, "occurrence_count": pattern.occurrence_count,
            "confidence": pattern.confidence,
        })
        result = self._maybe_suggest(pattern, candidate)
        return {"status": result, "pattern": self.store.get_pattern(pattern.pattern_id).public()}

    def _maybe_suggest(self, pattern: ShadowPattern, candidate) -> str:
        installed = self._installed_coverage(candidate)
        if installed:
            self.store.update_pattern(
                pattern.pattern_id, status="covered",
                metadata_json=json.dumps({"covered_by_skill": installed}),
            )
            current = self.store.suggestion_for_pattern(pattern.pattern_id)
            if current:
                self.store.update_suggestion(current.suggestion_id, status="covered")
            return "covered"
        if pattern.status == "covered":
            pattern = self.store.update_pattern(pattern.pattern_id, status="observing")

        draft_id = self._existing_draft(pattern.workflow_signature)
        if draft_id:
            self.store.update_pattern(
                pattern.pattern_id, status="forged", forge_draft_id=draft_id,
                metadata_json=json.dumps({"existing_draft": draft_id}),
            )
            return "existing_draft"
        if pattern.status == "forged":
            # A prior handoff whose draft was deleted/rejected is not promotion evidence.
            self.store.update_pattern(
                pattern.pattern_id, status="ignored",
                suppress_until_count=pattern.occurrence_count + self.settings.shadow_min_occurrences,
                forge_draft_id="",
            )
            return "suppressed"
        if pattern.status == "dismissed":
            return "dismissed"
        if pattern.status == "snoozed" and self._future(pattern.snoozed_until):
            return "snoozed"
        if (pattern.status == "ignored"
                and pattern.occurrence_count < pattern.suppress_until_count):
            return "ignored"
        if pattern.occurrence_count < self.settings.shadow_min_occurrences:
            return "observing"
        if not self.settings.shadow_suggestions:
            return "observing"

        existing_suggestion = self.store.suggestion_for_pattern(pattern.pattern_id)
        already_ready = (
            pattern.status == "suggestion_ready"
            and existing_suggestion is not None
            and existing_suggestion.status == "ready"
        )
        pattern = self.store.update_pattern(
            pattern.pattern_id, status="suggestion_ready", snoozed_until=""
        )
        suggestion = self.store.upsert_suggestion(
            pattern,
            name=slugify(candidate.title),
            summary=self._summary(pattern),
            explanation=self._explanation(pattern),
        )
        if not already_ready:
            self._emit("shadow_threshold_reached", {
                "pattern_id": pattern.pattern_id,
                "occurrence_count": pattern.occurrence_count,
            })
            self._emit("shadow_suggestion_created", {
                "suggestion_id": suggestion.suggestion_id,
                "pattern_id": pattern.pattern_id,
            })
        return "suggestion_ready"

    def ignore(self, suggestion_id: str) -> ShadowSuggestion:
        suggestion = self.store.get_suggestion(suggestion_id)
        pattern = self.store.get_pattern(suggestion.pattern_id)
        self.store.update_pattern(
            pattern.pattern_id, status="ignored",
            suppress_until_count=pattern.occurrence_count + self.settings.shadow_min_occurrences,
            snoozed_until="",
        )
        result = self.store.update_suggestion(suggestion_id, status="ignored", snoozed_until="")
        self._emit("shadow_suggestion_ignored", {"suggestion_id": suggestion_id})
        return result

    def snooze(self, suggestion_id: str, *, days: int | None = None) -> ShadowSuggestion:
        suggestion = self.store.get_suggestion(suggestion_id)
        until = (datetime.now(UTC) + timedelta(
            days=max(1, days or self.settings.shadow_snooze_days)
        )).isoformat(timespec="seconds")
        self.store.update_pattern(
            suggestion.pattern_id, status="snoozed", snoozed_until=until
        )
        result = self.store.update_suggestion(
            suggestion_id, status="snoozed", snoozed_until=until
        )
        self._emit("shadow_suggestion_snoozed", {
            "suggestion_id": suggestion_id, "snoozed_until": until,
        })
        return result

    def dismiss(self, suggestion_id: str) -> ShadowSuggestion:
        suggestion = self.store.get_suggestion(suggestion_id)
        self.store.update_pattern(
            suggestion.pattern_id, status="dismissed", snoozed_until=""
        )
        result = self.store.update_suggestion(
            suggestion_id, status="dismissed", snoozed_until=""
        )
        self._emit("shadow_suggestion_dismissed", {"suggestion_id": suggestion_id})
        return result

    def forge(self, suggestion_id: str, forge_service):
        """Explicit handoff to M9. The returned object is an inactive draft."""
        suggestion = self.store.get_suggestion(suggestion_id)
        if suggestion.status != "ready":
            raise ShadowLifecycleError("only a ready suggestion can be handed to Forge")
        run_ids = suggestion.representative_run_ids[-3:]
        self._emit("shadow_forge_requested", {
            "suggestion_id": suggestion_id, "source_run_ids": run_ids,
        })
        draft = forge_service.forge(run_ids)
        self.store.update_pattern(
            suggestion.pattern_id, status="forged", forge_draft_id=draft.draft_id
        )
        self.store.update_suggestion(
            suggestion_id, status="forged", forge_draft_id=draft.draft_id
        )
        return draft

    def inspect(self, suggestion_id: str) -> dict:
        suggestion = self.store.get_suggestion(suggestion_id)
        pattern = self.store.get_pattern(suggestion.pattern_id)
        return {"suggestion": suggestion.public(), "pattern": pattern.public()}

    def patterns(self) -> list[dict]:
        return [
            item.public()
            for item in self.store.list_patterns()
        ]

    def suggestions(self, *, include_inactive: bool = False) -> list[dict]:
        return [item.public() for item in self.store.list_suggestions(
            include_inactive=include_inactive
        )]

    def status(self) -> dict:
        patterns = self.patterns()
        suggestions = self.suggestions()
        return {
            "enabled": self.enabled,
            "configured_default": self.settings.shadow_enabled,
            "min_occurrences": self.settings.shadow_min_occurrences,
            "max_evidence_runs": self.settings.shadow_max_evidence_runs,
            "suggestions_enabled": self.settings.shadow_suggestions,
            "pattern_count": len(patterns),
            "suggestion_count": sum(item["status"] == "ready" for item in suggestions),
            "covered_count": sum(item["status"] in {"covered", "forged"} for item in patterns),
        }

    def snapshot(self) -> dict:
        return {
            "status": self.status(), "patterns": self.patterns(),
            "suggestions": self.suggestions(),
        }

    def _installed_coverage(self, candidate) -> str:
        loader = SkillLoader([*bundled_skill_dirs(), self.settings.home / "skills"])
        wanted_name = slugify(candidate.title)
        for skill in loader.skills:
            meta_path = skill.path.parent / "forge-metadata.json"
            signature = ""
            if meta_path.exists():
                try:
                    signature = json.loads(
                        meta_path.read_text(encoding="utf-8")
                    ).get("workflow_signature", "")
                except (OSError, ValueError):
                    pass
            positions = [skill.body.find(f"`{tool}`") for tool in candidate.tools]
            same_tools = bool(positions) and all(pos >= 0 for pos in positions) and positions == sorted(positions)
            if (signature == candidate.workflow_signature
                    or slugify(skill.name) == wanted_name or same_tools):
                return skill.name
        return ""

    def _existing_draft(self, signature: str) -> str:
        for draft in ForgeStore(self.settings.home).list():
            status = draft.metadata.get("status")
            if (draft.metadata.get("workflow_signature") == signature
                    and status not in {"rejected", "archived", "installed"}):
                return draft.draft_id
        return ""

    def _confidence(self, pattern: ShadowPattern) -> str:
        if (pattern.occurrence_count >= max(5, self.settings.shadow_min_occurrences + 2)
                and pattern.successful_count == pattern.occurrence_count
                and pattern.verification_count == pattern.occurrence_count):
            return "high"
        if (pattern.occurrence_count >= self.settings.shadow_min_occurrences
                and pattern.successful_count == pattern.occurrence_count):
            return "medium"
        return "low"

    @staticmethod
    def _summary(pattern: ShadowPattern) -> str:
        tools = " then ".join(tool.replace("_", " ") for tool in pattern.representative_tools)
        return f"Repeated workflow detected: {tools or 'observable tool sequence'}."

    @staticmethod
    def _explanation(pattern: ShadowPattern) -> list[str]:
        return [
            f"Workflow signature occurred {pattern.occurrence_count} times.",
            f"All {pattern.successful_count} observed runs completed successfully.",
            "The ordered tool and operation structure remained consistent.",
            f"Verification was present in {pattern.verification_count} run(s).",
            "No equivalent installed skill was found.",
        ]

    @staticmethod
    def _future(value: str) -> bool:
        try:
            return datetime.fromisoformat(value) > datetime.now(UTC)
        except (TypeError, ValueError):
            return False

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.observer:
            self.observer(kind, {**payload, "timestamp": now_utc()})

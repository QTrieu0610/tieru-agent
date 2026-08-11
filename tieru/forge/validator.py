"""Deterministic structural, provenance, safety, and bounds validation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from tieru.forge.generator import REQUIRED_HEADINGS
from tieru.forge.models import SkillDraft
from tieru.memory.personal import contains_secret
from tieru.memory.procedural.loader import _parse_text

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_AUTH = re.compile(
    r"(?:authorization|proxy-authorization|cookie)\s*:\s*(?!\[REDACTED\])\S+",
    re.IGNORECASE,
)
_UNSAFE = (
    re.compile(
        r"\b(?:ignore|bypass|disable|modify|rewrite)\b.{0,40}\btrust(?: kernel| policy)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:permanent(?:ly)?|always)\s+(?:grant|allow|approve|authorized|permission)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:rm\s+-rf\s+[/~*]|delete\s+(?:all|any)\s+files|unrestricted destructive)\b",
        re.IGNORECASE,
    ),
)


class DraftValidator:
    def __init__(self, replay, *, available_tools: Iterable[str] | None = None):
        self.replay = replay
        self.available_tools = set(available_tools or ())

    def validate(self, draft: SkillDraft) -> dict:
        errors: list[str] = []
        warnings: list[str] = []
        text = draft.content
        metadata = draft.metadata
        parsed = _parse_text(text, Path("SKILL.md"))
        if parsed is None:
            errors.append("SKILL.md must have canonical name and description frontmatter")
        else:
            if not _NAME.fullmatch(parsed.name) or len(parsed.name) > 64:
                errors.append("skill name must be a lowercase, path-safe identifier")
            if parsed.name != metadata.get("skill_id"):
                errors.append("frontmatter name does not match metadata skill_id")
        for heading in REQUIRED_HEADINGS:
            if f"## {heading}" not in text:
                errors.append(f"missing required section: {heading}")
        if len(text.encode("utf-8")) > 65_536:
            errors.append("SKILL.md exceeds 65536 bytes")
        if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 65_536:
            errors.append("metadata exceeds 65536 bytes")
        if len(json.dumps(draft.workflow.to_dict(), ensure_ascii=False).encode("utf-8")) > 262_144:
            errors.append("workflow metadata exceeds 262144 bytes")
        if contains_secret(text) or _AUTH.search(text):
            errors.append("SKILL.md appears to contain a secret or credential header")
        if any(pattern.search(text) for pattern in _UNSAFE):
            errors.append("SKILL.md contains unsafe permission, Trust-policy, or destructive language")
        if not str(draft.draft_id).startswith("draft_") or any(part in draft.draft_id for part in ("..", "/", "\\")):
            errors.append("invalid or unsafe draft identifier")

        provenance = metadata.get("provenance")
        if not isinstance(provenance, dict):
            errors.append("missing provenance metadata")
            provenance = {}
        run_ids = provenance.get("source_run_ids") or []
        event_refs = provenance.get("event_refs") or []
        if not run_ids or not event_refs:
            errors.append("provenance requires source runs and event references")
        known_events: set[str] = set()
        for run_id in run_ids:
            try:
                run = self.replay.get_run(str(run_id))
                if run.get("status") != "completed":
                    errors.append(f"source run is not eligible: {run_id}")
                known_events.update(
                    f"{run_id}:{event['event_id']}" for event in self.replay.get_events(str(run_id))
                )
            except KeyError:
                errors.append(f"source run does not exist: {run_id}")
        for ref in event_refs:
            if ref not in known_events:
                errors.append(f"provenance event does not exist: {ref}")

        candidate_tools = set(draft.workflow.tools)
        declared_tools = set(metadata.get("required_tools") or ())
        optional_tools = set(metadata.get("optional_tools") or ())
        if candidate_tools != declared_tools | (candidate_tools & optional_tools):
            errors.append("tool declarations do not match WorkflowCandidate")
        if self.available_tools:
            missing = declared_tools - self.available_tools - optional_tools
            if missing:
                errors.append("required tools are unavailable: " + ", ".join(sorted(missing)))
        elif declared_tools:
            warnings.append("tool availability was provenance-verified but not checked against a live registry")
        if set(metadata.get("required_capabilities") or ()) != set(draft.workflow.capabilities):
            errors.append("capability declarations do not match WorkflowCandidate")
        if "capabilities are requests" not in text.lower():
            errors.append("draft must state that capabilities request rather than grant permission")

        return {
            "passed": not errors,
            "errors": list(dict.fromkeys(errors)),
            "warnings": list(dict.fromkeys(warnings)),
            "deterministic": True,
        }

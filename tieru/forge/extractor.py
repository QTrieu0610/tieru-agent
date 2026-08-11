"""Deterministic extraction of reusable workflow structure from Replay."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from tieru.forge.generalize import generalize
from tieru.forge.models import WorkflowCandidate, WorkflowStep
from tieru.forge.signature import compatible, workflow_signature


class ForgeSourceError(ValueError):
    pass


class WorkflowExtractor:
    def __init__(self, replay):
        self.replay = replay

    def extract(self, run_ids: list[str]) -> WorkflowCandidate:
        ids = list(dict.fromkeys(str(item).strip() for item in run_ids if str(item).strip()))
        if not ids:
            raise ForgeSourceError("at least one Replay run ID is required")
        candidates = [self._one(run_id) for run_id in ids]
        first = candidates[0]
        for item in candidates[1:]:
            if not compatible(first, item):
                raise ForgeSourceError("selected Replay runs are not structurally compatible")
        if len(candidates) > 1:
            first.source_run_ids = ids
            first.evidence = list(dict.fromkeys(ref for c in candidates for ref in c.evidence))
            for index, step in enumerate(first.steps):
                step.evidence = list(dict.fromkeys(
                    ref for c in candidates for ref in c.steps[index].evidence
                ))
        first = generalize(first)
        first.workflow_signature = workflow_signature(first)
        return first

    def _one(self, run_id: str) -> WorkflowCandidate:
        try:
            run = self.replay.get_run(run_id)
            events = self.replay.get_events(run_id)
        except KeyError as exc:
            raise ForgeSourceError(f"Replay run not found: {run_id}") from exc
        if run.get("status") != "completed":
            raise ForgeSourceError(f"Replay run is not completed successfully: {run_id}")
        if len(events) > 1000:
            raise ForgeSourceError(f"Replay run exceeds the Forge event bound: {run_id}")
        types = [event.get("event_type") for event in events]
        if "run_started" not in types or "run_completed" not in types:
            raise ForgeSourceError(f"Replay run has incomplete lifecycle evidence: {run_id}")

        steps: list[WorkflowStep] = []
        pending: WorkflowStep | None = None
        all_evidence = [f"{run_id}:{event['event_id']}" for event in events]
        for event in events:
            event_type = event.get("event_type", "")
            ref = f"{run_id}:{event.get('event_id', '')}"
            payload = event.get("safe_payload") or {}
            tool = str(event.get("tool") or payload.get("tool") or "")
            if event_type == "tool_requested":
                if pending is not None:
                    raise ForgeSourceError(f"Replay run has an unterminated tool request: {run_id}")
                pending = WorkflowStep(
                    step_id=f"step_{len(steps) + 1}", operation=tool or "observable_action",
                    tool=tool, status="requested",
                    arguments=self._bounded_args(payload.get("args") or {}),
                    evidence=[ref], verification=self._verification(tool),
                )
            elif event_type in {"trust_request", "trust_decision", "trust_approval"} and pending:
                caps = payload.get("capability") or payload.get("capabilities") or []
                if isinstance(caps, str):
                    caps = [caps]
                pending.capabilities = list(dict.fromkeys([*pending.capabilities, *map(str, caps)]))
                operation = str(payload.get("operation") or "")
                if operation:
                    pending.operation = operation
                if event_type == "trust_decision":
                    allowed = bool(payload.get("allowed"))
                    pending.trust_requirements.append(
                        "Trust authorization required" + ("" if allowed else "; source action denied")
                    )
                pending.evidence.append(ref)
            elif event_type in {"tool_completed", "tool_failed", "tool_denied"}:
                if pending is None:
                    raise ForgeSourceError(f"Replay run has a tool result without a request: {run_id}")
                if tool and pending.tool and tool != pending.tool:
                    raise ForgeSourceError(f"Replay tool lifecycle is corrupted: {run_id}")
                pending.status = {
                    "tool_completed": "completed", "tool_failed": "failed", "tool_denied": "denied"
                }[event_type]
                pending.evidence.append(ref)
                steps.append(pending)
                pending = None
        if pending is not None:
            raise ForgeSourceError(f"Replay run has an unterminated tool request: {run_id}")
        if not steps:
            raise ForgeSourceError(f"Replay run has no observable tool workflow: {run_id}")
        if len(steps) > 128:
            raise ForgeSourceError(f"Replay run exceeds the Forge step bound: {run_id}")

        tools = list(dict.fromkeys(step.tool for step in steps if step.tool))
        caps = list(dict.fromkeys(cap for step in steps for cap in step.capabilities))
        denied = [step for step in steps if step.status == "denied"]
        failed = [step for step in steps if step.status == "failed"]
        title = " ".join(word.capitalize() for word in tools[:3]) + " Workflow"
        candidate = WorkflowCandidate(
            candidate_id=f"wf_{uuid4().hex[:16]}", source_run_ids=[run_id],
            title=title or "Replay Workflow",
            intent=f"Repeat the observed {' then '.join(tools)} procedure safely.",
            inputs=[], steps=steps,
            outputs=["A verified result consistent with the source workflow."],
            tools=tools, capabilities=caps,
            trust_requirements=["Every action remains subject to the Tieru Trust Kernel."],
            preconditions=["Required tools are available.", "The user explicitly requested this procedure."],
            postconditions=["The workflow result is verified before reporting completion."],
            failure_paths=[
                *(f"Stop or use a read-only fallback when {s.tool} is denied." for s in denied),
                *(f"Report the failure and do not claim success when {s.tool} fails." for s in failed),
            ],
            evidence=list(dict.fromkeys([
                *(ref for ref, event in zip(all_evidence, events, strict=True)
                  if event.get("event_type") in {"run_started", "run_completed"}),
                *(ref for step in steps for ref in step.evidence),
            ])),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        candidate.workflow_signature = workflow_signature(candidate)
        return candidate

    @staticmethod
    def _verification(tool: str) -> bool:
        return bool(re.search(
            r"(?:test|check|verify|status|lint|inspect)", tool, re.IGNORECASE
        ))

    @staticmethod
    def _bounded_args(value) -> dict:
        args = dict(value) if isinstance(value, dict) else {}
        raw = json.dumps(args, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(raw.encode("utf-8")) <= 2048:
            return args
        return {
            "_forge_omitted": "source arguments exceeded the 2048-byte Forge bound",
            "source_size": len(raw.encode("utf-8")),
        }

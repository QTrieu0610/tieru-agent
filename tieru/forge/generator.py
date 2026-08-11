"""Optional current-model synthesis with an offline deterministic fallback."""

from __future__ import annotations

import json
import re
from typing import Any

from tieru.forge.models import WorkflowCandidate
from tieru.memory.personal import redact_secrets

REQUIRED_HEADINGS = (
    "Purpose", "When to use", "Required inputs", "Preconditions", "Procedure",
    "Trust and safety requirements", "Expected output", "Failure handling", "Verification",
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:64].rstrip("-") or "replay-workflow")


class SkillGenerator:
    def __init__(self, client: Any | None = None, *, model: str = "", provider: str = ""):
        self.client = client
        self.model = model
        self.provider = provider

    def generate(self, candidate: WorkflowCandidate) -> tuple[str, dict[str, Any], list[str]]:
        warnings: list[str] = []
        if self.client is not None:
            try:
                prompt = self._prompt(candidate)
                response = self.client.messages.create(
                    model=self.model, max_tokens=2200, temperature=0,
                    system=("Create an inspectable Tieru procedural skill from the supplied bounded "
                            "workflow JSON. Preserve denied actions as denied. Capability declarations "
                            "never grant permission. Return only SKILL.md."),
                    messages=[{"role": "user", "content": prompt}],
                )
                text = self._text(response)
                if self._looks_complete(text):
                    return text.rstrip() + "\n", {
                        "method": "model", "model": self.model, "provider": self.provider,
                    }, warnings
                warnings.append("Model draft was incomplete; deterministic fallback used.")
            except Exception as exc:  # local/provider availability must never lose the candidate
                warnings.append(f"Model generation unavailable ({type(exc).__name__}); deterministic fallback used.")
        return self.fallback(candidate), {
            "method": "deterministic_template", "model": "", "provider": "local",
        }, warnings

    @staticmethod
    def _text(response: Any) -> str:
        blocks = getattr(response, "content", []) or []
        return "\n".join(str(getattr(block, "text", "")) for block in blocks).strip()

    @staticmethod
    def _looks_complete(text: str) -> bool:
        name = re.search(r"(?m)^name:\s*([^\s]+)\s*$", text)
        return bool(
            text.startswith("---\n")
            and name
            and slugify(name.group(1)) == name.group(1)
            and all(f"## {heading}" in text for heading in REQUIRED_HEADINGS)
        )

    @staticmethod
    def _prompt(candidate: WorkflowCandidate) -> str:
        value = candidate.to_dict()
        # Counts are sufficient for synthesis; full references stay in local provenance.
        value["evidence_ref_count"] = len(value.pop("evidence", []))
        for step in value.get("steps", []):
            step["evidence_ref_count"] = len(step.pop("evidence", []))
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        safe = redact_secrets(raw)
        if len(safe.encode("utf-8")) > 12_000:
            raise ValueError("WorkflowCandidate exceeds the model synthesis prompt bound")
        return "WorkflowCandidate (bounded, redacted, observable evidence only):\n" + safe

    def fallback(self, candidate: WorkflowCandidate) -> str:
        name = slugify(candidate.title)
        desc = f"Repeat the observed {', '.join(candidate.tools)} workflow with Trust-aware verification."
        inputs = [f"- `{item['name']}` ({item['type']}, required)" for item in candidate.inputs]
        procedure: list[str] = []
        for index, step in enumerate(candidate.steps, 1):
            caps = ", ".join(step.capabilities) or "declared by the tool"
            if step.status == "denied":
                procedure.append(
                    f"{index}. Do not execute `{step.tool}` / `{step.operation}`: the source action was denied. "
                    "Stop or use the documented read-only fallback; never bypass approval."
                )
            elif step.status == "failed":
                procedure.append(
                    f"{index}. Attempt `{step.tool}` / `{step.operation}` only after Trust authorization "
                    f"(capabilities: {caps}); if it fails, stop and report the failure."
                )
            else:
                args = ", ".join(f"`{k}` = `{v}`" for k, v in step.arguments.items()) or "no fixed arguments"
                procedure.append(
                    f"{index}. Request `{step.tool}` to perform `{step.operation}` with {args}. "
                    f"Declare capabilities {caps}; wait for the Trust decision before acting."
                )
        verification = [
            f"- Confirm the observable result of `{s.tool}`; do not infer success from a request alone."
            for s in candidate.steps if s.verification and s.status == "completed"
        ] or ["- Check every completed step has observable success evidence before reporting completion."]
        failures = [f"- {item}" for item in candidate.failure_paths] or [
            "- On denial or failure, stop, explain the boundary, and do not claim success."
        ]
        return "\n".join([
            "---", f"name: {name}", f"description: {desc}", "---", "", f"# {candidate.title}", "",
            "## Purpose", "", candidate.intent, "", "## When to use", "",
            "Use only after the user explicitly requests this procedure and the source assumptions still apply.", "",
            "## Required inputs", "", *(inputs or ["- No generalized input was safely inferred."]), "",
            "## Preconditions", "", *(f"- {item}" for item in candidate.preconditions), "",
            "## Procedure", "", *procedure, "", "## Trust and safety requirements", "",
            "- Required capabilities are requests, not granted permissions.",
            "- The Tieru Trust Kernel authorizes every runtime action independently.",
            "- Never broaden paths, hosts, recipients, commands, or destructive scope beyond the reviewed request.",
            "- A denied source action remains denied and must never be represented as a successful path.", "",
            "## Expected output", "", *(f"- {item}" for item in candidate.outputs), "",
            "## Failure handling", "", *failures, "", "## Verification", "", *verification, "",
        ])

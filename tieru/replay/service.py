"""Read-only inspection and deterministic summaries for Tieru Replay."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tieru.config import Settings
from tieru.replay.normalize import ReplayNormalizer, bounded_text
from tieru.replay.store import ReplayStore


class ReplayService:
    def __init__(self, conn, settings: Settings):
        self.settings = settings
        self.store = ReplayStore(conn)
        self.normalizer = ReplayNormalizer(
            settings.replay_max_event_payload_bytes,
            settings.replay_max_tool_output_bytes,
        )

    def start_run(self, **fields):
        preview, _, _ = bounded_text(fields.pop("user_input", ""), 240)
        run = self.store.start_run(input_preview=preview, **fields)
        self.record_event(run.run_id, "run_started", {"status": "running"})
        self.store.cleanup(
            max_runs=self.settings.replay_max_runs,
            max_age_days=self.settings.replay_max_age_days,
        )
        return run

    def record_event(self, run_id: str, kind: str, event: dict[str, Any]):
        normalized = self.normalizer.normalize(kind, event)
        return self.store.append(run_id, normalized) if normalized else None

    def complete_run(
        self,
        run_id: str,
        *,
        output: str,
        iterations: int,
        latency_ms: int,
        role: str,
        model: str,
        provider: str,
    ):
        preview, size, truncated = bounded_text(output, self.settings.replay_max_tool_output_bytes)
        self.record_event(
            run_id,
            "run_completed",
            {"status": "completed", "output_size": size, "truncated": truncated},
        )
        return self.store.finish(
            run_id,
            status="completed",
            iterations=iterations,
            latency_ms=latency_ms,
            role=role,
            model=model,
            provider=provider,
            output_preview=preview,
        )

    def fail_run(
        self,
        run_id: str,
        *,
        error_code: str,
        error_summary: str,
        latency_ms: int,
        role: str,
        model: str,
        provider: str,
    ):
        summary, _, _ = bounded_text(error_summary, 500)
        self.record_event(
            run_id,
            "run_failed",
            {"error_code": error_code, "error_summary": summary},
        )
        return self.store.finish(
            run_id,
            status="failed",
            iterations=0,
            latency_ms=latency_ms,
            role=role,
            model=model,
            provider=provider,
            error_code=error_code,
            error_summary=summary,
        )

    def get_run(self, run_id: str) -> dict:
        return asdict(self.store.get_run(run_id))

    def list_runs(self, **filters) -> list[dict]:
        return [asdict(run) for run in self.store.list_runs(**filters)]

    def get_events(self, run_id: str, **options) -> list[dict]:
        return [asdict(event) for event in self.store.get_events(run_id, **options)]

    def inspect(self, run_id: str, *, include_events: bool = True) -> dict:
        run = self.get_run(run_id)
        run["summary"] = self.summarize_run(run_id)
        if include_events:
            run["events"] = self.get_events(run_id)
        return run

    def summarize_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        events = self.store.get_events(run_id)
        tool = {"successful": 0, "denied": 0, "failed": 0}
        trust = {"allowed": 0, "denied": 0}
        memory: list[str] = []
        fabric: dict[str, Any] = {}
        initial_model = {"provider": run.provider, "model": run.model, "candidate_id": ""}
        final_model = {"provider": run.provider, "model": run.model, "candidate_id": ""}
        fallback_count = 0
        for event in events:
            if event.event_type == "tool_completed":
                tool["successful"] += 1
            elif event.event_type == "tool_denied":
                tool["denied"] += 1
            elif event.event_type == "tool_failed":
                tool["failed"] += 1
            elif event.event_type == "trust_decision":
                key = "allowed" if event.safe_payload.get("allowed") else "denied"
                trust[key] += 1
            elif event.category == "memory" and event.event_type not in memory:
                memory.append(event.event_type)
            elif event.event_type == "fabric_route":
                fabric = {
                    "execution_mode": event.safe_payload.get("execution_mode", ""),
                    "reason_codes": event.safe_payload.get("reason_codes", []),
                    "classifier_source": event.safe_payload.get("classifier_source", ""),
                    "fallback_used": bool(event.safe_payload.get("fallback_used")),
                }
            elif event.event_type == "fabric_selection":
                selected = {
                    "provider": event.provider or event.safe_payload.get("provider", ""),
                    "model": event.model or event.safe_payload.get("model", ""),
                    "candidate_id": event.safe_payload.get("candidate_id", ""),
                }
                if not initial_model["candidate_id"]:
                    initial_model = selected
                final_model = selected
                fallback_count = max(
                    fallback_count, int(event.safe_payload.get("fallback_count") or 0)
                )
        if fabric:
            fabric.update({
                "initial_model": initial_model,
                "final_model": final_model,
                "fallback_used": fallback_count > 0 or fabric.get("fallback_used", False),
                "fallback_count": fallback_count,
            })
        return {
            "status": run.status,
            "sentence": (
                "Run completed successfully."
                if run.status == "completed"
                else "Run failed."
                if run.status == "failed"
                else "Run is still in progress."
            ),
            "model": {"role": run.role, "model": run.model, "provider": run.provider},
            "fabric": fabric,
            "memory": memory,
            "tools": tool,
            "trust": trust,
            "duration_ms": run.latency_ms,
            "iterations": run.iterations,
            "error": {"code": run.error_code, "summary": run.error_summary},
        }

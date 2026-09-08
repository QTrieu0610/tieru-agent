"""Human-controlled recovery orchestration; never invokes a tool function."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tieru.recovery.models import RecoveryError, RecoveryResolution
from tieru.recovery.store import RecoveryStore
from tieru.replay import new_run_id
from tieru.tasks.models import StepExecution, TaskStatus, VerificationStatus


class RecoveryService:
    def __init__(self, store: RecoveryStore, *, replay=None) -> None:
        self.store = store
        self.replay = replay

    def list_uncertain(self, *, limit: int = 50) -> list[dict]:
        return self.store.list_uncertain(limit=limit)

    def show_execution(self, action_fingerprint: str) -> dict:
        return self.store.inspect_execution(action_fingerprint)

    def _start_replay(self, resource_id: str) -> str:
        if self.replay is None:
            return ""
        run_id = new_run_id()
        try:
            self.replay.start_run(
                run_id=run_id,
                session_id="recovery",
                source="recovery",
                role="system",
                model="",
                provider="",
                user_input="",
            )
            self.replay.record_event(
                run_id,
                "recovery_opened",
                {"resource_id": str(resource_id)[:256], "status": "opened"},
            )
            return run_id
        except Exception:
            return ""

    def _event(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        if not run_id or self.replay is None:
            return
        try:
            self.replay.record_event(run_id, kind, payload)
        except Exception:
            return

    def _finish_replay(self, run_id: str, output: str) -> None:
        if not run_id or self.replay is None:
            return
        try:
            self.replay.complete_run(
                run_id,
                output=output,
                iterations=0,
                latency_ms=0,
                role="system",
                model="",
                provider="",
            )
        except Exception:
            return

    def resolve_execution(
        self,
        action_fingerprint: str,
        resolution: RecoveryResolution | str,
        *,
        note: str = "",
        source: str = "cli",
    ) -> dict:
        try:
            selected = RecoveryResolution(str(resolution))
        except ValueError as exc:
            raise RecoveryError("resolution_invalid", "Recovery resolution is invalid.") from exc
        run_id = self._start_replay(action_fingerprint)
        try:
            decision, created = self.store.resolve_execution(
                action_fingerprint,
                selected,
                note=note,
                source=source,
                replay_run_id=run_id,
            )
        except Exception as exc:
            self._event(
                run_id,
                "recovery_decision",
                {
                    "resource_type": "execution",
                    "resource_id": str(action_fingerprint)[:256],
                    "resolution": selected.value,
                    "status": "rejected",
                    "error_code": str(getattr(exc, "code", type(exc).__name__)),
                },
            )
            self._finish_replay(run_id, "Recovery decision was rejected.")
            raise
        payload = {
            "recovery_id": decision.recovery_id,
            "resource_type": "execution",
            "resource_id": decision.resource_id,
            "resolution": decision.resolution.value,
            "status": "created" if created else "already_resolved",
        }
        self._event(run_id, "recovery_decision", payload)
        self._event(
            run_id,
            "execution_reconciled",
            {**payload, "status": self.store.inspect_execution(action_fingerprint)["execution"]["status"]},
        )
        if selected is RecoveryResolution.CONFIRMED_NOT_EXECUTED:
            permit = self.store.get_permit(action_fingerprint)
            self._event(
                run_id,
                "manual_retry_authorized",
                {
                    **payload,
                    "permit_id": permit.permit_id if permit else "",
                    "status": "available" if permit and permit.consumed_at is None else "consumed",
                },
            )
        self._finish_replay(run_id, f"Recovery {payload['status']}: {selected.value}")
        return {
            "decision": asdict(decision),
            "created": created,
            "execution": self.store.inspect_execution(action_fingerprint)["execution"],
        }

    def _step_executions(self, run_id: str) -> list[dict]:
        if not run_id:
            return []
        rows = self.store.conn.execute(
            """SELECT DISTINCT x.* FROM tool_executions x
               JOIN replay_events e ON e.run_id=?
                    AND instr(e.payload_json, x.action_fingerprint) > 0
               ORDER BY x.updated_at DESC""",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_task(
        self,
        task_id: str,
        task_service,
        *,
        note: str = "",
        source: str = "cli",
        observer=None,
    ) -> dict:
        task = task_service.store.get_task(task_id)
        if task.status is not TaskStatus.BLOCKED:
            raise RecoveryError("task_not_blocked", "Only a blocked task can be recovered.")
        blocked = [
            step for step in task_service.store.list_steps(task_id) if step.status.value == "blocked"
        ]
        if len(blocked) != 1:
            raise RecoveryError(
                "task_recovery_ambiguous", "Task must have exactly one blocked step."
            )
        step = blocked[0]
        run_id = self._start_replay(task_id)
        self._event(
            run_id,
            "task_recovery_started",
            {"task_id": task_id, "step_id": step.step_id, "status": "blocked"},
        )
        executions = self._step_executions(step.execution_run_id or "")
        relevant = [
            item
            for item in executions
            if item["status"] == "uncertain" or item.get("recovery_id")
        ]
        if len(relevant) > 1:
            self._event(
                run_id,
                "task_recovery_blocked",
                {"task_id": task_id, "step_id": step.step_id, "status": "ambiguous"},
            )
            self._finish_replay(run_id, "Task recovery remained blocked.")
            raise RecoveryError(
                "task_recovery_ambiguous",
                "Blocked step refers to multiple ambiguous executions.",
            )

        if relevant:
            execution = relevant[0]
            fingerprint = execution["action_fingerprint"]
            decisions = self.store.get_decisions("execution", fingerprint)
            if not decisions:
                self._event(
                    run_id,
                    "task_recovery_blocked",
                    {
                        "task_id": task_id,
                        "step_id": step.step_id,
                        "status": "execution_recovery_required",
                    },
                )
                self._finish_replay(run_id, "Task recovery requires execution reconciliation.")
                raise RecoveryError(
                    "execution_recovery_required",
                    "The step execution must be reconciled before task recovery.",
                )
            resolution = decisions[0].resolution
            if resolution is RecoveryResolution.ABANDONED:
                self.store.record_task_decision(
                    resource_type="task_step",
                    resource_id=step.step_id,
                    previous_status="blocked",
                    resolution=RecoveryResolution.VERIFICATION_BLOCKED,
                    note=note,
                    source=source,
                    replay_run_id=run_id,
                )
                self._event(
                    run_id,
                    "task_recovery_blocked",
                    {"task_id": task_id, "step_id": step.step_id, "status": "abandoned"},
                )
                self._finish_replay(run_id, "Task recovery remained blocked: action abandoned.")
                return {"task": asdict(task), "step": asdict(step), "code": "action_abandoned"}
            if resolution is RecoveryResolution.CONFIRMED_NOT_EXECUTED:
                permit = self.store.get_permit(fingerprint)
                if permit is None or permit.consumed_at is not None:
                    self._event(
                        run_id,
                        "task_recovery_blocked",
                        {
                            "task_id": task_id,
                            "step_id": step.step_id,
                            "status": "manual_retry_unavailable",
                        },
                    )
                    self._finish_replay(run_id, "Task recovery has no retry permit.")
                    raise RecoveryError(
                        "manual_retry_unavailable", "No unused manual retry permit is available."
                    )
                self.store.record_task_decision(
                    resource_type="task_step",
                    resource_id=step.step_id,
                    previous_status="blocked",
                    resolution=RecoveryResolution.RETRY_STEP,
                    note=note,
                    source=source,
                    replay_run_id=run_id,
                )
                task, step = task_service.store.prepare_blocked_step_retry(task_id)
                self._event(
                    run_id,
                    "task_recovery_completed",
                    {"task_id": task_id, "step_id": step.step_id, "status": "runnable"},
                )
                self._finish_replay(run_id, "Task step prepared for an explicit retry.")
                return {"task": asdict(task), "step": asdict(step), "code": "retry_prepared"}

            evidence = str(execution.get("result") or "")
            verification = task_service.executor.verifier.verify(
                task,
                step,
                StepExecution(
                    result=evidence,
                    run_id=step.execution_run_id or "",
                    tool_calls=(),
                ),
            )
            resolution = (
                RecoveryResolution.VERIFICATION_PASSED
                if verification.status is VerificationStatus.PASS
                else RecoveryResolution.VERIFICATION_BLOCKED
            )
            self.store.record_task_decision(
                resource_type="task_step",
                resource_id=step.step_id,
                previous_status="blocked",
                resolution=resolution,
                note=note,
                source=source,
                replay_run_id=run_id,
            )
            if verification.status is not VerificationStatus.PASS:
                self._event(
                    run_id,
                    "task_recovery_blocked",
                    {
                        "task_id": task_id,
                        "step_id": step.step_id,
                        "status": "verification_blocked",
                    },
                )
                self._finish_replay(run_id, "Task recovery verification was inconclusive.")
                return {
                    "task": asdict(task),
                    "step": asdict(step),
                    "code": "verification_blocked",
                }
            task, step = task_service.store.complete_blocked_step_after_verification(
                step.step_id,
                result=evidence,
                verification=verification,
            )
            self._event(
                run_id,
                "task_recovery_completed",
                {"task_id": task_id, "step_id": step.step_id, "status": task.status.value},
            )
            self._finish_replay(run_id, "Task recovery verification passed.")
            return {"task": asdict(task), "step": asdict(step), "code": "verification_passed"}

        # Permission-denied and other tool-free blocked steps may be retried only
        # through an explicit recovery call; Trust policy is not changed here.
        self.store.record_task_decision(
            resource_type="task_step",
            resource_id=step.step_id,
            previous_status="blocked",
            resolution=RecoveryResolution.RETRY_STEP,
            note=note,
            source=source,
            replay_run_id=run_id,
        )
        task, step = task_service.store.prepare_blocked_step_retry(task_id)
        self._event(
            run_id,
            "task_recovery_completed",
            {"task_id": task_id, "step_id": step.step_id, "status": "runnable"},
        )
        self._finish_replay(run_id, "Task step prepared for an explicit retry.")
        if observer is not None:
            observer(
                "task_recovery_completed",
                {"task_id": task_id, "step_id": step.step_id, "status": "runnable"},
            )
        return {"task": asdict(task), "step": asdict(step), "code": "retry_prepared"}

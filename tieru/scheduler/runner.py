"""Bounded foreground scheduler tick using the normal Durable Task path."""

from __future__ import annotations

from datetime import UTC, datetime

from tieru.replay import ReplayRecorder, new_run_id
from tieru.scheduler.models import ScheduleRunStatus, TickResult
from tieru.scheduler.store import ScheduleStore
from tieru.tasks.models import TaskStatus

_TASK_TO_RUN = {
    TaskStatus.COMPLETED: ScheduleRunStatus.COMPLETED,
    TaskStatus.BLOCKED: ScheduleRunStatus.BLOCKED,
    TaskStatus.FAILED: ScheduleRunStatus.FAILED,
    TaskStatus.CANCELLED: ScheduleRunStatus.CANCELLED,
}


class SchedulerRunner:
    """One deterministic foreground pass; no daemon or direct tool execution."""

    def __init__(self, store: ScheduleStore, tasks, *, replay=None, clock=None) -> None:
        self.store = store
        self.tasks = tasks
        self.replay = replay
        self.clock = clock or (lambda: datetime.now(UTC))

    def _recorder(self) -> ReplayRecorder | None:
        if self.replay is None:
            return None
        recorder = ReplayRecorder(self.replay)
        recorder.start(
            run_id=new_run_id(), session_id="scheduler", source="scheduler",
            role="main", model="", provider="", user_input="scheduler tick",
        )
        return recorder

    @staticmethod
    def _event(recorder, observer, kind: str, payload: dict) -> None:
        if recorder is not None:
            recorder.event(kind, payload)
        if observer is not None:
            observer(kind, payload)

    def _reconcile(self, occurrence, task):
        mapped = _TASK_TO_RUN.get(task.status)
        if mapped is None:
            if occurrence.status is ScheduleRunStatus.BLOCKED:
                return self.store.reconcile(
                    occurrence.run_id, ScheduleRunStatus.RUNNING,
                    outcome="explicit_recovery_observed",
                )
            return occurrence
        return self.store.reconcile(
            occurrence.run_id, mapped, outcome=f"task_{task.status.value}"
        )

    def tick(self, *, max_occurrences: int = 10, observer=None) -> TickResult:
        bound = max(1, min(int(max_occurrences), 100))
        recorder = self._recorder()
        result = TickResult()
        try:
            existing = self.store.pending_runs(limit=bound)
            remaining = bound - len(existing)
            due = self.store.claim_due(self.clock(), limit=remaining) if remaining else []
            skipped = sum(
                run.status is ScheduleRunStatus.SKIPPED_OVERLAP for run in due
            )
            result = TickResult(claimed=len(due) - skipped, skipped_overlap=skipped)
            for run in due:
                kind = (
                    "schedule_overlap_skipped"
                    if run.status is ScheduleRunStatus.SKIPPED_OVERLAP
                    else "schedule_occurrence_claimed"
                )
                self._event(
                    recorder, observer, kind,
                    {"schedule_id": run.schedule_id, "schedule_run_id": run.run_id,
                     "scheduled_for": run.scheduled_for, "status": run.status.value},
                )

            work = existing + [
                run for run in due if run.status is not ScheduleRunStatus.SKIPPED_OVERLAP
            ]
            materialized = advanced = reconciled = blocked = 0
            for occurrence in work:
                schedule = self.store.get(occurrence.schedule_id)
                task = (
                    self.tasks.store.get_task(occurrence.task_id)
                    if occurrence.task_id else
                    self.tasks.store.get_task_by_source("scheduled", occurrence.run_id)
                )
                if task is None:
                    try:
                        task = self.tasks.create(
                            goal=schedule.goal, source="scheduled",
                            source_id=occurrence.run_id,
                            session_id=f"schedule:{schedule.schedule_id}",
                            observer=observer,
                        )
                    except Exception as exc:
                        self._event(
                            recorder, observer, "schedule_materialization_deferred",
                            {"schedule_id": schedule.schedule_id,
                             "schedule_run_id": occurrence.run_id,
                             "error_code": type(exc).__name__},
                        )
                        continue
                if occurrence.task_id is None:
                    occurrence = self.store.link_task(occurrence.run_id, task.task_id)
                    materialized += 1
                    self._event(
                        recorder, observer, "schedule_task_materialized",
                        {"schedule_id": schedule.schedule_id,
                         "schedule_run_id": occurrence.run_id,
                         "task_id": task.task_id},
                    )

                before = occurrence.status
                occurrence = self._reconcile(occurrence, task)
                if occurrence.status in {
                    ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED,
                    ScheduleRunStatus.CANCELLED,
                }:
                    if before is not occurrence.status:
                        reconciled += 1
                    self._event(
                        recorder, observer, "schedule_occurrence_reconciled",
                        {"schedule_id": schedule.schedule_id,
                         "schedule_run_id": occurrence.run_id,
                         "task_id": task.task_id, "status": occurrence.status.value},
                    )
                    continue
                if task.status is TaskStatus.BLOCKED:
                    blocked += 1
                    self._event(
                        recorder, observer, "schedule_occurrence_blocked",
                        {"schedule_id": schedule.schedule_id,
                         "schedule_run_id": occurrence.run_id,
                         "task_id": task.task_id, "status": "blocked"},
                    )
                    continue

                self.store.mark_running(occurrence.run_id)
                runs = self.tasks.resume(
                    task.task_id, max_steps=1, observer=observer
                )
                advanced += int(bool(runs and runs[0].executed))
                task = self.tasks.store.get_task(task.task_id)
                final = self._reconcile(self.store.get_run(occurrence.run_id), task)
                if final.status in {
                    ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED,
                    ScheduleRunStatus.CANCELLED,
                }:
                    reconciled += 1
                elif final.status is ScheduleRunStatus.BLOCKED:
                    blocked += 1
                self._event(
                    recorder, observer, "schedule_occurrence_advanced",
                    {"schedule_id": schedule.schedule_id,
                     "schedule_run_id": occurrence.run_id, "task_id": task.task_id,
                     "status": final.status.value},
                )
            result = TickResult(
                claimed=result.claimed, materialized=materialized, advanced=advanced,
                reconciled=reconciled, blocked=blocked,
                skipped_overlap=result.skipped_overlap,
            )
            if recorder is not None:
                recorder.complete(
                    output="scheduler tick completed", iterations=len(work), latency_ms=0,
                    role="main", model="", provider="",
                )
            return result
        except Exception as exc:
            if recorder is not None:
                recorder.fail(
                    error_code=type(exc).__name__, error_summary="scheduler tick failed",
                    latency_ms=0, role="main", model="", provider="",
                )
            raise

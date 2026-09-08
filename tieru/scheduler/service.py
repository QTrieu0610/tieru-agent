"""User-facing lifecycle operations for persistent schedules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tieru.scheduler.models import ScheduleStatus, TriggerType
from tieru.scheduler.recurrence import interval_spec, once_spec, parse_interval, timezone
from tieru.scheduler.store import ScheduleStore


class SchedulerService:
    def __init__(self, store: ScheduleStore, *, clock=None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    def create_once(
        self, *, name: str, goal: str, at: str | datetime, timezone_name: str = "UTC"
    ):
        timezone(timezone_name)
        spec, next_run = once_spec(at, timezone_name)
        return self.store.create(
            name=name, goal=goal, trigger_type=TriggerType.ONCE,
            trigger_spec=spec, timezone=timezone_name, next_run_at=next_run,
        )

    def create_interval(
        self, *, name: str, goal: str, every: str | int,
        timezone_name: str = "UTC", anchor: str | datetime | None = None,
    ):
        timezone(timezone_name)
        first = anchor
        if first is None:
            first = self.clock() + timedelta(seconds=parse_interval(every))
        spec, next_run = interval_spec(every, timezone_name, anchor=first)
        return self.store.create(
            name=name, goal=goal, trigger_type=TriggerType.INTERVAL,
            trigger_spec=spec, timezone=timezone_name, next_run_at=next_run,
        )

    def pause(self, schedule_id: str):
        return self.store.transition(schedule_id, ScheduleStatus.PAUSED)

    def resume(self, schedule_id: str):
        return self.store.transition(schedule_id, ScheduleStatus.ACTIVE)

    def cancel(self, schedule_id: str):
        return self.store.transition(schedule_id, ScheduleStatus.CANCELLED)

    def show(self, schedule_id: str):
        return self.store.get(schedule_id)

    def list(self, *, status: ScheduleStatus | None = None, limit: int = 100):
        return self.store.list(status=status, limit=limit)

    def runs(self, schedule_id: str, *, limit: int = 100):
        self.store.get(schedule_id)
        return self.store.list_runs(schedule_id, limit=limit)

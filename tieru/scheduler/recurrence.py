"""Deterministic, timezone-aware trigger parsing and recurrence math."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tieru.scheduler.models import ScheduleValidationError, TriggerType

_INTERVAL = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
MIN_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 366 * 86400


def timezone(name: str) -> tzinfo:
    if str(name).upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleValidationError(f"unknown timezone: {name}") from exc


def aware(value: str | datetime, timezone_name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ScheduleValidationError("timestamp must be ISO 8601") from exc
    zone = timezone(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ScheduleValidationError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_interval(value: str | int) -> int:
    if isinstance(value, int):
        seconds = value
    else:
        match = _INTERVAL.fullmatch(str(value))
        if match is None:
            raise ScheduleValidationError("interval must use m, h, or d (for example 6h)")
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        raise ScheduleValidationError(
            f"interval must be {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS} seconds"
        )
    return seconds


def once_spec(at: str | datetime, timezone_name: str) -> tuple[str, str]:
    instant = aware(at, timezone_name)
    return json.dumps({"at": iso(instant)}, sort_keys=True), iso(instant)


def interval_spec(
    every: str | int,
    timezone_name: str,
    *,
    anchor: str | datetime,
) -> tuple[str, str]:
    seconds = parse_interval(every)
    instant = aware(anchor, timezone_name)
    return (
        json.dumps({"seconds": seconds, "anchor": iso(instant)}, sort_keys=True),
        iso(instant),
    )


def latest_due(
    trigger_type: TriggerType,
    trigger_spec: str,
    next_run_at: str,
    now: datetime,
) -> tuple[datetime, datetime | None]:
    """Return the latest due logical occurrence and its no-drift successor."""
    logical = aware(next_run_at, "UTC")
    current = now.astimezone(UTC)
    if logical > current:
        raise ScheduleValidationError("schedule is not due")
    if trigger_type is TriggerType.ONCE:
        return logical, None
    try:
        seconds = int(json.loads(trigger_spec)["seconds"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScheduleValidationError("invalid interval trigger") from exc
    interval = timedelta(seconds=parse_interval(seconds))
    elapsed = current - logical
    latest = logical + interval * int(elapsed // interval)
    return latest, latest + interval

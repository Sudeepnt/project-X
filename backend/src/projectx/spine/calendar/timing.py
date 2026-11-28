"""Pure time logic: free slots inside working hours, and when a reminder should fire next.

No database and no model here, so every rule is easy to test, including the days when clocks
change (local times are built with zoneinfo and converted to UTC one by one).
"""

from __future__ import annotations

import calendar as _cal
import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from projectx.kernel.time import zone

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REPEATS = ("none", "daily", "weekly", "monthly", "yearly")


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    h, m = int(hours), int(minutes)
    if h == 24 and m == 0:
        return time.max
    return time(h, m)


def validate_working_hours(hours: dict[str, Any]) -> dict[str, list[list[str]]]:
    """Check and normalise {"mon": [["09:00", "17:00"]], ...}. Raises ValueError with a clear message."""
    clean: dict[str, list[list[str]]] = {}
    for day in WEEKDAYS:
        windows = hours.get(day, [])
        if not isinstance(windows, list):
            raise ValueError(f"{day}: expected a list of [start, end] pairs")
        out: list[list[str]] = []
        for window in windows:
            if not (isinstance(window, list | tuple) and len(window) == 2):
                raise ValueError(f"{day}: each window is [start, end]")
            try:
                start, end = parse_hhmm(str(window[0])), parse_hhmm(str(window[1]))
            except ValueError:
                raise ValueError(f"{day}: times look like 09:00") from None
            if start >= end:
                raise ValueError(f"{day}: {window[0]} must be before {window[1]}")
            out.append([str(window[0]), str(window[1])])
        out.sort()
        for a, b in itertools.pairwise(out):
            if parse_hhmm(a[1]) > parse_hhmm(b[0]):
                raise ValueError(f"{day}: windows overlap")
        clean[day] = out
    unknown = set(hours) - set(WEEKDAYS)
    if unknown:
        raise ValueError(f"unknown day(s): {', '.join(sorted(unknown))}")
    return clean


def working_intervals(hours: dict[str, Any], start: datetime, end: datetime, tz_name: str) -> list[Interval]:
    """Working windows between start and end, as UTC intervals."""
    tz = zone(tz_name)
    out: list[Interval] = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date()
    while day <= last:
        for window in hours.get(WEEKDAYS[day.weekday()], []):
            a = datetime.combine(day, parse_hhmm(window[0]), tzinfo=tz).astimezone(UTC)
            end_t = parse_hhmm(window[1])
            b = (
                datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
                if end_t == time.max
                else datetime.combine(day, end_t, tzinfo=tz)
            ).astimezone(UTC)
            a, b = max(a, start), min(b, end)
            if a < b:
                out.append(Interval(a, b))
        day += timedelta(days=1)
    return out


def subtract(free: list[Interval], busy: list[Interval]) -> list[Interval]:
    busy = sorted(busy, key=lambda i: i.start)
    out: list[Interval] = []
    for window in free:
        cursor = window.start
        for b in busy:
            if b.end <= cursor or b.start >= window.end:
                continue
            if b.start > cursor:
                out.append(Interval(cursor, b.start))
            cursor = max(cursor, b.end)
            if cursor >= window.end:
                break
        if cursor < window.end:
            out.append(Interval(cursor, window.end))
    return out


def free_slots(
    *,
    hours: dict[str, Any],
    busy: list[Interval],
    start: datetime,
    end: datetime,
    duration: timedelta,
    tz_name: str,
    now: datetime,
    buffer: timedelta = timedelta(0),
    min_notice: timedelta = timedelta(0),
    step: timedelta = timedelta(minutes=30),
    limit: int = 20,
    per_day: int | None = None,
) -> list[Interval]:
    """Open slots of `duration`, starting on `step` boundaries (local clock), inside working hours,
    clear of busy time plus `buffer` on each side, and no sooner than `now + min_notice`."""
    earliest = max(start, now + min_notice)
    if earliest >= end or duration <= timedelta(0):
        return []
    padded = [Interval(b.start - buffer, b.end + buffer) for b in busy]
    open_time = subtract(working_intervals(hours, earliest, end, tz_name), padded)
    tz = zone(tz_name)
    step_s = max(int(step.total_seconds()), 60)
    slots: list[Interval] = []
    per_day_count: dict[date, int] = {}
    for window in open_time:
        local = window.start.astimezone(tz)
        midnight = datetime.combine(local.date(), time(0), tzinfo=tz)
        offset = (local - midnight).total_seconds()
        aligned = midnight + timedelta(seconds=-(-offset // step_s) * step_s)
        cursor = aligned.astimezone(UTC)
        while cursor + duration <= window.end:
            day = cursor.astimezone(tz).date()
            if per_day is None or per_day_count.get(day, 0) < per_day:
                slots.append(Interval(cursor, cursor + duration))
                per_day_count[day] = per_day_count.get(day, 0) + 1
                if len(slots) >= limit:
                    return slots
            cursor += timedelta(seconds=step_s)
    return slots


def overlaps(a: Interval, b: Interval) -> bool:
    return a.start < b.end and b.start < a.end


# ---------------------------------------------------------------- reminders


def notify_points(due_at: datetime, before_minutes: list[int]) -> list[datetime]:
    return sorted({due_at - timedelta(minutes=m) for m in before_minutes} | {due_at})


def next_notify_at(
    due_at: datetime, before_minutes: list[int], *, last_notified_at: datetime | None, now: datetime
) -> datetime | None:
    """The next moment to remind. A new reminder whose early warnings are already in the past fires
    once straight away (a domain that expires in 2 days still gets its "30 days before" warning now)."""
    points = notify_points(due_at, before_minutes)
    if last_notified_at is None:
        if due_at <= now:
            return None
        if any(p <= now for p in points):
            return now
        return points[0]
    later = [p for p in points if p > last_notified_at]
    return later[0] if later else None


def advance(due_at: datetime, repeat: str, tz_name: str) -> datetime:
    """The next occurrence, on the same local wall-clock time (so 09:00 stays 09:00 across DST)."""
    if repeat == "none":
        return due_at
    tz = zone(tz_name)
    local = due_at.astimezone(tz)
    if repeat == "daily":
        nxt = local.replace(tzinfo=None) + timedelta(days=1)
    elif repeat == "weekly":
        nxt = local.replace(tzinfo=None) + timedelta(days=7)
    elif repeat in ("monthly", "yearly"):
        months = 1 if repeat == "monthly" else 12
        y, m = divmod(local.month - 1 + months, 12)
        year, month = local.year + y, m + 1
        day = min(local.day, _cal.monthrange(year, month)[1])
        nxt = local.replace(tzinfo=None, year=year, month=month, day=day)
    else:
        raise ValueError(f"unknown repeat {repeat!r}")
    return nxt.replace(tzinfo=tz).astimezone(UTC)


def roll_forward(due_at: datetime, repeat: str, tz_name: str, now: datetime) -> datetime:
    """Advance a repeating due date until it is in the future."""
    nxt = due_at
    for _ in range(10_000):
        if nxt > now:
            return nxt
        nxt = advance(nxt, repeat, tz_name)
    return nxt

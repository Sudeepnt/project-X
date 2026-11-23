"""Time helpers. Everything is stored and computed in UTC; local time only at the edges."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC)


def today_in(tz_name: str) -> date:
    """The current calendar date in a business's own timezone."""
    return utcnow().astimezone(zone(tz_name)).date()


def zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_timezone(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def start_of_day_utc(day: date, tz_name: str) -> datetime:
    """Midnight of `day` in the business timezone, expressed in UTC."""
    local = datetime(day.year, day.month, day.day, tzinfo=zone(tz_name))
    return local.astimezone(UTC)


def days_between(start: date, end: date) -> int:
    return (end - start).days


def add_days(moment: datetime, days: float) -> datetime:
    return moment + timedelta(days=days)

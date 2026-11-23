"""The holiday and event calendar: public holidays for the audience's countries plus the
marketing moments businesses plan around (Diwali, Black Friday, Christmas…)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import holidays as holidays_lib


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def marketing_moments(year: int) -> list[dict[str, Any]]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return [
        {"date": date(year, 1, 1), "name": "New Year's Day", "kind": "moment"},
        {"date": date(year, 2, 14), "name": "Valentine's Day", "kind": "moment"},
        {"date": date(year, 3, 8), "name": "International Women's Day", "kind": "moment"},
        {"date": _nth_weekday(year, 5, 6, 2), "name": "Mother's Day (US, UK differs)", "kind": "moment"},
        {"date": _nth_weekday(year, 6, 6, 3), "name": "Father's Day", "kind": "moment"},
        {"date": date(year, 10, 31), "name": "Halloween", "kind": "moment"},
        {"date": thanksgiving + timedelta(days=1), "name": "Black Friday", "kind": "sale"},
        {"date": thanksgiving + timedelta(days=4), "name": "Cyber Monday", "kind": "sale"},
        {"date": thanksgiving + timedelta(days=5), "name": "Giving Tuesday", "kind": "moment"},
        {"date": date(year, 12, 25), "name": "Christmas", "kind": "moment"},
        {"date": date(year, 12, 31), "name": "New Year's Eve", "kind": "moment"},
        {"date": _last_weekday(year, 7, 4), "name": "Mid-year sales season", "kind": "sale"},
    ]


def upcoming(countries: list[str], start: date, days: int = 30) -> list[dict[str, Any]]:
    end = start + timedelta(days=days)
    out: dict[tuple[date, str], dict[str, Any]] = {}
    years = {start.year, end.year}
    for country in countries or ["US"]:
        try:
            calendar = holidays_lib.country_holidays(country.upper(), years=years)
        except (NotImplementedError, KeyError):
            continue
        for day, name in calendar.items():
            if start <= day <= end:
                out[(day, name)] = {
                    "date": day.isoformat(),
                    "name": name,
                    "kind": "holiday",
                    "country": country.upper(),
                }
    for year in years:
        for moment in marketing_moments(year):
            if start <= moment["date"] <= end:
                out[(moment["date"], moment["name"])] = {**moment, "date": moment["date"].isoformat()}
    return sorted(out.values(), key=lambda m: (m["date"], m["name"]))

"""Preserve event time precision without inventing a day or clock time."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class EventTime:
    start: datetime
    end: datetime | None
    precision: Literal["year", "month", "day", "instant"]


def parse_event_time(value: str, *, timezone: str = "Asia/Shanghai") -> EventTime:
    """Interpret an exact time as a point and a calendar date as an interval."""
    zone = ZoneInfo(timezone)
    if re.fullmatch(r"\d{4}", value):
        year = int(value)
        start = datetime(year, 1, 1, tzinfo=zone)
        end = datetime(year + 1, 1, 1, tzinfo=zone) if year < 9999 else None
        return EventTime(start, end, "year")
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        start = datetime(year, month, 1, tzinfo=zone)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=zone) if year < 9999 else None
        else:
            end = datetime(year, month + 1, 1, tzinfo=zone)
        return EventTime(start, end, "month")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        start = datetime.fromisoformat(value).replace(tzinfo=zone)
        end = start + timedelta(days=1) if start.year < 9999 or start.month < 12 or start.day < 31 else None
        return EventTime(start, end, "day")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return EventTime(moment, None, "instant")


def overlaps(event: EventTime, start: datetime | None, end: datetime | None) -> bool:
    """Coarse dates match overlapping intervals; output must retain their precision."""
    if event.precision == "instant":
        return (start is None or event.start >= start) and (end is None or event.start < end)
    return (start is None or event.end is None or event.end > start) and (end is None or event.start < end)


def label_event_time(value: str, *, timezone: str = "Asia/Shanghai") -> str:
    precision = parse_event_time(value, timezone=timezone).precision
    suffix = {"year": "（仅精确到年）", "month": "（仅精确到月）", "day": "（仅精确到日）"}.get(precision, "")
    return value + suffix

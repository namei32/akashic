"""WATCH.md：主 Agent 写给值班员的嘱托。一行一条，默认会过期，触发过的一次性嘱托自动归档。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)
_reported: set[str] = set()
ORDERS, NOTES = "叫醒我", "留意"
DEFAULT_DAYS = {ORDERS: 14, NOTES: 60}
_ITEM = re.compile(r"^- ((?:\[[^\]]*\]\s*)*)(.*)$")
_TAG = re.compile(r"\[([^\]]*)\]")
_UNTIL = re.compile(r"^到 (\d{4}-\d{2}-\d{2})$")
_CLOCK = re.compile(r"^(每天|工作日|周末|周[一二三四五六日]+) (\d{1,2}):(\d{2})$")
_ONCE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})$")
_WEEK = "一二三四五六日"


@dataclass(frozen=True, slots=True)
class Order:
    raw: str
    section: str
    text: str
    until: date | None
    standing: bool
    when: str | None

    @property
    def recurring(self) -> bool:
        return self.when is not None and _ONCE.fullmatch(self.when) is None


def parse(content: str) -> list[Order]:
    orders: list[Order] = []
    section = ""
    for raw in content.splitlines():
        if raw.startswith("## "):
            section = raw[3:].strip()
            continue
        match = _ITEM.fullmatch(raw.rstrip())
        if match is None or section not in DEFAULT_DAYS or not match.group(2).strip():
            continue
        until: date | None = None
        standing, when, unknown = False, None, ""
        for tag in _TAG.findall(match.group(1)):
            tag = tag.strip()
            expiry = _UNTIL.fullmatch(tag)
            if expiry is not None:
                try:
                    until = date.fromisoformat(expiry.group(1))
                except ValueError:
                    _report_bad_tag(raw, tag)
                    unknown += f"[{tag}] "
            elif tag == "长期":
                standing = True
            elif _is_time(tag):
                when = tag
            else:
                if _ONCE.fullmatch(tag) or _CLOCK.fullmatch(tag) or tag.startswith("到 "):
                    _report_bad_tag(raw, tag)
                unknown += f"[{tag}] "
        orders.append(Order(raw, section, unknown + match.group(2).strip(), until, standing, when))
    return orders


def _report_bad_tag(raw: str, tag: str) -> None:
    if raw in _reported:
        return
    _reported.add(raw)
    logger.warning("[paused] 值班嘱托时间标记无法解析，这条嘱托不会按时间触发：标记=%s 原文=%s", tag[:40], raw[:80])


def _is_time(tag: str) -> bool:
    once, clock = _ONCE.fullmatch(tag), _CLOCK.fullmatch(tag)
    try:
        if once is not None:
            _ = datetime.fromisoformat(f"{once.group(1)}T{int(once.group(2)):02d}:{once.group(3)}")
            return True
        return clock is not None and int(clock.group(2)) < 24 and int(clock.group(3)) < 60
    except ValueError:
        return False


def render(orders: list[Order]) -> tuple[str, list[Order]]:
    wake = [order for order in orders if order.section == ORDERS]
    notes = [order for order in orders if order.section == NOTES]
    lines = [f"{index}. {order.text}" + (f"（{order.when} 查看）" if order.when else "") for index, order in enumerate(wake, 1)]
    text = "什么情况叫醒主 Agent：\n" + ("\n".join(lines) or "（目前没有）")
    if notes:
        text += "\n另外请留意：\n" + "\n".join(f"- {order.text}" for order in notes)
    return text, wake


def due(order: Order, after: datetime, now: datetime) -> bool:
    if order.when is None:
        return False
    once = _ONCE.fullmatch(order.when)
    if once is not None:
        try:
            moment = datetime.fromisoformat(f"{once.group(1)}T{int(once.group(2)):02d}:{once.group(3)}").replace(tzinfo=now.tzinfo)
        except ValueError:
            return False
        return after < moment <= now
    clock = _CLOCK.fullmatch(order.when)
    assert clock is not None
    days = clock.group(1)
    day = after.date()
    while day <= now.date():
        allowed = days == "每天" or (days == "工作日" and day.weekday() < 5) or (days == "周末" and day.weekday() >= 5) \
            or (days.startswith("周") and _WEEK[day.weekday()] in days[1:])
        try:
            moment = datetime(day.year, day.month, day.day, int(clock.group(2)), int(clock.group(3)), tzinfo=now.tzinfo)
        except ValueError:
            return False
        if allowed and after < moment <= now:
            return True
        day += timedelta(days=1)
    return False

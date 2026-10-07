"""A small, dependency-free parser for five-field cron expressions.

Supports ``*``, lists, ranges, steps, month and weekday names, ``@hourly``-style
aliases and both 0 and 7 for Sunday. Computes the next fire time after a given
aware datetime in a given zone.
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
WEEKDAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
FIELDS = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day", 1, 31, {}),
    ("month", 1, 12, MONTHS),
    ("weekday", 0, 7, WEEKDAYS),
)


class CronError(ValueError):
    pass


def _value(token: str, names: dict, lo: int, hi: int, field: str) -> int:
    token = token.lower()
    if token in names:
        return names[token]
    try:
        n = int(token)
    except ValueError as exc:
        raise CronError(f"invalid {field} value {token!r}") from exc
    if not lo <= n <= hi:
        raise CronError(f"{field} value {n} out of range {lo}-{hi}")
    return n


def _parse_field(expr: str, field: str, lo: int, hi: int, names: dict) -> frozenset[int]:
    values: set[int] = set()
    for part in expr.split(","):
        if not part:
            raise CronError(f"empty element in {field} field")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = _value(step_s, {}, 1, hi, field)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names, lo, hi, field), _value(b, names, lo, hi, field)
            if start > end:
                raise CronError(f"{field} range {part!r} is reversed")
        else:
            start = _value(part, names, lo, hi, field)
            end = hi if "/" in expr and step > 1 else start
        values.update(range(start, end + 1, step))
    if field == "weekday" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    def _day_matches(self, dt: datetime) -> bool:
        day_ok = dt.day in self.days
        wd_ok = (dt.isoweekday() % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or wd_ok  # classic cron: either restricted field may match
        return day_ok and wd_ok

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._day_matches(dt)
        )

    def next_after(self, after: datetime, tz: str | None = None) -> datetime:
        """The first matching minute strictly after ``after`` (aware), evaluated in ``tz``."""
        if after.tzinfo is None:
            raise CronError("next_after() needs an aware datetime")
        zone = zoneinfo.ZoneInfo(tz) if tz else after.tzinfo
        local = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = local + timedelta(days=366 * 5)
        while local < limit:
            if local.month not in self.months:
                local = (local.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(local):
                local = (local + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if local.hour not in self.hours:
                local = (local + timedelta(hours=1)).replace(minute=0)
                continue
            if local.minute not in self.minutes:
                local += timedelta(minutes=1)
                continue
            return local.astimezone(after.tzinfo)
        raise CronError(f"{self.expression!r} never fires within five years")


def parse(expression: str) -> Cron:
    expr = ALIASES.get(expression.strip().lower(), expression.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f"expected five fields, got {len(parts)} in {expression!r}")
    parsed = [
        _parse_field(part, name, lo, hi, names)
        for part, (name, lo, hi, names) in zip(parts, FIELDS, strict=True)
    ]
    return Cron(
        expression=expression,
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=parsed[4],
        day_restricted=parts[2] != "*",
        weekday_restricted=parts[4] != "*",
    )

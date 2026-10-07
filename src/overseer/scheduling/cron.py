"""A small, dependency-free parser for five-field cron expressions.

Supports ``*``, lists, ranges, steps, month and weekday names, ``@hourly``-style
aliases and both 0 and 7 for Sunday. Computes the next fire time after a given
aware datetime in a given zone.
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
# Largest daylight-saving shift to look around (real zones shift by at most two hours).
DST_MARGIN = timedelta(hours=3)
MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
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

    def _instants(self, wall: datetime, zone) -> tuple[datetime, ...]:
        """The real instants a matching wall-clock minute stands for in ``zone``.

        Usually one. In a repeated hour (clocks going back) a wall time happens twice:
        jobs that run every hour follow real time and fire in both passes, jobs pinned to
        an hour fire once, in the first pass (vixie-cron's rule). A wall time inside a gap
        (clocks going forward) does not exist; it fires at the same offset past the gap.
        """
        # Returned in UTC: aware datetimes sharing a tzinfo compare by wall clock and ignore
        # ``fold``, which would make the two passes of a repeated hour compare as equal.
        early = wall.replace(tzinfo=zone, fold=0)
        late = wall.replace(tzinfo=zone, fold=1)
        if early.utcoffset() == late.utcoffset():
            return (early.astimezone(UTC),)
        exists = early.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall
        if exists and len(self.hours) == 24:
            return (early.astimezone(UTC), late.astimezone(UTC))
        return (early.astimezone(UTC),)

    def next_after(self, after: datetime, tz: str | None = None) -> datetime:
        """The first matching instant strictly after ``after`` (aware), evaluated in ``tz``.

        Wall-clock order and real-time order disagree around daylight-saving changes, so
        the search starts a little before ``after`` in wall-clock time, resolves every
        matching minute to real instants and keeps the earliest one after ``after``.
        """
        if after.tzinfo is None:
            raise CronError("next_after() needs an aware datetime")
        zone = zoneinfo.ZoneInfo(tz) if tz else after.tzinfo
        wall = (
            (after - DST_MARGIN)
            .astimezone(zone)
            .replace(tzinfo=None, second=0, microsecond=0, fold=0)
        )
        limit = wall + timedelta(days=366 * 5)
        best = stop = None
        while wall < limit and (stop is None or wall <= stop):
            if wall.month not in self.months:
                wall = (wall.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(wall):
                wall = (wall + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if wall.hour not in self.hours:
                wall = (wall + timedelta(hours=1)).replace(minute=0)
                continue
            if wall.minute in self.minutes:
                for instant in self._instants(wall, zone):
                    if instant > after and (best is None or instant < best):
                        best = instant
                if best is not None and stop is None:
                    stop = wall + DST_MARGIN
            wall += timedelta(minutes=1)
        if best is None:
            raise CronError(f"{self.expression!r} never fires within five years")
        return best.astimezone(after.tzinfo)


def parse(expression: str) -> Cron:
    expr = ALIASES.get(expression.strip().lower(), expression.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f"expected five fields, got {len(parts)} in {expression!r}")
    parsed = [
        _parse_field(part, name, lo, hi, names)
        for part, (name, lo, hi, names) in zip(parts, FIELDS, strict=True)
    ]
    cron = Cron(
        expression=expression,
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=parsed[4],
        # As in vixie-cron, a field starting with "*" (including "*/2") is unrestricted for
        # the "day-of-month OR day-of-week" rule.
        day_restricted=not parts[2].startswith("*"),
        weekday_restricted=not parts[4].startswith("*"),
    )
    if not cron.weekday_restricted and not any(
        day <= MONTH_DAYS[month] for day in cron.days for month in cron.months
    ):
        raise CronError(f"{expression!r} names a day that none of its months has")
    return cron

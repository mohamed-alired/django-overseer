"""Checks that a schedule can ever fire. Free of model imports, so declarations can use it."""

from __future__ import annotations

import zoneinfo

from django.conf import settings

from .. import conf
from .cron import CronError, parse


def schedule_timezone(schedule) -> str:
    """The IANA zone a schedule's cron expression is evaluated in."""
    return (
        schedule.timezone
        or conf.get_setting("OVERSEER_SCHEDULER_TIMEZONE")
        or settings.TIME_ZONE
        or "UTC"
    )


def check_schedule(schedule) -> None:
    """Raise ``ValueError`` with a readable reason when ``schedule`` can never fire.

    ``schedule`` is anything with ``cron``, ``interval_seconds`` and ``timezone``
    attributes: a ``Schedule`` row or a ``ScheduleSpec`` declaration.
    """
    if not schedule.cron and not schedule.interval_seconds:
        raise ValueError("needs a cron expression or a positive interval")
    if not schedule.cron and schedule.interval_seconds <= 0:
        raise ValueError("the interval must be a positive number of seconds")
    zone = schedule_timezone(schedule)
    try:
        zoneinfo.ZoneInfo(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {zone!r}") from None
    if schedule.cron:
        try:
            parse(schedule.cron)
        except CronError as exc:
            raise ValueError(f"invalid cron expression: {exc}") from None

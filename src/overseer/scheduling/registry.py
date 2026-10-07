"""In-code schedule declarations, synced to the ``Schedule`` table by ``sync_schedules``."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    task_path: str
    cron: str = ""
    interval_seconds: int | None = None
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)
    queue_name: str = ""
    priority: int | None = None
    backend: str = ""
    timezone: str = ""


_schedules: dict[str, ScheduleSpec] = {}


def register(**kwargs) -> ScheduleSpec:
    spec = ScheduleSpec(**kwargs)
    if spec.cron:
        from .cron import parse  # validate eagerly so a typo fails at import time

        parse(spec.cron)
    _schedules[spec.name] = spec
    return spec


def all_specs() -> dict[str, ScheduleSpec]:
    return dict(_schedules)


def clear() -> None:  # tests
    _schedules.clear()

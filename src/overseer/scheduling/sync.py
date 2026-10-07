"""Mirror in-code schedule declarations into the ``Schedule`` table."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from ..models import Schedule
from . import registry
from .scheduler import compute_next_run

logger = logging.getLogger("overseer")

SPEC_FIELDS = (
    "task_path",
    "cron",
    "interval_seconds",
    "args",
    "kwargs",
    "queue_name",
    "priority",
    "backend",
    "timezone",
)


def sync_schedules(now=None) -> dict[str, list[str]]:
    """Create, update or disable DB rows so they match ``overseer.schedule`` declarations.

    Rows created by hand (``declared_in_code=False``) are never touched. A row whose
    declaration disappeared from the code is disabled, not deleted, so its history stays.
    """
    now = now or timezone.now()
    report = {"created": [], "updated": [], "disabled": [], "unchanged": []}
    specs = registry.all_specs()
    with transaction.atomic():
        existing = {s.name: s for s in Schedule.objects.select_for_update().filter(name__in=specs)}
        for name, spec in specs.items():
            values = {f: getattr(spec, f) for f in SPEC_FIELDS}
            row = existing.get(name)
            if row is None:
                row = Schedule(name=name, declared_in_code=True, **values)
                row.next_run_at = compute_next_run(row, now)
                row.save()
                report["created"].append(name)
                continue
            changed = [f for f in SPEC_FIELDS if getattr(row, f) != values[f]]
            if not row.declared_in_code:
                changed.append("declared_in_code")
            if changed:
                for f, v in values.items():
                    setattr(row, f, v)
                row.declared_in_code = True
                if {"cron", "interval_seconds", "timezone"} & set(
                    changed
                ) or row.next_run_at is None:
                    row.next_run_at = compute_next_run(row, now)
                row.save()
                report["updated"].append(name)
            else:
                if row.next_run_at is None:
                    row.next_run_at = compute_next_run(row, now)
                    row.save(update_fields=["next_run_at", "updated_at"])
                report["unchanged"].append(name)
        stale = (
            Schedule.objects.select_for_update()
            .filter(declared_in_code=True, enabled=True)
            .exclude(name__in=specs)
        )
        for row in stale:
            row.enabled = False
            row.save(update_fields=["enabled", "updated_at"])
            report["disabled"].append(row.name)
            logger.warning("Schedule %r is no longer declared in code; disabled", row.name)
    return report

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


def sync_schedules(now=None, *, enable_all: bool = False) -> dict[str, list[str]]:
    """Create, update or disable DB rows so they match ``overseer.schedule`` declarations.

    Rows created by hand (``declared_in_code=False``) are never touched, even when a
    declaration has the same name (reported under ``conflicts``). With ``enable_all`` every
    declared row that is disabled is enabled again, whoever disabled it. A row whose declaration
    disappeared from the code is disabled, not deleted, so its history stays; it is enabled
    again when the declaration comes back (a schedule paused by a person stays paused).
    When no declarations are loaded at all, nothing is disabled: that is far more likely a
    process that did not import the task modules than every schedule being removed.
    """
    now = now or timezone.now()
    report = {
        "created": [],
        "updated": [],
        "enabled": [],
        "disabled": [],
        "unchanged": [],
        "conflicts": [],
        "errors": [],
    }
    specs = registry.all_specs()
    with transaction.atomic():
        existing = {s.name: s for s in Schedule.objects.select_for_update().filter(name__in=specs)}
        for name, spec in specs.items():
            values = {f: getattr(spec, f) for f in SPEC_FIELDS}
            row = existing.get(name)
            try:
                if row is None:
                    row = Schedule(name=name, declared_in_code=True, **values)
                    row.next_run_at = compute_next_run(row, now)
                    row.save()
                    report["created"].append(name)
                    continue
                if not row.declared_in_code:
                    logger.warning(
                        "Schedule %r exists as a hand-made row; the declaration is ignored", name
                    )
                    report["conflicts"].append(name)
                    continue
                changed = [f for f in SPEC_FIELDS if getattr(row, f) != values[f]]
                # Disabled by Overseer itself (declaration gone, or it could never fire) and
                # now declared again and valid: enable it. ``enable_all`` also covers rows
                # disabled before 0.1.1, when no marker was kept.
                revived = not row.enabled and (
                    row.missing_from_code or row.last_error.startswith("Disabled:") or enable_all
                )
                if not changed and not revived and row.next_run_at is not None:
                    report["unchanged"].append(name)
                    continue
                for f, v in values.items():
                    setattr(row, f, v)
                if revived:
                    row.enabled = True
                    row.missing_from_code = False
                if (
                    revived
                    or {"cron", "interval_seconds", "timezone"} & set(changed)
                    or row.next_run_at is None
                ):
                    row.next_run_at = compute_next_run(row, now)
                row.last_error = ""
                row.save()
                report["enabled" if revived else "updated" if changed else "unchanged"].append(name)
            except ValueError as exc:
                logger.error("Schedule %r cannot be synced: %s", name, exc)
                report["errors"].append(name)
        if not specs:
            if Schedule.objects.filter(declared_in_code=True, enabled=True).exists():
                logger.warning(
                    "No overseer.schedule declarations are loaded in this process; declared "
                    "schedules were left as they are. Check OVERSEER_AUTODISCOVER and "
                    "OVERSEER_TASK_MODULES."
                )
            return report
        stale = (
            Schedule.objects.select_for_update()
            .filter(declared_in_code=True, enabled=True)
            .exclude(name__in=specs)
        )
        for row in stale:
            row.enabled = False
            row.missing_from_code = True
            row.save(update_fields=["enabled", "missing_from_code", "updated_at"])
            report["disabled"].append(row.name)
            logger.warning("Schedule %r is no longer declared in code; disabled", row.name)
    return report

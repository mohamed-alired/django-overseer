"""Fire due schedules. Safe to run in several processes: due rows are claimed with a row lock."""

from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone
from django.utils.module_loading import import_string

from .. import conf
from ..db import atomic_for
from ..models import Job, JobSource, Schedule
from .cron import parse
from .validation import check_schedule, schedule_timezone

logger = logging.getLogger("overseer")


def compute_next_run(schedule: Schedule, after=None):
    """The next fire time strictly after ``after`` (default: now).

    Raises ``ValueError`` (``CronError`` is one) when the schedule can never fire.
    """
    after = after or timezone.now()
    check_schedule(schedule)
    # Projects with USE_TZ = False work in naive local time; cron needs aware datetimes.
    naive = timezone.is_naive(after)
    if naive:
        after = timezone.make_aware(after)
    if schedule.cron:
        result = parse(schedule.cron).next_after(after, tz=schedule_timezone(schedule))
    else:
        result = after + timedelta(seconds=schedule.interval_seconds)
    return timezone.make_naive(result) if naive else result


def disable_broken(schedule: Schedule, exc: Exception) -> None:
    """Switch off a schedule that can never fire, and say why on the row."""
    logger.error("Schedule %r disabled: %s", schedule.name, exc)
    schedule.enabled = False
    schedule.next_run_at = None
    schedule.last_error = f"Disabled: {exc}"
    schedule.save(update_fields=["enabled", "next_run_at", "last_error", "updated_at"])


def enqueue_schedule(schedule: Schedule, *, source=JobSource.SCHEDULE) -> Job:
    """Enqueue the schedule's task once and return the recorded Job."""
    from .. import recorders

    task = import_string(schedule.task_path)
    overrides = {}
    if schedule.queue_name:
        overrides["queue_name"] = schedule.queue_name
    if schedule.priority is not None:
        overrides["priority"] = schedule.priority
    if schedule.backend:
        overrides["backend"] = schedule.backend
    if overrides:
        task = task.using(**overrides)
    with recorders.enqueue_context(source=source, schedule_id=schedule.pk):
        result = task.enqueue(*schedule.args, **schedule.kwargs)
    return Job.objects.get(runs__result_id=str(result.id))


def run_now(schedule: Schedule) -> Job:
    """Manual trigger from the dashboard; does not move ``next_run_at``."""
    job = enqueue_schedule(schedule, source=JobSource.MANUAL)
    Schedule.objects.filter(pk=schedule.pk).update(
        last_run_at=timezone.now(), last_job=job, runs_count=F("runs_count") + 1
    )
    return job


def tick(now=None) -> list[Job]:
    """Fire every enabled schedule whose ``next_run_at`` has passed. Returns the Jobs enqueued.

    A schedule missed while no scheduler was running fires once, then continues from now;
    missed occurrences are not replayed. Enabled rows without a ``next_run_at`` (created by
    hand) get one. A schedule that can never fire (bad cron, unknown timezone) is disabled
    with the reason in ``last_error``; it never blocks the others.
    """
    now = now or timezone.now()
    jobs = []
    with atomic_for(Schedule):
        unscheduled = Schedule.objects.select_for_update(skip_locked=True).filter(
            enabled=True, next_run_at__isnull=True
        )
        for schedule in unscheduled:
            try:
                schedule.next_run_at = compute_next_run(schedule, now)
            except ValueError as exc:
                disable_broken(schedule, exc)
            else:
                schedule.save(update_fields=["next_run_at", "updated_at"])

        due = (
            Schedule.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, next_run_at__lte=now)
            .order_by("next_run_at")
        )
        for schedule in due:
            try:
                next_run = compute_next_run(schedule, now)
            except ValueError as exc:
                disable_broken(schedule, exc)
                continue
            job, error = None, ""
            try:
                with atomic_for(Job):  # a failed enqueue must not poison the others
                    job = enqueue_schedule(schedule)
            except Exception as exc:
                logger.exception(
                    "Schedule %r could not enqueue %s", schedule.name, schedule.task_path
                )
                error = f"{type(exc).__name__}: {exc}"
            schedule.last_run_at = now
            schedule.next_run_at = next_run
            schedule.last_error = error
            if job is not None:
                schedule.last_job = job
                schedule.runs_count += 1
                jobs.append(job)
            schedule.save(
                update_fields=[
                    "last_run_at",
                    "next_run_at",
                    "last_job",
                    "runs_count",
                    "last_error",
                    "updated_at",
                ]
            )
    return jobs


class Scheduler:
    """The loop behind ``manage.py overseer_scheduler``."""

    def __init__(self, interval: float | None = None):
        self.interval = interval or conf.get_setting("OVERSEER_SCHEDULER_INTERVAL")
        self.running = False

    def _stop(self, signum, frame):
        logger.info("Scheduler received signal %s, stopping", signum)
        self.running = False

    def _step(self, *, sync: bool, rescue_due: bool, maintenance_due: bool) -> list[Job]:
        """One iteration of the loop: sync (first time), fire due schedules, housekeeping."""
        from ..alerts import evaluate as evaluate_alerts
        from ..metrics import rollup
        from ..rescue import rescue
        from .sync import sync_schedules

        if sync:
            sync_schedules()
        jobs = tick()
        if jobs:
            logger.info("Scheduler enqueued %d job(s)", len(jobs))
        if rescue_due:
            abandoned = rescue()
            if abandoned:
                logger.warning("Scheduler abandoned %d stale run(s)", len(abandoned))
        if maintenance_due:
            rollup()
            evaluate_alerts()
        return jobs

    def run(self, *, once: bool = False, max_ticks: int | None = None) -> int:
        """Loop until a signal arrives (or ``once`` / ``max_ticks``). Returns the tick count.

        An error in an iteration (a locked SQLite file, a server restarting, a broken
        notifier) is logged and the loop carries on at the next interval; a scheduler never
        dies over one failure. With ``once`` the error propagates so the command exits
        non-zero.
        """
        self.running = True
        previous = {s: signal.signal(s, self._stop) for s in (signal.SIGINT, signal.SIGTERM)}
        ticks = 0
        synced = False
        last_rescue = last_maintenance = None
        rescue_interval = conf.get_setting("OVERSEER_RESCUE_INTERVAL")
        maintenance_interval = conf.get_setting("OVERSEER_MAINTENANCE_INTERVAL")
        try:
            while self.running:
                now = time.monotonic()
                rescue_due = last_rescue is None or now - last_rescue >= rescue_interval
                maintenance_due = (
                    last_maintenance is None or now - last_maintenance >= maintenance_interval
                )
                try:
                    self._step(
                        sync=not synced, rescue_due=rescue_due, maintenance_due=maintenance_due
                    )
                except Exception:
                    if once:
                        raise
                    logger.exception("Scheduler iteration failed; retrying in %ss", self.interval)
                    close_old_connections()
                else:
                    synced = True
                    ticks += 1
                    if rescue_due:
                        last_rescue = now
                    if maintenance_due:
                        last_maintenance = now
                if once or (max_ticks is not None and ticks >= max_ticks):
                    break
                time.sleep(self.interval)
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
            self.running = False
        return ticks

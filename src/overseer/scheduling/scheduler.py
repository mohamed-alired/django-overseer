"""Fire due schedules. Safe to run in several processes: due rows are claimed with a row lock."""

from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from .. import conf
from ..models import Job, JobSource, Schedule
from .cron import parse

logger = logging.getLogger("overseer")


def schedule_timezone(schedule: Schedule) -> str:
    return (
        schedule.timezone
        or conf.get_setting("OVERSEER_SCHEDULER_TIMEZONE")
        or settings.TIME_ZONE
        or "UTC"
    )


def compute_next_run(schedule: Schedule, after=None):
    """The next fire time strictly after ``after`` (default: now)."""
    after = after or timezone.now()
    if schedule.cron:
        return parse(schedule.cron).next_after(after, tz=schedule_timezone(schedule))
    if schedule.interval_seconds:
        return after + timedelta(seconds=schedule.interval_seconds)
    raise ValueError(f"Schedule {schedule.name!r} has neither a cron expression nor an interval")


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
        last_run_at=timezone.now(), last_job=job, runs_count=schedule.runs_count + 1
    )
    return job


def tick(now=None) -> list[Job]:
    """Fire every enabled schedule whose ``next_run_at`` has passed. Returns the Jobs enqueued.

    A schedule missed while no scheduler was running fires once, then continues from now;
    missed occurrences are not replayed.
    """
    now = now or timezone.now()
    jobs = []
    with transaction.atomic():
        due = (
            Schedule.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, next_run_at__lte=now)
            .order_by("next_run_at")
        )
        for schedule in due:
            try:
                job = enqueue_schedule(schedule)
            except Exception:
                logger.exception(
                    "Schedule %r could not enqueue %s", schedule.name, schedule.task_path
                )
                job = None
            schedule.last_run_at = now
            schedule.next_run_at = compute_next_run(schedule, now)
            if job is not None:
                schedule.last_job = job
                schedule.runs_count += 1
                jobs.append(job)
            schedule.save(
                update_fields=["last_run_at", "next_run_at", "last_job", "runs_count", "updated_at"]
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

    def run(self, *, once: bool = False, max_ticks: int | None = None) -> int:
        from ..rescue import rescue
        from .sync import sync_schedules

        sync_schedules()
        self.running = True
        previous = {s: signal.signal(s, self._stop) for s in (signal.SIGINT, signal.SIGTERM)}
        ticks = 0
        last_rescue = None
        rescue_interval = conf.get_setting("OVERSEER_RESCUE_INTERVAL")
        try:
            while self.running:
                jobs = tick()
                ticks += 1
                if jobs:
                    logger.info("Scheduler enqueued %d job(s)", len(jobs))
                if last_rescue is None or time.monotonic() - last_rescue >= rescue_interval:
                    abandoned = rescue()
                    last_rescue = time.monotonic()
                    if abandoned:
                        logger.warning("Scheduler abandoned %d stale run(s)", len(abandoned))
                if once or (max_ticks is not None and ticks >= max_ticks):
                    break
                time.sleep(self.interval)
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
            self.running = False
        return ticks

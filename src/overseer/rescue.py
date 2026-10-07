"""Find attempts that are stuck in RUNNING and treat them as failures."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import conf, registry, retry
from .adapters.base import get_adapter
from .exceptions import AdapterUnsupported
from .models import Job, Run, RunStatus

logger = logging.getLogger("overseer")


def deadline_for(run: Run):
    """When ``run`` should have finished: policy timeout, else ``OVERSEER_STALE_AFTER``."""
    if run.started_at is None:
        return None
    policy = registry.get_policy(run.job.task_path)
    seconds = policy.timeout or conf.get_setting("OVERSEER_STALE_AFTER")
    return run.started_at + timedelta(seconds=seconds)


def stale_runs(now=None):
    now = now or timezone.now()
    for run in Run.objects.select_related("job").filter(status=RunStatus.RUNNING):
        deadline = deadline_for(run)
        if deadline is not None and deadline <= now:
            yield run


def abandon(run: Run, now=None) -> Run:
    """Mark ``run`` abandoned, reset it in the backend if possible, then apply the retry policy."""
    now = now or timezone.now()
    with transaction.atomic():
        run = Run.objects.select_for_update().select_related("job").get(pk=run.pk)
        if run.status != RunStatus.RUNNING:
            return run
        run.status = RunStatus.ABANDONED
        run.finished_at = now
        run.duration_ms = (
            int((now - run.started_at).total_seconds() * 1000) if run.started_at else None
        )
        run.exception_class = "overseer.exceptions.RunAbandoned"
        run.traceback = (
            "Marked abandoned by Overseer: the attempt exceeded its timeout or its worker "
            "disappeared."
        )
        run.save()
        job = run.job
        Job.objects.filter(pk=job.pk).update(attempts=run.attempt)
        job.attempts = run.attempt
    try:
        get_adapter(run.backend).reset(run)
    except AdapterUnsupported:
        logger.warning("Backend %r cannot reset abandoned run %s", run.backend, run.result_id)
    except Exception:  # pragma: no cover
        logger.exception("Resetting abandoned run %s failed", run.result_id)
    logger.warning("Run %s (%s) abandoned after timeout", run.result_id, job.task_path)
    retry.handle_failure(job, run)
    return run


def rescue(now=None) -> list[Run]:
    """Abandon every stale run. Called by the scheduler loop and ``overseer_rescue``."""
    now = now or timezone.now()
    return [abandon(run, now) for run in list(stale_runs(now))]

"""Find attempts that are stuck in RUNNING and treat them as failures."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.tasks import task_backends
from django.utils import timezone

from . import conf, registry, retry
from .adapters.base import get_adapter
from .db import atomic_for
from .exceptions import AdapterUnsupported
from .models import Job, JobStatus, Run, RunStatus

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
    with atomic_for(Run):
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
    """Abandon every stale run and reconcile waiting ones with the backend.

    Called by the scheduler loop and ``overseer_rescue``.
    """
    now = now or timezone.now()
    abandoned = [abandon(run, now) for run in list(stale_runs(now))]
    reconcile_waiting(now)
    return abandoned


# A run must have waited at least this long before the backend is asked about it, so that
# a result row being created in another transaction is not mistaken for a missing one.
RECONCILE_GRACE = timedelta(seconds=60)


RECONCILE_BATCH = 500  # waiting runs looked up per pass, oldest first


def reconcile_waiting(now=None) -> dict[str, int]:
    """Bring READY runs back in line with what their backend knows.

    A run stays READY in Overseer only because a signal never came: the backend row was
    deleted outside Overseer (restored database, admin, another tool), or the task finished
    while the signal was not delivered. Backends that can look results up are asked, in
    one query per backend through the adapter: a result that finished or started is
    recorded as such, and a result the backend no longer has marks the run lost
    (cancelled) so that health, alerts and unique keys move on. Backends without result
    lookup are left alone. At most ``RECONCILE_BATCH`` runs are examined per pass.
    """
    from django.tasks import TaskResultStatus

    from . import recorders
    from .adapters.base import get_adapter

    now = now or timezone.now()
    report = {"lost": 0, "recorded": 0}
    waiting = list(
        Run.objects.select_related("job")
        .filter(status=RunStatus.READY, enqueued_at__lt=now - RECONCILE_GRACE)
        .order_by("enqueued_at")[:RECONCILE_BATCH]
    )
    by_backend: dict[str, list[Run]] = {}
    for run in waiting:
        by_backend.setdefault(run.backend, []).append(run)
    for alias, runs in by_backend.items():
        try:
            backend = task_backends[alias]
            if not backend.supports_get_result:
                continue
            statuses = get_adapter(alias).result_statuses([r.result_id for r in runs])
        except Exception:  # noqa: BLE001 - alias removed from TASKS, backend down
            logger.exception("Could not look up waiting runs on backend %r", alias)
            continue
        for run in runs:
            status = statuses.get(run.result_id)
            if status is None:
                mark_lost(run, now)
                report["lost"] += 1
            elif status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED):
                recorders.record_finished(backend.get_result(run.result_id), local=False)
                report["recorded"] += 1
            elif status == TaskResultStatus.RUNNING:
                recorders.record_started(backend.get_result(run.result_id), local=False)
                report["recorded"] += 1
    if report["lost"] or report["recorded"]:
        logger.warning(
            "Reconciled waiting runs: %d lost, %d recorded from the backend",
            report["lost"],
            report["recorded"],
        )
    return report


def mark_lost(run: Run, now=None) -> Run:
    """The backend no longer has this waiting task: cancel the run, and the job with it."""
    now = now or timezone.now()
    with atomic_for(Run):
        run = Run.objects.select_for_update().select_related("job").get(pk=run.pk)
        if run.status != RunStatus.READY:
            return run
        run.status = RunStatus.CANCELLED
        run.finished_at = now
        run.exception_class = "overseer.exceptions.RunLost"
        run.traceback = (
            "Marked lost by Overseer: the task backend no longer has this task, so it will "
            "never run. Retry the job to enqueue it again."
        )
        run.save()
        Job.objects.filter(pk=run.job_id).update(
            status=JobStatus.CANCELLED, finished_at=now, next_retry_at=None
        )
    logger.warning("Run %s (%s) lost: backend has no such task", run.result_id, run.job.task_path)
    return run

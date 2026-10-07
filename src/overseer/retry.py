"""Retry decisions and re-enqueueing."""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.tasks import task_backends
from django.utils import timezone
from django.utils.module_loading import import_string

from . import registry
from .models import Job, JobSource, JobStatus, Run, RunStatus

logger = logging.getLogger("overseer")


def compute_delay(policy, attempt: int) -> float:
    """Seconds to wait before attempt number ``attempt`` (2 for the first retry)."""
    n = max(attempt - 1, 1)
    if policy.backoff == "exponential":
        delay = policy.backoff_base * (2 ** (n - 1))
    elif policy.backoff == "linear":
        delay = policy.backoff_base * n
    else:
        delay = policy.backoff_base
    delay = min(delay, policy.backoff_max)
    if policy.jitter:
        delay *= random.uniform(0.8, 1.2)
    return float(delay)


def _exception_class(run: Run):
    if not run.exception_class:
        return Exception
    try:
        cls = import_string(run.exception_class)
    except ImportError:
        return Exception
    return cls if isinstance(cls, type) else Exception


def should_retry(job: Job, run: Run) -> bool:
    policy = registry.get_policy(job.task_path)
    if run.attempt > policy.retries:
        return False
    return issubclass(_exception_class(run), policy.retry_on)


def enqueue_retry(job: Job, previous: Run | None, *, delay: float = 0.0, source=JobSource.RETRY):
    """Enqueue ``job``'s task again as the next attempt and return the new ``Run``."""
    from . import recorders

    attempt = (previous.attempt if previous is not None else job.attempts) + 1
    task = job.get_task()
    backend = task_backends[task.backend]
    run_after = None
    if delay > 0:
        if backend.supports_defer:
            run_after = timezone.now() + timedelta(seconds=delay)
        else:
            logger.warning(
                "Backend %r cannot defer; retrying %s immediately instead of in %.0fs",
                task.backend,
                job.task_path,
                delay,
            )
    if run_after is not None:
        task = task.using(run_after=run_after)
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.PENDING, next_retry_at=run_after, source=source, finished_at=None
    )
    try:
        with recorders.enqueue_context(
            job=job, retry_of=previous, attempt=attempt, source=source, unique_key=job.unique_key
        ):
            result = task.enqueue(*job.args, **job.kwargs)
    except Exception:
        Job.objects.filter(pk=job.pk).update(
            status=JobStatus.FAILED, next_retry_at=None, finished_at=timezone.now()
        )
        raise
    return Run.objects.get(result_id=str(result.id))


def handle_failure(job: Job, run: Run) -> Run | None:
    """Called when ``run`` failed: schedule the next attempt or mark the job failed."""
    if should_retry(job, run):
        policy = registry.get_policy(job.task_path)
        delay = compute_delay(policy, run.attempt + 1)
        try:
            return enqueue_retry(job, run, delay=delay)
        except Exception:
            logger.exception("Could not enqueue retry for job %s", job.pk)
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.FAILED,
        finished_at=run.finished_at or timezone.now(),
        next_retry_at=None,
    )
    return None


def retry_job(job: Job) -> Run:
    """Manual retry from the dashboard: a fresh attempt regardless of the policy."""
    previous = job.runs.order_by("-attempt").first()
    if previous is not None and previous.status in {RunStatus.READY, RunStatus.RUNNING}:
        raise ValueError("Job already has an active run")
    return enqueue_retry(job, previous, source=JobSource.MANUAL)

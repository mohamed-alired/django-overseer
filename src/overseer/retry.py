"""Retry decisions and re-enqueueing."""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.tasks import Task, task_backends
from django.utils import timezone
from django.utils.module_loading import import_string

from . import conf, registry, signals
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


class RetryUnavailable(ValueError):
    """The job cannot be enqueued again (its arguments are gone, or its key is taken)."""


def task_arguments(job: Job, previous: Run | None) -> tuple[list, dict]:
    """The arguments to call the task with again.

    The backend's own copy is preferred: it is exact (``Job.args`` went through a JSON
    round trip) and it exists even when ``OVERSEER_RECORD_ARGS`` is off. Without it the
    recorded arguments are used; when neither exists the job cannot be retried.
    """
    if previous is not None:
        try:
            backend = task_backends[previous.backend]
            result = backend.get_result(previous.result_id) if backend.supports_get_result else None
        except Exception:  # noqa: BLE001 - unknown alias, pruned row, forgetful backend
            result = None
        if result is not None:
            return list(result.args), dict(result.kwargs)
    if conf.get_setting("OVERSEER_RECORD_ARGS") or job.args or job.kwargs:
        return list(job.args), dict(job.kwargs)
    raise RetryUnavailable(
        "This job's arguments were not recorded (OVERSEER_RECORD_ARGS is off) and the "
        "backend no longer has them, so it cannot be retried."
    )


def enqueue_retry(
    job: Job, previous: Run | None, *, delay: float = 0.0, source=JobSource.RETRY
) -> Run | None:
    """Enqueue ``job``'s task again as the next attempt and return the new ``Run``."""
    from . import recorders

    attempt = (previous.attempt if previous is not None else job.attempts) + 1
    args, kwargs = task_arguments(job, previous)
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
    try:
        with transaction.atomic():
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.PENDING, next_retry_at=run_after, source=source, finished_at=None
            )
    except IntegrityError:
        # Another active job holds this job's unique key.
        raise RetryUnavailable(
            f"Another active job already holds the unique key {job.unique_key!r}."
        ) from None
    try:
        with recorders.enqueue_context(
            job=job, retry_of=previous, attempt=attempt, source=source, unique_key=job.unique_key
        ) as ctx:
            # ``Task.enqueue`` itself, never ``OverseerTask.enqueue``: this job already holds
            # its unique key, and the uniqueness check would hand back the failed attempt.
            result = Task.enqueue(task, *args, **kwargs)
            run = Run.objects.filter(result_id=str(result.id)).first()
            if run is None:
                # The recorder failed (and logged why); record the attempt here instead.
                ctx.consumed = False
                run = recorders.record_enqueued(result)
    except Exception:
        Job.objects.filter(pk=job.pk).update(
            status=JobStatus.FAILED, next_retry_at=None, finished_at=timezone.now()
        )
        raise
    return run


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
    job.status = JobStatus.FAILED
    signals.job_failed.send(sender=Job, job=job, run=run)
    return None


def retry_job(job: Job) -> Run:
    """Manual retry from the dashboard: a fresh attempt regardless of the policy.

    Only failed or cancelled jobs can be retried. Raises ``ValueError`` (with a message for
    the user) when the job is not retryable or cannot be enqueued again.
    """
    with transaction.atomic():
        # Lock the job so two dashboards clicking "retry" at once enqueue a single attempt.
        job = Job.objects.select_for_update().get(pk=job.pk)
        previous = job.runs.order_by("-attempt").first()
        if previous is not None and previous.status in {RunStatus.READY, RunStatus.RUNNING}:
            raise ValueError("Job already has an active run")
        if not job.can_retry:
            raise ValueError(
                f"Only failed or cancelled jobs can be retried (this one is {job.status.lower()})."
            )
        try:
            job.get_task()
        except ImportError as exc:
            raise ValueError(f"The task {job.task_path!r} no longer exists: {exc}") from None
        return enqueue_retry(job, previous, source=JobSource.MANUAL)

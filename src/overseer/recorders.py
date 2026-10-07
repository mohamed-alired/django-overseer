"""Turn the three ``django.tasks`` signals into Job, Run and Worker rows.

Recorders must never raise: a bug here must not break the worker or the enqueue.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import socket
from dataclasses import dataclass

from django.db import transaction
from django.tasks import TaskResultStatus
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone

from . import conf, registry
from .models import Job, JobSource, JobStatus, Run, RunStatus, Worker

logger = logging.getLogger("overseer")


@dataclass
class EnqueueContext:
    """What the enqueuing code knows that the signal does not."""

    job: Job | None = None  # retry of an existing job
    retry_of: Run | None = None
    attempt: int = 1
    source: str = JobSource.ENQUEUE
    schedule_id: int | None = None
    unique_key: str = ""


_context: contextvars.ContextVar[EnqueueContext | None] = contextvars.ContextVar(
    "overseer_enqueue_context", default=None
)


@contextlib.contextmanager
def enqueue_context(**kwargs):
    current = _context.get()
    merged = EnqueueContext(**{**(vars(current) if current else {}), **kwargs})
    token = _context.set(merged)
    try:
        yield merged
    finally:
        _context.reset(token)


def _safe(fn):
    def handler(sender, task_result, **kwargs):
        try:
            fn(task_result)
        except Exception:  # pragma: no cover - defensive; tested via a forced failure
            logger.exception(
                "overseer recorder %s failed for result %s", fn.__name__, task_result.id
            )

    handler.__name__ = f"overseer_{fn.__name__}"
    return handler


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def _ms(start, end):
    if start is None or end is None:
        return None
    return max(int((end - start).total_seconds() * 1000), 0)


def record_enqueued(task_result):
    existing = Run.objects.filter(result_id=str(task_result.id)).first()
    if existing is not None:
        return existing
    ctx = _context.get() or EnqueueContext()
    task = task_result.task
    policy = registry.get_policy(task.module_path)
    record_args = conf.get_setting("OVERSEER_RECORD_ARGS")
    with transaction.atomic():
        job = ctx.job
        if job is None:
            job = Job.objects.create(
                task_path=task.module_path,
                task_name=task.name,
                backend=task_result.backend,
                queue_name=task.queue_name,
                priority=task.priority,
                args=_json_safe(list(task_result.args)) if record_args else [],
                kwargs=_json_safe(dict(task_result.kwargs)) if record_args else {},
                status=JobStatus.PENDING,
                source=ctx.source,
                max_retries=policy.retries,
                unique_key=ctx.unique_key,
                tags=list(policy.tags),
                schedule_id=ctx.schedule_id,
            )
        run, created = Run.objects.get_or_create(
            result_id=str(task_result.id),
            defaults={
                "job": job,
                "backend": task_result.backend,
                "attempt": ctx.attempt,
                "status": RunStatus.READY,
                "enqueued_at": task_result.enqueued_at or timezone.now(),
                "run_after": task.run_after,
                "retry_of": ctx.retry_of,
            },
        )
    return run


def _get_run(task_result):
    try:
        return Run.objects.select_related("job").get(result_id=str(task_result.id))
    except Run.DoesNotExist:
        # Enqueued before Overseer was installed, or by a backend that skipped the signal.
        return record_enqueued(task_result)


def _touch_worker(task_result, run, *, finished=False, failed=False):
    worker_id = task_result.worker_ids[-1] if task_result.worker_ids else ""
    if not worker_id:
        return
    now = timezone.now()
    worker, _ = Worker.objects.get_or_create(
        worker_id=worker_id,
        defaults={"hostname": socket.gethostname(), "backend": task_result.backend},
    )
    worker.last_seen_at = now
    worker.stopped_at = None
    if finished:
        worker.current_run = None
        worker.tasks_processed += 1
        if failed:
            worker.tasks_failed += 1
    else:
        worker.current_run = run
    worker.save()


def record_started(task_result):
    run = _get_run(task_result)
    if run.is_finished:
        return run
    now = timezone.now()
    started_at = task_result.started_at or now
    run.status = RunStatus.RUNNING
    run.started_at = started_at
    run.worker_id = task_result.worker_ids[-1] if task_result.worker_ids else ""
    ready_at = max(filter(None, [run.enqueued_at, run.run_after]))
    run.wait_ms = _ms(ready_at, started_at)
    run.save(update_fields=["status", "started_at", "worker_id", "wait_ms"])
    Job.objects.filter(pk=run.job_id).update(status=JobStatus.RUNNING)
    _touch_worker(task_result, run)
    return run


def record_finished(task_result):
    run = _get_run(task_result)
    if run.is_finished:
        return run
    now = timezone.now()
    finished_at = task_result.finished_at or now
    succeeded = task_result.status == TaskResultStatus.SUCCESSFUL
    run.status = RunStatus.SUCCESSFUL if succeeded else RunStatus.FAILED
    run.finished_at = finished_at
    if run.started_at is None:
        run.started_at = task_result.started_at or finished_at
    if task_result.worker_ids and not run.worker_id:
        run.worker_id = task_result.worker_ids[-1]
    run.duration_ms = _ms(run.started_at, finished_at)
    if succeeded:
        try:
            run.return_value = _json_safe(task_result.return_value)
        except Exception:  # pragma: no cover
            run.return_value = None
    elif task_result.errors:
        error = task_result.errors[-1]
        run.exception_class = error.exception_class_path[:255]
        run.traceback = error.traceback[-conf.get_setting("OVERSEER_MAX_TRACEBACK_CHARS") :]
    run.save()

    job = run.job
    job.attempts = run.attempt
    job.save(update_fields=["attempts", "updated_at"])
    if succeeded:
        job.status = JobStatus.SUCCEEDED
        job.finished_at = finished_at
        job.next_retry_at = None
        job.save(update_fields=["status", "attempts", "finished_at", "next_retry_at", "updated_at"])
    else:
        from . import retry

        retry.handle_failure(job, run)
    _touch_worker(task_result, run, finished=True, failed=not succeeded)
    return run


on_enqueued = _safe(record_enqueued)
on_started = _safe(record_started)
on_finished = _safe(record_finished)

_connected = False


def connect():
    global _connected
    if _connected:
        return
    task_enqueued.connect(on_enqueued, dispatch_uid="overseer.enqueued")
    task_started.connect(on_started, dispatch_uid="overseer.started")
    task_finished.connect(on_finished, dispatch_uid="overseer.finished")
    _connected = True


def disconnect():  # tests
    global _connected
    task_enqueued.disconnect(dispatch_uid="overseer.enqueued")
    task_started.disconnect(dispatch_uid="overseer.started")
    task_finished.disconnect(dispatch_uid="overseer.finished")
    _connected = False

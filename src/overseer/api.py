"""JSON twins of the dashboard pages, for your own tooling and alerting."""

from __future__ import annotations

import uuid
from functools import wraps

from django.core.paginator import Paginator
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from . import conf, stats
from .models import Alert, Job, Schedule
from .views import WINDOWS, user_can_view


def _bearer_token_ok(request) -> bool:
    token = conf.get_setting("OVERSEER_HEALTH_TOKEN")
    header = request.headers.get("Authorization", "")
    return (
        bool(token)
        and header.startswith("Bearer ")
        and constant_time_compare(header.removeprefix("Bearer ").strip(), token)
    )


def access_required(view=None, *, allow_token=False):
    """Staff with the dashboard permission; ``allow_token`` also accepts the health token."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not user_can_view(request.user) and not (allow_token and _bearer_token_ok(request)):
                status = 401 if not request.user.is_authenticated else 403
                return JsonResponse({"detail": "Overseer access required."}, status=status)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator(view) if view is not None else decorator


def _int_param(request, name, default, lo, hi):
    """An integer query parameter clamped to [lo, hi]; ``default`` when absent or invalid."""
    try:
        value = int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, lo), hi)


def _minutes(request, default):
    try:
        minutes = int(request.GET.get("minutes", default))
    except ValueError:
        return default
    return minutes if minutes in WINDOWS else default


def _run(run):
    return {
        "id": run.pk,
        "result_id": run.result_id,
        "attempt": run.attempt,
        "status": run.status,
        "backend": run.backend,
        "enqueued_at": run.enqueued_at,
        "run_after": run.run_after,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "worker_id": run.worker_id,
        "duration_ms": run.duration_ms,
        "wait_ms": run.wait_ms,
        "exception_class": run.exception_class,
        "traceback": run.traceback,
        "return_value": run.return_value,
        "retry_of": run.retry_of_id,
    }


def _job(job, runs=None):
    data = {
        "id": str(job.pk),
        "task_path": job.task_path,
        "task_name": job.task_name,
        "backend": job.backend,
        "queue_name": job.queue_name,
        "priority": job.priority,
        "args": job.args,
        "kwargs": job.kwargs,
        "status": job.status,
        "source": job.source,
        "attempts": job.attempts,
        "max_retries": job.max_retries,
        "next_retry_at": job.next_retry_at,
        "unique_key": job.unique_key,
        "tags": job.tags,
        "schedule": job.schedule_id,
        "dismissed": job.dismissed,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
    if runs is not None:
        data["runs"] = [_run(r) for r in runs]
    return data


@access_required
def overview(request):
    minutes = _minutes(request, 60)
    return JsonResponse({"generated_at": timezone.now(), **stats.overview(minutes)})


@access_required
def queues(request):
    return JsonResponse({"queues": stats.queues(_minutes(request, 60))})


@access_required
def tasks(request):
    return JsonResponse({"tasks": stats.tasks(_minutes(request, 1440))})


@access_required
def jobs(request):
    g = request.GET
    qs = stats.job_queryset(
        status=g.get("status") or None,
        queue_name=g.get("queue") or None,
        task_path=g.get("task") or None,
        worker_id=g.get("worker") or None,
        search=g.get("q") or None,
    )
    per_page = _int_param(request, "per_page", 50, 1, 200)
    page = Paginator(qs, per_page).get_page(g.get("page"))
    return JsonResponse(
        {
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "per_page": per_page,
            "results": [_job(j) for j in page.object_list],
        }
    )


@access_required
def job(request, pk):
    try:
        obj = Job.objects.get(pk=uuid.UUID(str(pk)))
    except (Job.DoesNotExist, ValueError):
        return JsonResponse({"detail": "No such job."}, status=404)
    return JsonResponse(_job(obj, stats.run_chain(obj)))


@access_required
def workers(request):
    rows = stats.workers()
    for row in rows:
        run = row.pop("current_run")
        row["current_run"] = _run(run) if run else None
    return JsonResponse({"workers": rows})


@access_required
def schedules(request):
    return JsonResponse(
        {
            "schedules": [
                {
                    "id": s.pk,
                    "name": s.name,
                    "task_path": s.task_path,
                    "cron": s.cron,
                    "interval_seconds": s.interval_seconds,
                    "args": s.args,
                    "kwargs": s.kwargs,
                    "queue_name": s.queue_name,
                    "enabled": s.enabled,
                    "declared_in_code": s.declared_in_code,
                    "timezone": s.timezone,
                    "next_run_at": s.next_run_at,
                    "last_run_at": s.last_run_at,
                    "last_job": str(s.last_job_id) if s.last_job_id else None,
                    "runs_count": s.runs_count,
                    "last_error": s.last_error,
                    "missing_from_code": s.missing_from_code,
                }
                for s in Schedule.objects.all()
            ]
        }
    )


@access_required
def metrics(request):
    minutes = _minutes(request, 60)
    queue = request.GET.get("queue") or None
    return JsonResponse(
        {"minutes": minutes, "queue": queue, "points": stats.timeseries(minutes, queue_name=queue)}
    )


@access_required
def alerts(request):
    def row(a):
        return {
            "id": a.pk,
            "kind": a.kind,
            "key": a.key,
            "message": a.message,
            "value": a.value,
            "threshold": a.threshold,
            "created_at": a.created_at,
            "resolved_at": a.resolved_at,
        }

    return JsonResponse(
        {
            "open": [row(a) for a in Alert.objects.filter(resolved_at__isnull=True)],
            "resolved": [row(a) for a in Alert.objects.filter(resolved_at__isnull=False)[:50]],
        }
    )


@access_required(allow_token=True)
def health(request):
    """Liveness for load balancers and uptime checks: 503 when tasks are not being picked up.

    "Not picked up" means some ready task has waited longer than
    ``OVERSEER_ALERT_WAIT_SECONDS``. That works for every worker kind, including a plain
    ``db_worker`` that sends no heartbeats. Besides a staff session, the endpoint accepts
    ``Authorization: Bearer <OVERSEER_HEALTH_TOKEN>`` when that setting is set.
    """
    data = stats.overview(_minutes(request, 15))
    max_wait = conf.get_setting("OVERSEER_ALERT_WAIT_SECONDS")
    ok = data["oldest_wait_seconds"] <= max_wait
    payload = {
        "ok": ok,
        "waiting": data["waiting"],
        "oldest_wait_seconds": data["oldest_wait_seconds"],
        "max_wait_seconds": max_wait,
        "running": data["running"],
        "workers_online": data["workers_online"],
        "workers_without_heartbeat": data["workers_without_heartbeat"],
        "failed_jobs_open": data["failed_jobs_open"],
        "open_alerts": data["open_alerts"],
    }
    return JsonResponse(payload, status=200 if ok else 503)

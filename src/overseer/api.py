"""JSON twins of the dashboard pages, for your own tooling and alerting."""

from __future__ import annotations

from functools import wraps

from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.utils import timezone

from . import conf, stats
from .models import Alert, Job, Schedule
from .views import WINDOWS, user_can_view


def access_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not user_can_view(request.user):
            status = 401 if not request.user.is_authenticated else 403
            return JsonResponse({"detail": "Overseer access required."}, status=status)
        return view(request, *args, **kwargs)

    return wrapper


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
    page = Paginator(qs, min(int(g.get("per_page", 50) or 50), 200)).get_page(g.get("page"))
    return JsonResponse(
        {
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "results": [_job(j) for j in page.object_list],
        }
    )


@access_required
def job(request, pk):
    try:
        obj = Job.objects.get(pk=pk)
    except (Job.DoesNotExist, ValueError) as exc:
        raise Http404 from exc
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
                    "next_run_at": s.next_run_at,
                    "last_run_at": s.last_run_at,
                    "last_job": str(s.last_job_id) if s.last_job_id else None,
                    "runs_count": s.runs_count,
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


@access_required
def health(request):
    """A small liveness payload: workers online, waiting tasks, open alerts."""
    data = stats.overview(_minutes(request, 15))
    ok = data["workers_online"] > 0 or data["waiting"] == 0
    payload = {
        "ok": ok,
        "workers_online": data["workers_online"],
        "waiting": data["waiting"],
        "running": data["running"],
        "failed_jobs_open": data["failed_jobs_open"],
        "open_alerts": data["open_alerts"],
        "offline_after": conf.get_setting("OVERSEER_WORKER_OFFLINE_AFTER"),
    }
    return JsonResponse(payload, status=200 if ok else 503)

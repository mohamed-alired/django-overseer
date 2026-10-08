"""Read-side queries shared by the HTML dashboard and the JSON API."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, Max, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import metrics
from .adapters.base import get_adapter
from .models import Alert, Job, JobStatus, Run, RunStatus, Schedule, Worker

ACTIVE_RUN = (RunStatus.READY, RunStatus.RUNNING)
BAD_RUN = (RunStatus.FAILED, RunStatus.ABANDONED)


def window_bounds(minutes: int, now=None):
    now = now or timezone.now()
    return now - timedelta(minutes=minutes), now


def distinct_values(field: str) -> list:
    """Sorted distinct values of a Job column (without Meta.ordering, which would make
    DISTINCT span ``created_at`` too and return one row per job)."""
    return sorted(Job.objects.order_by().values_list(field, flat=True).distinct())


def worker_counts(now=None) -> dict:
    """Running worker processes: heartbeat workers online, and workers without heartbeats.

    Workers without heartbeats (a plain ``db_worker``) are only seen when they run a task,
    so whether they are alive cannot be known; they are counted apart.
    """
    now = now or timezone.now()
    active = [w for w in Worker.objects.filter(stopped_at__isnull=True) if not w.is_silent(now)]
    online = sum(1 for w in active if w.is_online(now))
    return {
        "workers_online": online,
        "workers_total": len(active),
        "workers_without_heartbeat": sum(1 for w in active if not w.has_heartbeat),
    }


def oldest_wait_seconds(now=None) -> float:
    """How long the longest-waiting ready run has been waiting, in seconds (0 if none)."""
    from .alerts import oldest_waiting

    now = now or timezone.now()
    oldest = min((row["ready_since"] for row in oldest_waiting(now)), default=None)
    return max((now - oldest).total_seconds(), 0.0) if oldest else 0.0


def overview(minutes: int = 60, now=None) -> dict:
    since, now = window_bounds(minutes, now)
    # Cancelled runs never executed: they are not "processed" and not part of failure rates.
    finished = Run.objects.filter(finished_at__gte=since).exclude(status=RunStatus.CANCELLED)
    counts = finished.aggregate(
        total=Count("id"),
        succeeded=Count("id", filter=Q(status=RunStatus.SUCCESSFUL)),
        failed=Count("id", filter=Q(status__in=BAD_RUN)),
        runtime_avg=Avg("duration_ms"),
        runtime_max=Max("duration_ms"),
    )
    waiting = Run.objects.filter(status=RunStatus.READY).filter(
        Q(run_after__isnull=True) | Q(run_after__lte=now)
    )
    scheduled = Run.objects.filter(status=RunStatus.READY, run_after__gt=now)
    total = counts["total"] or 0
    return {
        "window_minutes": minutes,
        "throughput_per_min": round(total / minutes, 2) if minutes else 0,
        "processed": total,
        "succeeded": counts["succeeded"] or 0,
        "failed": counts["failed"] or 0,
        "failure_rate": (counts["failed"] or 0) / total if total else 0.0,
        "runtime_avg_ms": int(counts["runtime_avg"] or 0),
        "runtime_max_ms": counts["runtime_max"] or 0,
        "waiting": waiting.count(),
        "oldest_wait_seconds": round(oldest_wait_seconds(now), 1),
        "scheduled": scheduled.count(),
        "running": Run.objects.filter(status=RunStatus.RUNNING).count(),
        "failed_jobs_open": Job.objects.filter(status=JobStatus.FAILED, dismissed=False).count(),
        **worker_counts(now),
        "open_alerts": Alert.objects.filter(resolved_at__isnull=True).count(),
        "schedules_enabled": Schedule.objects.filter(enabled=True).count(),
    }


def queues(minutes: int = 60, now=None) -> list[dict]:
    since, now = window_bounds(minutes, now)
    names = set(distinct_values("queue_name"))
    names |= set(Schedule.objects.exclude(queue_name="").values_list("queue_name", flat=True))
    rows = {
        name: {
            "queue_name": name,
            "waiting": 0,
            "running": 0,
            "scheduled": 0,
            "processed": 0,
            "failed": 0,
            "wait_avg_ms": 0,
            "runtime_avg_ms": 0,
            "backend_depth": None,
        }
        for name in names
    }
    active = (
        Run.objects.filter(status__in=ACTIVE_RUN)
        .values("job__queue_name")
        .annotate(
            running=Count("id", filter=Q(status=RunStatus.RUNNING)),
            scheduled=Count("id", filter=Q(status=RunStatus.READY, run_after__gt=now)),
            waiting=Count(
                "id",
                filter=Q(status=RunStatus.READY)
                & (Q(run_after__isnull=True) | Q(run_after__lte=now)),
            ),
        )
    )
    for row in active:
        r = rows.setdefault(row["job__queue_name"], {"queue_name": row["job__queue_name"]})
        r.update(waiting=row["waiting"], running=row["running"], scheduled=row["scheduled"])
    recent = (
        Run.objects.filter(finished_at__gte=since)
        .exclude(status=RunStatus.CANCELLED)
        .values("job__queue_name")
        .annotate(
            processed=Count("id"),
            failed=Count("id", filter=Q(status__in=BAD_RUN)),
            wait_avg=Avg("wait_ms"),
            runtime_avg=Avg("duration_ms"),
        )
    )
    for row in recent:
        r = rows.setdefault(row["job__queue_name"], {"queue_name": row["job__queue_name"]})
        r.update(
            processed=row["processed"],
            failed=row["failed"],
            wait_avg_ms=int(row["wait_avg"] or 0),
            runtime_avg_ms=int(row["runtime_avg"] or 0),
        )
    backends = set(distinct_values("backend")) or {"default"}
    for alias in backends:
        try:
            depths = get_adapter(alias).queue_depths(list(rows))
        except Exception:
            continue
        for name, depth in depths.items():
            if depth is not None and name in rows:
                rows[name]["backend_depth"] = (rows[name].get("backend_depth") or 0) + depth
    return sorted(rows.values(), key=lambda r: r["queue_name"])


def tasks(minutes: int = 60 * 24, now=None) -> list[dict]:
    since, now = window_bounds(minutes, now)
    rows = (
        Run.objects.filter(finished_at__gte=since)
        .exclude(status=RunStatus.CANCELLED)
        .values("job__task_path", "job__task_name")
        .annotate(
            processed=Count("id"),
            succeeded=Count("id", filter=Q(status=RunStatus.SUCCESSFUL)),
            failed=Count("id", filter=Q(status__in=BAD_RUN)),
            runtime_avg=Avg("duration_ms"),
            runtime_max=Max("duration_ms"),
            last_run=Max("finished_at"),
        )
        .order_by("-processed")
    )
    out = []
    for r in rows:
        total = r["processed"]
        out.append(
            {
                "task_path": r["job__task_path"],
                "task_name": r["job__task_name"],
                "processed": total,
                "succeeded": r["succeeded"],
                "failed": r["failed"],
                "failure_rate": r["failed"] / total if total else 0.0,
                "runtime_avg_ms": int(r["runtime_avg"] or 0),
                "runtime_max_ms": r["runtime_max"] or 0,
                "last_run": r["last_run"],
            }
        )
    return out


def workers(now=None) -> list[dict]:
    now = now or timezone.now()
    out = []
    for w in Worker.objects.select_related("current_run__job"):
        online = w.is_online(now)
        if w.stopped_at:
            state = "stopped"
        elif online is None:
            state = "silent" if w.is_silent(now) else "no heartbeat"
        else:
            state = "online" if online else "offline"
        out.append(
            {
                "worker_id": w.worker_id,
                "hostname": w.hostname,
                "pid": w.pid,
                "backend": w.backend,
                "queues": w.queues,
                "state": state,
                "started_at": w.started_at,
                "last_seen_at": w.last_seen_at,
                "stopped_at": w.stopped_at,
                "heartbeat_seconds": w.heartbeat_seconds,
                "tasks_processed": w.tasks_processed,
                "tasks_failed": w.tasks_failed,
                "current_run": w.current_run,
            }
        )
    return out


def timeseries(minutes: int = 60, queue_name: str | None = None, now=None) -> list[dict]:
    return metrics.series(minutes, queue_name=queue_name, now=now)


def job_queryset(
    *,
    status: str | None = None,
    queue_name: str | None = None,
    task_path: str | None = None,
    search: str | None = None,
    worker_id: str | None = None,
    dismissed: bool | None = None,
):
    qs = Job.objects.all()
    if status:
        qs = qs.filter(status=status)
    if queue_name:
        qs = qs.filter(queue_name=queue_name)
    if task_path:
        qs = qs.filter(task_path=task_path)
    # Run conditions are subqueries rather than joins: no DISTINCT, no GROUP BY, and the
    # paginator's COUNT stays a plain count over the job table.
    if worker_id:
        qs = qs.filter(id__in=Run.objects.filter(worker_id=worker_id).values("job_id"))
    if dismissed is not None:
        qs = qs.filter(dismissed=dismissed)
    if search:
        qs = qs.filter(
            Q(task_path__icontains=search)
            | Q(id__icontains=search)
            | Q(id__in=Run.objects.filter(result_id__icontains=search).values("job_id"))
            | Q(unique_key__icontains=search)
        )
    runs = Run.objects.filter(job=OuterRef("pk")).order_by()
    return qs.annotate(
        run_count=Coalesce(Subquery(runs.values("job").annotate(n=Count("pk")).values("n")[:1]), 0),
        last_run_at=Subquery(runs.order_by("-enqueued_at").values("enqueued_at")[:1]),
    ).order_by("-created_at", "id")


def run_chain(job: Job):
    return list(job.runs.select_related("retry_of").order_by("attempt"))


def failed_jobs():
    """Open failed jobs, newest failure first, with the last attempt's error attached."""
    last = Run.objects.filter(job=OuterRef("pk")).order_by("-attempt")
    return (
        Job.objects.filter(status=JobStatus.FAILED, dismissed=False)
        .annotate(
            last_error=Subquery(last.values("exception_class")[:1]),
            last_run_at=Subquery(last.values("finished_at")[:1]),
        )
        .order_by("-last_run_at", "-created_at")
    )

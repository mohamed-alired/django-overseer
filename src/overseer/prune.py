"""Retention: delete old jobs, runs, metrics, alerts and workers."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import conf
from .models import Alert, Job, JobStatus, MetricBucket, Worker

TERMINAL = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


def prune(now=None, *, days: int | None = None, metrics_days: int | None = None) -> dict[str, int]:
    now = now or timezone.now()
    if days is None:
        days = conf.get_setting("OVERSEER_RETENTION_DAYS")
    if metrics_days is None:
        metrics_days = conf.get_setting("OVERSEER_METRICS_RETENTION_DAYS")
    if days < 0 or metrics_days < 0:
        raise ValueError("retention days cannot be negative")
    cutoff = now - timedelta(days=days)
    metrics_cutoff = now - timedelta(days=metrics_days)
    _, by_model = Job.objects.filter(status__in=TERMINAL, finished_at__lt=cutoff).delete()
    buckets, _ = MetricBucket.objects.filter(bucket_start__lt=metrics_cutoff).delete()
    # Open alerts stay: deleting one would make a persisting condition fire (and notify) again.
    alerts, _ = Alert.objects.filter(created_at__lt=cutoff, resolved_at__isnull=False).delete()
    # Plain db_worker processes get a new random id on every start and never report a stop,
    # so their rows are dropped once silent for a day (or OVERSEER_SILENT_WORKER_AFTER when
    # that is longer); heartbeat workers keep the retention.
    silent_for = max(
        timedelta(days=1), timedelta(seconds=conf.get_setting("OVERSEER_SILENT_WORKER_AFTER"))
    )
    silent_cutoff = now - silent_for
    workers, _ = Worker.objects.filter(
        Q(last_seen_at__lt=cutoff)
        | Q(heartbeat_seconds__isnull=True, stopped_at__isnull=True, last_seen_at__lt=silent_cutoff)
    ).delete()
    return {
        "jobs": by_model.get("overseer.Job", 0),
        "runs": by_model.get("overseer.Run", 0),
        "metric_buckets": buckets,
        "alerts": alerts,
        "workers": workers,
    }

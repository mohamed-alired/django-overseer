"""Retention: delete old jobs, runs, metrics, alerts and workers."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from . import conf
from .models import Alert, Job, JobStatus, MetricBucket, Worker

TERMINAL = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


def prune(now=None, *, days: int | None = None, metrics_days: int | None = None) -> dict[str, int]:
    now = now or timezone.now()
    days = days or conf.get_setting("OVERSEER_RETENTION_DAYS")
    metrics_days = metrics_days or conf.get_setting("OVERSEER_METRICS_RETENTION_DAYS")
    cutoff = now - timedelta(days=days)
    metrics_cutoff = now - timedelta(days=metrics_days)
    _, by_model = Job.objects.filter(status__in=TERMINAL, finished_at__lt=cutoff).delete()
    buckets, _ = MetricBucket.objects.filter(bucket_start__lt=metrics_cutoff).delete()
    alerts, _ = Alert.objects.filter(created_at__lt=cutoff).delete()
    workers, _ = Worker.objects.filter(last_seen_at__lt=cutoff).delete()
    return {
        "jobs": by_model.get("overseer.Job", 0),
        "runs": by_model.get("overseer.Run", 0),
        "metric_buckets": buckets,
        "alerts": alerts,
        "workers": workers,
    }

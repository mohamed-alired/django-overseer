"""Per-minute rollups of Run rows into MetricBucket, so dashboards never scan Run."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import MetricBucket, Run, RunStatus

logger = logging.getLogger("overseer")

BUCKET = timedelta(minutes=1)
#: Buckets this far back are recomputed on every rollup, to absorb late finishes.
RECOMPUTE = timedelta(minutes=3)
ALL_TASKS = ""


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = math.ceil(pct * len(ordered))  # nearest-rank method
    return int(ordered[max(rank - 1, 0)])


class _Agg:
    __slots__ = ("abandoned", "durations", "enqueued", "failed", "started", "succeeded", "waits")

    def __init__(self):
        self.enqueued = self.started = self.succeeded = self.failed = self.abandoned = 0
        self.durations: list[int] = []
        self.waits: list[int] = []

    def as_fields(self) -> dict:
        return {
            "enqueued": self.enqueued,
            "started": self.started,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "abandoned": self.abandoned,
            "runtime_ms_sum": sum(self.durations),
            "runtime_ms_p50": percentile(self.durations, 0.50),
            "runtime_ms_p95": percentile(self.durations, 0.95),
            "runtime_ms_max": max(self.durations, default=0),
            "wait_ms_sum": sum(self.waits),
            "wait_ms_max": max(self.waits, default=0),
        }


METRIC_FIELDS = [
    "enqueued",
    "started",
    "succeeded",
    "failed",
    "abandoned",
    "runtime_ms_sum",
    "runtime_ms_p50",
    "runtime_ms_p95",
    "runtime_ms_max",
    "wait_ms_sum",
    "wait_ms_max",
]


def aggregate(since: datetime, until: datetime) -> dict[tuple[datetime, str, str], _Agg]:
    """Aggregate every Run event (enqueue, start, finish) that fell in [since, until)."""
    buckets: dict[tuple[datetime, str, str], _Agg] = defaultdict(_Agg)
    in_range = Q(enqueued_at__gte=since, enqueued_at__lt=until)
    in_range |= Q(started_at__gte=since, started_at__lt=until)
    in_range |= Q(finished_at__gte=since, finished_at__lt=until)
    runs = Run.objects.filter(in_range).values_list(
        "job__queue_name",
        "job__task_path",
        "enqueued_at",
        "started_at",
        "finished_at",
        "status",
        "duration_ms",
        "wait_ms",
    )
    for queue, task_path, enqueued_at, started_at, finished_at, status, duration, wait in runs:
        for key_task in (task_path, ALL_TASKS):
            if since <= enqueued_at < until:
                buckets[(floor_minute(enqueued_at), queue, key_task)].enqueued += 1
            if started_at and since <= started_at < until:
                agg = buckets[(floor_minute(started_at), queue, key_task)]
                agg.started += 1
                if wait is not None:
                    agg.waits.append(wait)
            if finished_at and since <= finished_at < until:
                agg = buckets[(floor_minute(finished_at), queue, key_task)]
                if status == RunStatus.SUCCESSFUL:
                    agg.succeeded += 1
                elif status == RunStatus.FAILED:
                    agg.failed += 1
                elif status == RunStatus.ABANDONED:
                    agg.abandoned += 1
                if duration is not None and status != RunStatus.CANCELLED:
                    agg.durations.append(duration)
    return buckets


def rollup(since: datetime | None = None, until: datetime | None = None) -> int:
    """Recompute buckets for [since, until). Defaults: from the newest bucket minus a few
    minutes (or the oldest run) through the current minute. Returns the buckets written."""
    now = timezone.now()
    # The current minute is included (partial) and recomputed by later rollups.
    until = floor_minute(until) if until is not None else floor_minute(now) + BUCKET
    if since is None:
        newest = MetricBucket.objects.aggregate(m=Max("bucket_start"))["m"]
        if newest is not None:
            since = newest - RECOMPUTE
        else:
            oldest = (
                Run.objects.order_by("enqueued_at").values_list("enqueued_at", flat=True).first()
            )
            since = floor_minute(oldest) if oldest else until
    since = floor_minute(since)
    if since >= until:
        return 0
    buckets = aggregate(since, until)
    rows = [
        MetricBucket(bucket_start=start, queue_name=queue, task_path=task, **agg.as_fields())
        for (start, queue, task), agg in buckets.items()
    ]
    unique = ["bucket_start", "queue_name", "task_path"]
    with transaction.atomic():
        MetricBucket.objects.filter(bucket_start__gte=since, bucket_start__lt=until).delete()
        if connection.features.supports_update_conflicts_with_target:
            # An upsert: two schedulers rolling up the same minute at once must not collide
            # on the unique (bucket_start, queue_name, task_path) constraint.
            MetricBucket.objects.bulk_create(
                rows,
                batch_size=500,
                update_conflicts=True,
                unique_fields=unique,
                update_fields=METRIC_FIELDS,
            )
        else:
            # MySQL, MariaDB and Oracle cannot upsert on a target: insert, and if another
            # scheduler wrote the same minutes first, let its rows stand.
            try:
                with transaction.atomic():
                    MetricBucket.objects.bulk_create(rows, batch_size=500)
            except IntegrityError:
                logger.info("Metric rollup for %s-%s was written by another process", since, until)
                return 0
    return len(rows)


def series(minutes: int, queue_name: str | None = None, task_path: str = ALL_TASKS, now=None):
    """Per-minute points for the last ``minutes`` (zero-filled), summed across queues unless
    one is given."""
    now = floor_minute(now or timezone.now())
    start = now - timedelta(minutes=minutes - 1)
    qs = MetricBucket.objects.filter(bucket_start__gte=start, task_path=task_path)
    if queue_name:
        qs = qs.filter(queue_name=queue_name)
    points = {start + timedelta(minutes=i): _Agg() for i in range(minutes)}
    for b in qs:
        agg = points.get(b.bucket_start)
        if agg is None:
            continue
        agg.enqueued += b.enqueued
        agg.started += b.started
        agg.succeeded += b.succeeded
        agg.failed += b.failed
        agg.abandoned += b.abandoned
        agg.durations.append(b.runtime_ms_p95)
        agg.waits.append(b.wait_ms_max)
    return [
        {
            "at": at,
            "enqueued": a.enqueued,
            "started": a.started,
            "succeeded": a.succeeded,
            "failed": a.failed + a.abandoned,
            "runtime_p95_ms": max(a.durations, default=0),
            "wait_max_ms": max(a.waits, default=0),
        }
        for at, a in sorted(points.items())
    ]

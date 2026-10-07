from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone
from freezegun import freeze_time

from overseer import metrics, prune
from overseer.models import Alert, Job, JobStatus, MetricBucket, Run, RunStatus, Worker

pytestmark = pytest.mark.django_db


def make_run(queue, task, enq, start, finish, status=RunStatus.SUCCESSFUL, duration=100):
    job = Job.objects.create(
        task_path=task,
        task_name=task.rsplit(".", 1)[-1],
        backend="default",
        queue_name=queue,
        status=JobStatus.SUCCEEDED if status == RunStatus.SUCCESSFUL else JobStatus.FAILED,
        finished_at=finish,
    )
    return Run.objects.create(
        job=job,
        result_id=f"r-{job.pk}",
        backend="default",
        status=status,
        enqueued_at=enq,
        started_at=start,
        finished_at=finish,
        duration_ms=duration,
        wait_ms=int((start - enq).total_seconds() * 1000) if start else None,
    )


T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestRollup:
    def test_buckets_per_queue_and_task_plus_all(self):
        s = T0 + timedelta(seconds=5)
        make_run("default", "app.a", T0, s, s + timedelta(seconds=1), duration=1000)
        make_run("default", "app.a", T0, s, s + timedelta(seconds=2), duration=2000)
        make_run("default", "app.b", T0, s, s + timedelta(seconds=3), RunStatus.FAILED, 3000)
        make_run("emails", "app.a", T0 + timedelta(minutes=1), None, None, RunStatus.READY, None)
        written = metrics.rollup(since=T0, until=T0 + timedelta(minutes=5))
        assert written == 5  # default: app.a, app.b, all; emails: app.a, all
        all_default = MetricBucket.objects.get(bucket_start=T0, queue_name="default", task_path="")
        assert all_default.enqueued == 3 and all_default.started == 3
        assert all_default.succeeded == 2 and all_default.failed == 1
        assert all_default.runtime_ms_sum == 6000 and all_default.runtime_ms_max == 3000
        assert all_default.runtime_ms_p50 == 2000 and all_default.runtime_ms_p95 == 3000
        assert all_default.wait_ms_max == 5000 and all_default.failure_rate == pytest.approx(1 / 3)
        a = MetricBucket.objects.get(bucket_start=T0, queue_name="default", task_path="app.a")
        assert a.succeeded == 2 and a.failed == 0
        emails = MetricBucket.objects.get(
            bucket_start=T0 + timedelta(minutes=1), queue_name="emails", task_path=""
        )
        assert emails.enqueued == 1 and emails.started == 0 and emails.finished == 0

    def test_events_land_in_their_own_minute(self):
        enq = T0
        start = T0 + timedelta(minutes=2)
        finish = T0 + timedelta(minutes=4, seconds=30)
        make_run("default", "app.a", enq, start, finish)
        metrics.rollup(since=T0, until=T0 + timedelta(minutes=10))
        by_min = {
            b.bucket_start: b
            for b in MetricBucket.objects.filter(task_path="", queue_name="default")
        }
        assert by_min[T0].enqueued == 1 and by_min[T0].started == 0
        assert by_min[T0 + timedelta(minutes=2)].started == 1
        assert by_min[T0 + timedelta(minutes=4)].succeeded == 1

    def test_rerollup_is_idempotent_and_absorbs_late_finishes(self):
        s = T0 + timedelta(seconds=1)
        run = make_run("default", "app.a", T0, s, None, RunStatus.RUNNING, None)
        metrics.rollup(since=T0, until=T0 + timedelta(minutes=2))
        assert MetricBucket.objects.get(queue_name="default", task_path="").succeeded == 0
        run.status = RunStatus.SUCCESSFUL
        run.finished_at = s + timedelta(seconds=30)
        run.duration_ms = 30000
        run.save()
        metrics.rollup(since=T0, until=T0 + timedelta(minutes=2))
        metrics.rollup(since=T0, until=T0 + timedelta(minutes=2))
        assert MetricBucket.objects.filter(bucket_start=T0).count() == 2  # app.a + all
        assert MetricBucket.objects.get(queue_name="default", task_path="").succeeded == 1

    def test_default_window_starts_at_oldest_run_then_recomputes_tail(self):
        with freeze_time(T0 + timedelta(minutes=10)):
            make_run("default", "app.a", T0, T0, T0 + timedelta(seconds=1))
            assert metrics.rollup() == 2
            assert MetricBucket.objects.filter(bucket_start=T0).count() == 2
        with freeze_time(T0 + timedelta(minutes=20)):
            make_run(
                "default",
                "app.a",
                T0 + timedelta(minutes=9),
                T0 + timedelta(minutes=9),
                T0 + timedelta(minutes=9, seconds=1),
            )
            metrics.rollup()
            assert MetricBucket.objects.filter(bucket_start=T0 + timedelta(minutes=9)).exists()

    def test_empty_range(self):
        assert metrics.rollup(since=T0, until=T0) == 0
        assert metrics.rollup() == 0

    def test_current_minute_is_included(self):
        now = timezone.now()
        make_run("default", "app.a", now, now, now)
        assert metrics.rollup() == 2
        assert MetricBucket.objects.get(task_path="").succeeded == 1

    def test_percentile(self):
        assert metrics.percentile([], 0.5) == 0
        assert metrics.percentile([5], 0.95) == 5
        assert metrics.percentile(list(range(1, 101)), 0.5) == 50
        assert metrics.percentile(list(range(1, 101)), 0.95) == 95

    def test_series_zero_fills_and_sums_queues(self):
        s = T0 + timedelta(seconds=1)
        make_run("default", "app.a", T0, s, s + timedelta(seconds=1))
        make_run("emails", "app.b", T0, s, s + timedelta(seconds=1), RunStatus.FAILED)
        metrics.rollup(since=T0, until=T0 + timedelta(minutes=5))
        points = metrics.series(3, now=T0 + timedelta(minutes=2))
        assert [p["at"] for p in points] == [
            T0,
            T0 + timedelta(minutes=1),
            T0 + timedelta(minutes=2),
        ]
        assert (
            points[0]["enqueued"] == 2 and points[0]["succeeded"] == 1 and points[0]["failed"] == 1
        )
        assert points[1]["enqueued"] == 0
        only_emails = metrics.series(3, queue_name="emails", now=T0 + timedelta(minutes=2))
        assert only_emails[0]["enqueued"] == 1 and only_emails[0]["failed"] == 1

    def test_command(self, capsys):
        make_run("default", "app.a", T0, T0, T0 + timedelta(seconds=1))
        call_command("overseer_rollup", since=T0.isoformat())
        assert "Wrote 2 metric bucket(s)." in capsys.readouterr().out


class TestPrune:
    def test_prunes_by_retention(self, settings, capsys):
        settings.OVERSEER_RETENTION_DAYS = 7
        settings.OVERSEER_METRICS_RETENTION_DAYS = 2
        now = timezone.now()
        old = now - timedelta(days=8)
        make_run("default", "app.a", old, old, old)  # finished 8 days ago -> gone
        make_run("default", "app.a", now, now, now)  # fresh -> kept
        pending = Job.objects.create(
            task_path="x", task_name="x", backend="default", queue_name="d"
        )
        Job.objects.filter(pk=pending.pk).update(created_at=old)  # pending jobs are never pruned
        MetricBucket.objects.create(bucket_start=now - timedelta(days=3), queue_name="d")
        MetricBucket.objects.create(bucket_start=now, queue_name="d")
        Alert.objects.create(kind="k", key="a", message="m", created_at=old, resolved_at=old)
        # Still open: kept, or a persisting condition would fire and notify again.
        Alert.objects.create(kind="k", key="b", message="m", created_at=old)
        Worker.objects.create(worker_id="dead", last_seen_at=old)
        Worker.objects.create(worker_id="alive")
        call_command("overseer_prune")
        out = capsys.readouterr().out
        assert "jobs: 1" in out and "runs: 1" in out and "metric_buckets: 1" in out
        assert "alerts: 1" in out and "workers: 1" in out
        assert Job.objects.count() == 2 and Run.objects.count() == 1
        assert MetricBucket.objects.count() == 1
        assert list(Alert.objects.values_list("key", flat=True)) == ["b"]
        assert list(Worker.objects.values_list("worker_id", flat=True)) == ["alive"]

    def test_overrides(self):
        now = timezone.now()
        old = now - timedelta(days=3)
        make_run("default", "app.a", old, old, old)
        assert prune.prune(days=2)["jobs"] == 1

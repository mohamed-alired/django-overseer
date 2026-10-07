"""Performance guards: query counts stay flat and pages stay fast with thousands of rows."""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from overseer import metrics, prune, stats
from overseer.models import Alert, Job, JobStatus, MetricBucket, Run, RunStatus, Schedule, Worker

pytestmark = [pytest.mark.perf, pytest.mark.django_db]

JOBS = 3000
TASKS = ["tests.tasks.plain", "tests.tasks.flaky", "tests.tasks.unique_by_args"]
QUEUES = ["default", "emails", "reports"]
BUDGET_SECONDS = 2.0  # generous: CI machines are slow; a regression is 10x, not 10%


@pytest.fixture(scope="module")
def big_dataset(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        now = timezone.now()
        schedules = [
            Schedule(name=f"s{i}", task_path=TASKS[i % 3], cron="*/5 * * * *", enabled=True)
            for i in range(50)
        ]
        Schedule.objects.bulk_create(schedules)
        jobs, runs = [], []
        for i in range(JOBS):
            created = now - timedelta(seconds=i * 2)
            failed = i % 10 == 0
            job = Job(
                id=uuid.uuid4(),
                task_path=TASKS[i % 3],
                task_name=TASKS[i % 3].rsplit(".", 1)[1],
                backend="default",
                queue_name=QUEUES[i % 3],
                args=[i],
                kwargs={"n": i},
                status=JobStatus.FAILED if failed else JobStatus.SUCCEEDED,
                attempts=2 if failed else 1,
                max_retries=1 if failed else 0,
                tags=["perf"],
                schedule=schedules[i % 50] if i % 7 == 0 else None,
                created_at=created,
                finished_at=created + timedelta(seconds=3),
            )
            jobs.append(job)
            for attempt in range(1, job.attempts + 1):
                started = created + timedelta(milliseconds=150 * attempt)
                runs.append(
                    Run(
                        job=job,
                        result_id=str(uuid.uuid4()),
                        backend="default",
                        attempt=attempt,
                        status=RunStatus.FAILED if failed else RunStatus.SUCCESSFUL,
                        enqueued_at=created,
                        started_at=started,
                        finished_at=started + timedelta(milliseconds=40 + (i % 50)),
                        worker_id=f"w{i % 8}",
                        exception_class="builtins.RuntimeError" if failed else "",
                        traceback="Traceback...\nRuntimeError: perf" if failed else "",
                        duration_ms=40 + (i % 50),
                        wait_ms=150,
                    )
                )
        Job.objects.bulk_create(jobs, batch_size=500)
        Run.objects.bulk_create(runs, batch_size=500)
        Worker.objects.bulk_create(
            [
                Worker(
                    worker_id=f"w{i}", hostname="perf", backend="default", pid=100 + i, queues=["*"]
                )
                for i in range(8)
            ]
        )
        Alert.objects.bulk_create(
            [
                Alert(kind="failure_rate", key=f"q{i}", message="m", value=0.5, threshold=0.25)
                for i in range(100)
            ]
        )
        yield
        for model in (Run, Job, Schedule, Worker, Alert, MetricBucket):
            model.objects.all().delete()


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, time.perf_counter() - start


class TestQueryBudgets:
    @pytest.mark.parametrize(
        ("name", "budget"),
        [
            ("overview", 30),
            ("queues", 20),
            ("tasks", 20),
            ("jobs", 12),
            ("failed", 12),
            ("schedules", 8),
            ("workers", 8),
            ("metrics", 12),
            ("alerts", 8),
        ],
    )
    def test_pages_have_flat_query_counts(
        self, big_dataset, staff_client, django_assert_max_num_queries, name, budget
    ):
        with django_assert_max_num_queries(budget):
            resp, elapsed = timed(staff_client.get, reverse(f"overseer:{name}"))
        assert resp.status_code == 200
        assert elapsed < BUDGET_SECONDS, f"{name} took {elapsed:.2f}s"

    @pytest.mark.parametrize(
        "name", ["overview", "queues", "tasks", "jobs", "workers", "schedules", "metrics", "alerts"]
    )
    def test_api_endpoints(self, big_dataset, staff_client, django_assert_max_num_queries, name):
        with django_assert_max_num_queries(20):
            resp, elapsed = timed(staff_client.get, reverse(f"overseer:api-{name}"))
        assert resp.status_code == 200
        assert elapsed < BUDGET_SECONDS

    def test_job_detail_is_constant(self, big_dataset, staff_client, django_assert_max_num_queries):
        job = Job.objects.filter(status=JobStatus.FAILED).first()
        with django_assert_max_num_queries(10):
            resp = staff_client.get(reverse("overseer:job", kwargs={"pk": job.pk}))
        assert resp.status_code == 200

    def test_jobs_page_last_page_and_search(self, big_dataset, staff_client):
        last = (JOBS // 50) + 1
        resp, elapsed = timed(staff_client.get, reverse("overseer:jobs") + f"?page={last}")
        assert resp.status_code == 200 and elapsed < BUDGET_SECONDS
        resp, elapsed = timed(staff_client.get, reverse("overseer:jobs") + "?q=plain&status=failed")
        assert resp.status_code == 200 and elapsed < BUDGET_SECONDS


class TestBatchWork:
    def test_rollup_thousands_of_runs(self, big_dataset):
        since = timezone.now() - timedelta(seconds=JOBS * 2 + 60)
        written, elapsed = timed(metrics.rollup, since)
        assert written > 0
        assert elapsed < 5 * BUDGET_SECONDS, f"rollup took {elapsed:.2f}s"
        total = MetricBucket.objects.filter(task_path="").aggregate(s=Sum("succeeded"))["s"]
        assert total == Job.objects.filter(status=JobStatus.SUCCEEDED).count()
        # A second rollup over the same window rewrites the same buckets, not new ones.
        count = MetricBucket.objects.count()
        metrics.rollup(since)
        assert MetricBucket.objects.count() == count

    def test_series_over_a_day(self, big_dataset, django_assert_max_num_queries):
        metrics.rollup(timezone.now() - timedelta(days=1))
        with django_assert_max_num_queries(3):
            points, elapsed = timed(metrics.series, 1440)
        assert len(points) == 1440
        assert elapsed < BUDGET_SECONDS

    def test_stats_functions(self, big_dataset, django_assert_max_num_queries):
        with django_assert_max_num_queries(15):
            _, elapsed = timed(stats.overview, 1440)
        assert elapsed < BUDGET_SECONDS
        with django_assert_max_num_queries(15):
            rows, elapsed = timed(stats.tasks, 1440)
        assert len(rows) == 3 and elapsed < BUDGET_SECONDS
        with django_assert_max_num_queries(15):
            rows, elapsed = timed(stats.queues, 1440)
        assert len(rows) == 3 and elapsed < BUDGET_SECONDS

    def test_prune_is_fast(self, big_dataset):
        before = Job.objects.count()
        report, elapsed = timed(prune.prune, now=timezone.now() + timedelta(days=15))
        assert elapsed < 5 * BUDGET_SECONDS
        assert Job.objects.count() < before
        assert report["jobs"] == before - Job.objects.count()

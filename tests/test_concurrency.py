"""Races that only a real row-locking database can show (PostgreSQL)."""

from __future__ import annotations

import threading

import pytest
from django.db import connection
from django_tasks_db.management.commands.db_worker import Worker as DBWorker

from overseer.models import Job, JobStatus, Run, RunStatus, Worker
from overseer.retry import retry_job
from tests import tasks

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="row locking is only real on PostgreSQL"
    ),
]


def run_threads(n, target):
    """Run ``target(i)`` in ``n`` threads released together; collect results and errors."""
    barrier = threading.Barrier(n)
    results, errors = [None] * n, [None] * n

    def runner(i):
        try:
            barrier.wait(timeout=10)
            results[i] = target(i)
        except Exception as exc:  # noqa: BLE001
            errors[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def test_concurrent_unique_enqueue_creates_one_job():
    results, errors = run_threads(8, lambda i: tasks.unique_by_args.enqueue("race@example.com"))
    assert not any(errors), errors
    assert Job.objects.filter(unique_key__isnull=False).exclude(unique_key="").count() == 1
    assert Run.objects.count() == 1
    assert len({r.id for r in results}) == 1


def test_two_workers_never_run_the_same_job_twice():
    for i in range(60):
        tasks.plain.enqueue(i)

    def run_worker(i):
        DBWorker(
            queue_names=["*"],
            interval=0.0,
            batch=True,
            backend_name="default",
            startup_delay=False,
            max_tasks=None,
            worker_id=f"concurrent-{i}",
            excluded_queue_names=[],
        ).run()

    _, errors = run_threads(2, run_worker)
    assert not any(errors), errors
    assert Job.objects.filter(status=JobStatus.SUCCEEDED).count() == 60
    assert Run.objects.count() == 60
    assert set(Run.objects.values_list("worker_id", flat=True)) <= {
        "concurrent-0",
        "concurrent-1",
    }
    assert sum(Worker.objects.values_list("tasks_processed", flat=True)) == 60
    assert sorted(r.return_value for r in Run.objects.all()) == [i * 2 for i in range(60)]


def test_concurrent_manual_retries_enqueue_one_attempt(worker):
    tasks.always_fails.enqueue()
    worker()
    job = Job.objects.get()
    assert job.status == JobStatus.FAILED

    results, errors = run_threads(6, lambda i: retry_job(Job.objects.get(pk=job.pk)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert all(isinstance(e, ValueError) for e in errors if e is not None)
    assert job.runs.count() == 2
    assert job.runs.filter(status=RunStatus.READY).count() == 1


def test_concurrent_finishes_update_counters_consistently():
    """Many jobs finishing at once (two worker threads) keep the Worker counters exact."""
    for _ in range(20):
        tasks.always_fails.enqueue()
    for i in range(20):
        tasks.plain.enqueue(i)

    def run_worker(i):
        DBWorker(
            queue_names=["*"],
            interval=0.0,
            batch=True,
            backend_name="default",
            startup_delay=False,
            max_tasks=None,
            worker_id=f"counter-{i}",
            excluded_queue_names=[],
        ).run()

    _, errors = run_threads(3, run_worker)
    assert not any(errors), errors
    assert Job.objects.filter(status=JobStatus.FAILED).count() == 20
    assert Job.objects.filter(status=JobStatus.SUCCEEDED).count() == 20
    assert sum(Worker.objects.values_list("tasks_processed", flat=True)) == 40
    assert sum(Worker.objects.values_list("tasks_failed", flat=True)) == 20

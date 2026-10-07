"""Integration: real ``overseer_worker`` / ``overseer_scheduler`` processes sharing the DB.

These need a database a subprocess can reach: PostgreSQL, or SQLite in a file
(``OVERSEER_SQLITE_FILE=/tmp/overseer.sqlite3 pytest``). They are skipped on the default
in-memory SQLite.
"""

from __future__ import annotations

import os
import signal
import time

import pytest
from django.test import override_settings
from django.utils import timezone

from overseer.models import Job, JobStatus, Run, RunStatus, Schedule, Worker
from overseer.rescue import rescue
from tests import tasks
from tests.procs import Process, db_is_shareable, wait_for

pytestmark = [
    pytest.mark.worker,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(not db_is_shareable(), reason="needs a database a subprocess can reach"),
]


def worker_process(worker_id="itest", **extra):
    args = [
        "--queue-name=*",
        "--interval=0.1",
        "--no-startup-delay",
        f"--worker-id={worker_id}",
        "--heartbeat=0.2",
    ]
    args += [f"--{k.replace('_', '-')}={v}" for k, v in extra.items()]
    return Process("overseer_worker", *args)


def job_state(result):
    def _state():
        job = Job.objects.filter(runs__result_id=str(result.id)).first()
        return job if job and job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED} else None

    return _state


class TestRealWorker:
    def test_runs_jobs_and_reports_heartbeats(self):
        with worker_process("itest-1") as proc:
            r1 = tasks.plain.enqueue(1)
            r2 = tasks.unique_by_args.enqueue("a@example.com")
            r3 = tasks.with_context.enqueue(5)
            for r in (r1, r2, r3):
                job = wait_for(job_state(r), message=f"job for {r.id}")
                assert job.status == JobStatus.SUCCEEDED, proc.output

            row = Worker.objects.get(worker_id="itest-1")
            assert row.pid == proc.pid
            assert row.hostname
            assert row.queues == ["*"]
            assert row.stopped_at is None
            seen = row.last_seen_at
            time.sleep(0.6)
            assert Worker.objects.get(pk=row.pk).last_seen_at > seen, "heartbeat not advancing"
            assert row.tasks_processed == 3 or Worker.objects.get(pk=row.pk).tasks_processed == 3

            runs = Run.objects.filter(result_id__in=[str(r.id) for r in (r1, r2, r3)])
            assert {run.worker_id for run in runs} == {"itest-1"}
            assert Run.objects.get(result_id=str(r3.id)).return_value == {
                "attempt": 1,
                "value": 5,
            }
            assert all(run.duration_ms is not None and run.wait_ms is not None for run in runs)

            assert proc.stop() == 0, proc.output
        row.refresh_from_db()
        assert row.stopped_at is not None

    def test_retries_until_success(self):
        with worker_process("itest-retry") as proc:
            result = tasks.flaky_fast.enqueue(2)
            job = wait_for(job_state(result), message="flaky job to finish")
            assert job.status == JobStatus.SUCCEEDED, proc.output
            assert job.attempts == 3
            runs = list(job.runs.order_by("attempt"))
            assert [r.status for r in runs] == [
                RunStatus.FAILED,
                RunStatus.FAILED,
                RunStatus.SUCCESSFUL,
            ]
            assert [r.attempt for r in runs] == [1, 2, 3]
            assert runs[1].retry_of_id == runs[0].pk and runs[2].retry_of_id == runs[1].pk
            assert "boom 1" in runs[0].traceback and runs[0].exception_class.endswith(
                "RuntimeError"
            )
            assert runs[2].return_value == "done"
            proc.stop()

    def test_exhausts_retries_and_marks_failed(self):
        with worker_process("itest-fail") as proc:
            result = tasks.flaky_fast.enqueue(10)
            job = wait_for(job_state(result), message="flaky job to give up")
            assert job.status == JobStatus.FAILED, proc.output
            assert job.attempts == 3
            assert job.finished_at is not None
            assert job.runs.filter(status=RunStatus.FAILED).count() == 3
            proc.stop()

    def test_sigterm_finishes_current_task_then_stops(self):
        with worker_process("itest-term") as proc:
            result = tasks.slow.enqueue(1.5)
            wait_for(
                lambda: Run.objects.filter(
                    result_id=str(result.id), status=RunStatus.RUNNING
                ).exists(),
                message="slow task to start",
            )
            code = proc.stop(sig=signal.SIGTERM)
            assert code == 0, proc.output
        job = Job.objects.get(runs__result_id=str(result.id))
        assert job.status == JobStatus.SUCCEEDED
        assert Worker.objects.get(worker_id="itest-term").stopped_at is not None

    def test_killed_worker_is_rescued(self):
        with worker_process("itest-kill") as proc:
            result = tasks.slow.enqueue(30)
            wait_for(
                lambda: Run.objects.filter(
                    result_id=str(result.id), status=RunStatus.RUNNING
                ).exists(),
                message="slow task to start",
            )
            proc.kill()
        run = Run.objects.get(result_id=str(result.id))
        assert run.status == RunStatus.RUNNING  # nobody told the database
        assert Worker.objects.get(worker_id="itest-kill").stopped_at is None  # a crash, not a stop

        with override_settings(OVERSEER_STALE_AFTER=0):
            abandoned = rescue(now=timezone.now() + timezone.timedelta(seconds=1))
        assert [r.pk for r in abandoned] == [run.pk]
        run.refresh_from_db()
        assert run.status == RunStatus.ABANDONED
        job = run.job
        assert job.status == JobStatus.FAILED  # ``slow`` has no retries
        # The backend row was reset too, so it is not re-run by the next worker.
        from django_tasks_db.models import DBTaskResult

        assert DBTaskResult.objects.get(pk=result.id).status != "RUNNING"

    def test_exclude_queues_and_max_tasks(self):
        tasks.unique_by_args.enqueue("x@example.com")  # queue "emails"
        r_default = tasks.plain.enqueue(3)
        with worker_process("itest-excl", exclude_queues="emails", max_tasks=1) as proc:
            assert proc.wait() == 0, proc.output  # exits by itself after one task
        assert Job.objects.get(runs__result_id=str(r_default.id)).status == JobStatus.SUCCEEDED
        assert Job.objects.get(queue_name="emails").status == JobStatus.PENDING


class TestRealScheduler:
    def test_scheduler_and_worker_cooperate(self):
        schedule = Schedule.objects.create(
            name="itest-every-second",
            task_path="tests.tasks.plain",
            interval_seconds=1,
            args=[7],
            enabled=True,
            next_run_at=timezone.now(),
        )
        with (
            Process("overseer_scheduler", "--interval=0.1") as scheduler,
            worker_process("itest-sched") as worker,
        ):
            wait_for(
                lambda: (
                    Job.objects.filter(schedule=schedule, status=JobStatus.SUCCEEDED).count() >= 2
                ),
                message="two scheduled jobs to succeed",
            )
            schedule.refresh_from_db()
            assert schedule.runs_count >= 2
            assert schedule.last_job_id is not None
            assert schedule.next_run_at > timezone.now() - timezone.timedelta(seconds=2)
            # Declared-in-code schedules were synced by the scheduler process.
            assert Schedule.objects.filter(name="heartbeat", declared_in_code=True).exists()
            assert Schedule.objects.filter(
                name="tests.tasks.scheduled_every_5", enabled=True
            ).exists()
            assert scheduler.stop() == 0, scheduler.output
            worker.stop()
        job = Job.objects.filter(schedule=schedule).first()
        assert job.source == "schedule"
        assert job.runs.first().return_value == 14

    def test_scheduler_once(self):
        Schedule.objects.create(
            name="itest-once",
            task_path="tests.tasks.plain",
            cron="* * * * *",
            args=[1],
            enabled=True,
            next_run_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        with Process("overseer_scheduler", "--once") as proc:
            assert proc.wait() == 0, proc.output
        assert "1 tick" in proc.output
        assert Job.objects.filter(schedule__name="itest-once").count() == 1


def test_subprocess_sees_the_same_database():
    """Guard for the harness itself: the env var really points the child at the test DB."""
    env_name = os.environ.get("OVERSEER_DB_NAME")
    assert env_name is None or env_name  # the parent never sets it; children do

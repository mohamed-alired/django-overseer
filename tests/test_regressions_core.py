"""Regression tests for the core bugs fixed in 0.1.1."""

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.tasks.signals import task_enqueued
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from overseer import recorders, retry
from overseer.models import Job, JobSource, JobStatus, Run, RunStatus, Schedule
from overseer.scheduling.scheduler import enqueue_schedule
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def calls(name):
    return [c for c in tasks.CALLS if c[0] == name]


class TestUniqueTasksRetry:
    def test_automatic_retry_runs_again(self, worker):
        tasks.unique_flaky.enqueue(1)
        worker()
        job = Job.objects.get()
        assert job.status == JobStatus.SUCCEEDED
        assert [r.status for r in job.runs.order_by("attempt")] == [
            RunStatus.FAILED,
            RunStatus.SUCCESSFUL,
        ]
        assert len(calls("unique_flaky")) == 2

    def test_key_is_free_after_success(self, worker):
        tasks.unique_flaky.enqueue(1)
        worker()
        tasks.unique_flaky.enqueue(1)
        assert Job.objects.count() == 2

    def test_manual_retry_enqueues_a_new_attempt(self, worker):
        tasks.unique_by_args.enqueue("a@example.com")
        worker()
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
        run = retry.retry_job(job)
        assert run.attempt == 2 and run.status == RunStatus.READY
        worker()
        job.refresh_from_db()
        assert job.status == JobStatus.SUCCEEDED
        assert len(calls("unique")) == 2

    @pytest.mark.skipif(
        not connection.features.supports_partial_indexes,
        reason="unique keys are only enforced with partial indexes",
    )
    def test_manual_retry_refused_when_another_job_holds_the_key(self, worker):
        tasks.unique_by_args.enqueue("k@example.com")
        worker()
        old = Job.objects.get()
        Job.objects.filter(pk=old.pk).update(status=JobStatus.FAILED)
        tasks.unique_by_args.enqueue("k@example.com")  # a new active job takes the key
        with pytest.raises(ValueError, match="unique key"):
            retry.retry_job(old)
        old.refresh_from_db()
        assert old.status == JobStatus.FAILED


class TestArgumentsWhenNotRecorded:
    def test_automatic_retry_gets_the_arguments(self, worker, settings):
        settings.OVERSEER_RECORD_ARGS = False
        tasks.needs_arg.enqueue(42)
        worker()
        job = Job.objects.get()
        assert job.args == [] and job.status == JobStatus.SUCCEEDED
        assert calls("needs_arg") == [("needs_arg", 42), ("needs_arg", 42)]

    def test_manual_retry_gets_the_arguments(self, worker, settings):
        settings.OVERSEER_RECORD_ARGS = False
        tasks.child.enqueue(7)
        worker()
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
        retry.retry_job(job)
        worker()
        assert calls("child") == [("child", 7), ("child", 7)]

    def test_manual_retry_refused_when_the_backend_forgot_them(self, worker, settings):
        settings.OVERSEER_RECORD_ARGS = False
        tasks.child.enqueue(7)
        worker()
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
        DBTaskResult.objects.all().delete()
        with pytest.raises(ValueError, match="arguments were not recorded"):
            retry.retry_job(job)
        assert job.runs.count() == 1


class TestContextDoesNotLeak:
    def test_child_of_a_retried_immediate_task_gets_its_own_job(self):
        tasks.parent_immediate.enqueue(5)
        parent = Job.objects.get(task_path="tests.tasks.parent_immediate")
        assert parent.status == JobStatus.SUCCEEDED
        assert [r.attempt for r in parent.runs.order_by("attempt")] == [1, 2]
        children = Job.objects.filter(task_path="tests.tasks.child")
        assert children.count() == 2
        assert all(c.source == JobSource.ENQUEUE and c.runs.count() == 1 for c in children)

    def test_child_of_a_unique_immediate_task_gets_its_own_job(self):
        tasks.unique_parent_immediate.enqueue(9)
        parent = Job.objects.get(task_path="tests.tasks.unique_parent_immediate")
        child = Job.objects.get(task_path="tests.tasks.child")
        assert parent.runs.count() == 1 and child.runs.count() == 1
        assert child.unique_key == ""


class TestUniqueEnqueuePaths:
    def test_aenqueue_is_unique_too(self):
        a = async_to_sync(tasks.unique_by_args.aenqueue)("b@example.com")
        b = async_to_sync(tasks.unique_by_args.aenqueue)("b@example.com")
        c = tasks.unique_by_args.enqueue("b@example.com")
        assert a.id == b.id == c.id
        assert Job.objects.count() == 1 and DBTaskResult.objects.count() == 1

    def test_aenqueue_without_unique_still_works(self):
        result = async_to_sync(tasks.plain.aenqueue)(3)
        assert Run.objects.filter(result_id=str(result.id)).exists()

    def test_scheduled_unique_job_keeps_its_schedule(self):
        schedule = Schedule.objects.create(
            name="unique-mail",
            task_path="tests.tasks.unique_by_args",
            interval_seconds=60,
            args=["s@example.com"],
            next_run_at=timezone.now(),
        )
        job = enqueue_schedule(schedule)
        assert job.source == JobSource.SCHEDULE and job.schedule_id == schedule.pk
        assert job.unique_key

    def test_failed_recorder_does_not_leave_the_key_without_a_run(self):
        task_enqueued.disconnect(dispatch_uid="overseer.enqueued")
        try:
            result = tasks.unique_by_args.enqueue("r@example.com")
        finally:
            recorders._connected = False
            recorders.connect()
        job = Job.objects.get()
        assert job.runs.get().result_id == str(result.id)
        again = tasks.unique_by_args.enqueue("r@example.com")
        assert again.id == result.id and DBTaskResult.objects.count() == 1

    def test_stale_key_holder_without_runs_is_released(self):
        Job.objects.create(
            task_path="tests.tasks.unique_by_args",
            task_name="unique_by_args",
            backend="default",
            queue_name="emails",
            status=JobStatus.PENDING,
            unique_key=tasks.unique_by_args._unique_key(("z@example.com",), {}),
        )
        tasks.unique_by_args.enqueue("z@example.com")
        assert Job.objects.filter(status=JobStatus.FAILED).count() == 1
        assert Job.objects.filter(status=JobStatus.PENDING).get().runs.count() == 1


class TestManualRetryGuards:
    def test_succeeded_job_cannot_be_retried(self, worker):
        tasks.plain.enqueue(1)
        worker()
        with pytest.raises(ValueError, match="Only failed or cancelled"):
            retry.retry_job(Job.objects.get())
        assert Run.objects.count() == 1

    def test_missing_task_is_reported(self, worker):
        tasks.always_fails.enqueue()
        worker()
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(task_path="tests.tasks.renamed_away")
        job.refresh_from_db()
        with pytest.raises(ValueError, match="no longer exists"):
            retry.retry_job(job)

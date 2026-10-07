import pytest
from django.utils import timezone

from overseer import retry
from overseer.models import Job, JobSource, JobStatus, Run, RunStatus
from overseer.registry import TaskPolicy
from tests import tasks

pytestmark = pytest.mark.django_db


class TestComputeDelay:
    def test_strategies(self):
        exp = TaskPolicy(backoff="exponential", backoff_base=10, backoff_max=1000, jitter=False)
        assert [retry.compute_delay(exp, a) for a in (2, 3, 4, 5)] == [10, 20, 40, 80]
        lin = TaskPolicy(backoff="linear", backoff_base=10, jitter=False)
        assert [retry.compute_delay(lin, a) for a in (2, 3, 4)] == [10, 20, 30]
        const = TaskPolicy(backoff="constant", backoff_base=7, jitter=False)
        assert [retry.compute_delay(const, a) for a in (2, 3)] == [7, 7]

    def test_cap_and_jitter(self):
        p = TaskPolicy(backoff="exponential", backoff_base=100, backoff_max=150, jitter=False)
        assert retry.compute_delay(p, 10) == 150
        j = TaskPolicy(backoff="constant", backoff_base=100, jitter=True)
        samples = {retry.compute_delay(j, 2) for _ in range(50)}
        assert all(80 <= s <= 120 for s in samples) and len(samples) > 1

    def test_invalid_policy_rejected(self):
        with pytest.raises(ValueError):
            TaskPolicy(backoff="random")
        with pytest.raises(ValueError):
            TaskPolicy(retries=-1)


class TestAutomaticRetries:
    def test_fails_then_succeeds_within_budget(self, worker, job_of):
        result = tasks.flaky.enqueue(1)
        worker()  # attempt 1 fails -> retry scheduled 10s out
        job = job_of(result)
        assert job.status == JobStatus.PENDING and job.attempts == 1
        assert job.next_retry_at and job.next_retry_at > timezone.now()
        runs = list(job.runs.order_by("attempt"))
        assert [r.status for r in runs] == [RunStatus.FAILED, RunStatus.READY]
        assert runs[1].retry_of_id == runs[0].pk and runs[1].run_after == job.next_retry_at
        assert job.source == JobSource.RETRY

        # Make the retry due and run it.
        Run.objects.filter(pk=runs[1].pk).update(run_after=timezone.now())
        from django_tasks_db.models import DBTaskResult

        DBTaskResult.objects.filter(id=runs[1].result_id).update(run_after=timezone.now())
        worker()
        job.refresh_from_db()
        assert job.status == JobStatus.SUCCEEDED and job.attempts == 2
        assert job.runs.count() == 2 and job.next_retry_at is None

    def test_exhausted_budget_marks_failed(self, worker, job_of):
        result = tasks.flaky.enqueue(5)  # needs 5 failures, only 2 retries allowed
        from django_tasks_db.models import DBTaskResult

        for _ in range(3):
            DBTaskResult.objects.update(run_after=timezone.now())
            worker()
        job = job_of(result)
        assert job.status == JobStatus.FAILED and job.attempts == 3
        assert job.runs.count() == 3
        assert [r.status for r in job.runs.order_by("attempt")] == [RunStatus.FAILED] * 3

    def test_retry_on_filters_exceptions(self, worker, job_of):
        value = tasks.picky.enqueue("value")
        kind = tasks.picky.enqueue("type")
        worker()
        assert job_of(value).status == JobStatus.PENDING  # ValueError retried
        assert job_of(kind).status == JobStatus.FAILED  # TypeError not

    def test_defaults_from_settings_apply_to_plain_django_tasks(self, settings, worker, job_of):
        settings.OVERSEER_DEFAULT_RETRIES = 1
        settings.OVERSEER_DEFAULT_JITTER = False
        settings.OVERSEER_DEFAULT_BACKOFF_BASE = 1
        result = tasks.always_fails.enqueue()
        worker()
        job = job_of(result)
        assert job.max_retries == 1
        assert job.runs.count() == 2 and job.status == JobStatus.PENDING
        settings.OVERSEER_DEFAULT_RETRIES = 0
        assert job_of(tasks.picky.enqueue("type")).max_retries == 3  # declared policy wins

    def test_immediate_backend_retries_inline(self, job_of, caplog):
        result = tasks.flaky.using(backend="immediate").enqueue(1)
        job = job_of(result)
        assert job.status == JobStatus.SUCCEEDED and job.attempts == 2
        assert job.runs.count() == 2
        assert "cannot defer" in caplog.text


class TestManualRetry:
    def test_retry_failed_job(self, worker, job_of):
        result = tasks.picky.enqueue("type")
        worker()
        job = job_of(result)
        new_run = retry.retry_job(job)
        assert new_run.attempt == 2 and new_run.status == RunStatus.READY
        job.refresh_from_db()
        assert job.status == JobStatus.PENDING and job.source == JobSource.MANUAL

    def test_cannot_retry_active_job(self, job_of):
        job = job_of(tasks.plain.enqueue(1))
        with pytest.raises(ValueError):
            retry.retry_job(job)


class TestUnique:
    def test_identical_pending_enqueues_collapse(self, job_of):
        a = tasks.unique_by_args.enqueue("x@example.com")
        b = tasks.unique_by_args.enqueue("x@example.com")
        c = tasks.unique_by_args.enqueue("y@example.com")
        assert a.id == b.id and a.id != c.id
        assert Job.objects.count() == 2
        assert job_of(a).unique_key.startswith("tests.tasks.unique_by_args:")

    def test_unique_released_after_finish(self, worker):
        a = tasks.unique_by_args.enqueue("x@example.com")
        worker()
        b = tasks.unique_by_args.enqueue("x@example.com")
        assert a.id != b.id

    def test_callable_key(self, job_of):
        a = tasks.unique_by_callable.enqueue(7)
        b = tasks.unique_by_callable.enqueue(7, force=True)
        assert a.id == b.id
        assert job_of(a).unique_key == "report:7"

    def test_retry_keeps_unique_key(self, worker, job_of):
        result = tasks.flaky.enqueue(1)
        worker()
        job = job_of(result)
        assert all(r.job_id == job.pk for r in Run.objects.all())


class TestContextTask:
    def test_takes_context_attempt_number(self, worker, run_of):
        result = tasks.with_context.enqueue("v")
        worker()
        assert run_of(result).return_value == {"attempt": 1, "value": "v"}

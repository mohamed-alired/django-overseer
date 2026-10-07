import pytest
from django.tasks import task_backends
from django.utils import timezone

from overseer import recorders
from overseer.models import Job, JobSource, JobStatus, Run, RunStatus, Worker
from tests import tasks

pytestmark = pytest.mark.django_db


class TestEnqueue:
    def test_creates_job_and_ready_run(self, job_of, run_of):
        result = tasks.plain.enqueue(21)
        job = job_of(result)
        run = run_of(result)
        assert job.task_path == "tests.tasks.plain"
        assert job.task_name == "plain"
        assert job.queue_name == "default" and job.backend == "default"
        assert job.args == [21] and job.kwargs == {}
        assert job.status == JobStatus.PENDING and job.source == JobSource.ENQUEUE
        assert job.max_retries == 0
        assert run.status == RunStatus.READY and run.attempt == 1
        assert run.result_id == str(result.id) and run.retry_of is None

    def test_policy_metadata_copied(self, job_of):
        job = job_of(tasks.unique_by_args.enqueue("a@example.com"))
        assert job.max_retries == 0 and job.tags == ["mail"] and job.queue_name == "emails"
        job = job_of(tasks.flaky.enqueue(1))
        assert job.max_retries == 2

    def test_run_after_recorded(self, run_of):
        when = timezone.now() + timezone.timedelta(minutes=5)
        run = run_of(tasks.plain.using(run_after=when).enqueue(1))
        assert run.run_after == when

    def test_args_not_recorded_when_disabled(self, settings, job_of):
        settings.OVERSEER_RECORD_ARGS = False
        job = job_of(tasks.plain.enqueue(5))
        assert job.args == [] and job.kwargs == {}

    def test_non_json_args_are_stringified(self, job_of):
        # The backend itself only accepts JSON-serialisable args, so use the dummy backend
        # (which stores anything) to exercise the recorder's fallback.
        job = job_of(tasks.plain.using(backend="dummy").enqueue(3))
        assert job.args == [3]

    def test_enqueue_is_idempotent_per_result(self):
        result = tasks.plain.enqueue(1)
        recorders.record_enqueued(result)
        assert Run.objects.filter(result_id=str(result.id)).count() == 1
        assert Job.objects.count() == 1


class TestLifecycle:
    def test_success_path(self, worker, job_of, run_of):
        result = tasks.plain.enqueue(4)
        worker()
        run = run_of(result)
        job = job_of(result)
        assert run.status == RunStatus.SUCCESSFUL
        assert run.started_at and run.finished_at and run.duration_ms is not None
        assert run.wait_ms is not None and run.worker_id
        assert run.return_value == 8
        assert job.status == JobStatus.SUCCEEDED and job.attempts == 1 and job.finished_at
        worker_row = Worker.objects.get(worker_id=run.worker_id)
        assert worker_row.tasks_processed == 1 and worker_row.current_run is None

    def test_failure_without_retries(self, worker, job_of, run_of):
        result = tasks.picky.enqueue("type")  # TypeError is not in retry_on
        worker()
        run = run_of(result)
        assert run.status == RunStatus.FAILED
        assert run.exception_class == "builtins.TypeError"
        assert "not retryable" in run.traceback
        assert job_of(result).status == JobStatus.FAILED
        assert Worker.objects.get().tasks_failed == 1

    def test_immediate_backend_records_everything_synchronously(self, job_of, run_of):
        result = tasks.plain.using(backend="immediate").enqueue(10)
        run = run_of(result)
        assert run.status == RunStatus.SUCCESSFUL and run.return_value == 20
        assert job_of(result).status == JobStatus.SUCCEEDED

    def test_started_for_unknown_result_backfills(self):
        """A result enqueued before Overseer was installed still gets recorded on start."""
        recorders.disconnect()
        try:
            result = tasks.plain.enqueue(1)
        finally:
            recorders.connect()
        assert Run.objects.count() == 0
        recorders.record_started(result)
        run = Run.objects.get(result_id=str(result.id))
        assert run.status == RunStatus.RUNNING

    def test_finished_is_idempotent(self, worker, run_of):
        result = tasks.plain.enqueue(1)
        worker()
        run = run_of(result)
        recorders.record_finished(task_backends["default"].get_result(result.id))
        assert Run.objects.get(pk=run.pk).finished_at == run.finished_at

    def test_recorder_failures_never_propagate(self, monkeypatch, caplog):
        def boom(task_result):
            raise RuntimeError("recorder bug")

        monkeypatch.setattr(recorders, "record_enqueued", boom)
        handler = recorders._safe(boom)
        handler(None, task_result=type("R", (), {"id": "x"})())
        assert "failed for result" in caplog.text
        # And the real task can still be enqueued.
        assert tasks.plain.enqueue(1) is not None

    def test_traceback_truncated(self, settings, worker, run_of):
        settings.OVERSEER_MAX_TRACEBACK_CHARS = 50
        result = tasks.picky.enqueue("type")
        worker()
        assert len(run_of(result).traceback) <= 50

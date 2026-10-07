from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from overseer import rescue
from overseer.adapters.base import GenericAdapter, get_adapter
from overseer.adapters.django_tasks_db import DatabaseAdapter
from overseer.exceptions import AdapterUnsupported
from overseer.models import Job, JobStatus, Run, RunStatus
from overseer.scheduling.scheduler import Scheduler
from tests import tasks

pytestmark = pytest.mark.django_db


def start(result, minutes_ago):
    """Pretend the worker started this result ``minutes_ago`` and never finished."""
    from django_tasks_db.models import DBTaskResult

    started = timezone.now() - timedelta(minutes=minutes_ago)
    DBTaskResult.objects.filter(id=result.id).update(status="RUNNING", started_at=started)
    run = Run.objects.get(result_id=str(result.id))
    run.status = RunStatus.RUNNING
    run.started_at = started
    run.worker_id = "w1"
    run.save()
    Job.objects.filter(pk=run.job_id).update(status=JobStatus.RUNNING)
    return run


class TestStaleDetection:
    def test_policy_timeout_and_default(self, settings):
        settings.OVERSEER_STALE_AFTER = 600
        timed = start(tasks.with_context.enqueue("v"), minutes_ago=1)  # timeout=30s
        plain = start(tasks.plain.enqueue(1), minutes_ago=5)  # default 600s, not yet
        old = start(tasks.plain.enqueue(2), minutes_ago=11)
        stale = {r.pk for r in rescue.stale_runs()}
        assert stale == {timed.pk, old.pk}
        assert plain.pk not in stale

    def test_deadline_none_without_started_at(self, run_of):
        run = run_of(tasks.plain.enqueue(1))
        assert rescue.deadline_for(run) is None


class TestAbandon:
    def test_marks_abandoned_resets_backend_and_fails_job(self, settings):
        from django_tasks_db.models import DBTaskResult

        settings.OVERSEER_STALE_AFTER = 1
        run = start(tasks.plain.enqueue(1), minutes_ago=2)
        (abandoned,) = rescue.rescue()
        abandoned.refresh_from_db()
        assert abandoned.status == RunStatus.ABANDONED
        assert abandoned.exception_class == "overseer.exceptions.RunAbandoned"
        assert abandoned.duration_ms and abandoned.finished_at
        db = DBTaskResult.objects.get(id=run.result_id)
        assert db.status == "FAILED" and "RunAbandoned" in db.exception_class_path
        assert Job.objects.get(pk=run.job_id).status == JobStatus.FAILED

    def test_abandoned_run_is_retried_by_policy(self):
        run = start(tasks.flaky.enqueue(0), minutes_ago=90)  # retries=2, default stale 3600s
        rescue.rescue()
        job = Job.objects.get(pk=run.job_id)
        assert job.status == JobStatus.PENDING and job.attempts == 1
        assert job.runs.filter(status=RunStatus.READY, attempt=2).exists()

    def test_abandon_is_idempotent(self, settings):
        settings.OVERSEER_STALE_AFTER = 1
        run = start(tasks.plain.enqueue(1), minutes_ago=2)
        rescue.abandon(run)
        rescue.abandon(run)
        assert Run.objects.get(pk=run.pk).status == RunStatus.ABANDONED
        assert Run.objects.count() == 1

    def test_generic_backend_logs_unsupported(self, settings, caplog):
        settings.OVERSEER_STALE_AFTER = 1
        result = tasks.plain.using(backend="dummy").enqueue(1)
        run = Run.objects.get(result_id=str(result.id))
        run.status = RunStatus.RUNNING
        run.started_at = timezone.now() - timedelta(minutes=5)
        run.save()
        rescue.rescue()
        assert "cannot reset abandoned run" in caplog.text
        assert Run.objects.get(pk=run.pk).status == RunStatus.ABANDONED

    def test_command(self, settings, capsys):
        settings.OVERSEER_STALE_AFTER = 1
        start(tasks.plain.enqueue(1), minutes_ago=2)
        call_command("overseer_rescue")
        assert "Abandoned 1 stale run(s)." in capsys.readouterr().out

    def test_scheduler_loop_runs_rescue(self, settings, monkeypatch):
        settings.OVERSEER_STALE_AFTER = 1
        start(tasks.plain.enqueue(1), minutes_ago=2)
        Scheduler(interval=0.01).run(max_ticks=1)
        assert Run.objects.get().status == RunStatus.ABANDONED


class TestAdapters:
    def test_selection(self):
        assert isinstance(get_adapter("default"), DatabaseAdapter)
        assert isinstance(get_adapter("immediate"), GenericAdapter)

    def test_generic_raises_unsupported(self, run_of):
        run = run_of(tasks.plain.using(backend="dummy").enqueue(1))
        adapter = get_adapter("dummy")
        assert adapter.queue_depth() is None
        with pytest.raises(AdapterUnsupported):
            adapter.cancel(run)
        with pytest.raises(AdapterUnsupported):
            adapter.reset(run)

    def test_database_cancel_only_ready(self, run_of):
        from django_tasks_db.models import DBTaskResult

        run = run_of(tasks.plain.enqueue(1))
        adapter = get_adapter("default")
        assert adapter.queue_depth() == 1 and adapter.queue_depth("default") == 1
        assert adapter.queue_depth("emails") == 0
        assert adapter.cancel(run) is True
        assert not DBTaskResult.objects.filter(id=run.result_id).exists()
        assert adapter.cancel(run) is False
        running = start(tasks.plain.enqueue(2), minutes_ago=1)
        assert adapter.cancel(running) is False
        assert adapter.reset(running) is True and adapter.reset(running) is False

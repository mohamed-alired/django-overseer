"""Regression tests for the bugs fixed in 0.1.2 (core, scheduling, alerts)."""

from datetime import timedelta

import pytest
from django.db import connection
from django.tasks import task_backends
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from overseer import alerts, metrics, rescue, signals, stats
from overseer.models import Job, JobStatus, MetricBucket, Run, RunStatus, Worker
from overseer.scheduling import scheduler
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


class TestRollupPortability:
    def test_rollup_without_upsert_support(self, monkeypatch, worker):
        tasks.plain.enqueue(1)
        worker()
        monkeypatch.setattr(connection.features, "supports_update_conflicts_with_target", False)
        monkeypatch.setattr(connection.features, "supports_update_conflicts", False)
        assert metrics.rollup() > 0
        first = MetricBucket.objects.count()
        assert metrics.rollup() > 0  # a second pass rewrites, it does not duplicate
        assert MetricBucket.objects.count() == first

    def test_concurrent_writer_without_upsert_is_tolerated(self, monkeypatch, worker):
        from django.db.models.query import QuerySet

        tasks.plain.enqueue(1)
        worker()
        monkeypatch.setattr(connection.features, "supports_update_conflicts_with_target", False)
        assert metrics.rollup() > 0
        written = MetricBucket.objects.count()
        # Another scheduler committed the same minutes between this one's delete and insert:
        # simulate by making the delete a no-op, so the insert collides.
        original_delete = QuerySet.delete

        def delete(qs):
            return (0, {}) if qs.model is MetricBucket else original_delete(qs)

        monkeypatch.setattr(QuerySet, "delete", delete)
        assert metrics.rollup() == 0
        assert MetricBucket.objects.count() == written


class TestJobFailedReceivers:
    def test_raising_receiver_does_not_undo_the_failure(self, worker, caplog):
        def boom(**kwargs):
            raise RuntimeError("mail server down")

        signals.job_failed.connect(boom, dispatch_uid="t012-boom")
        try:
            tasks.always_fails.enqueue()
            worker()
        finally:
            signals.job_failed.disconnect(dispatch_uid="t012-boom")
        run = Run.objects.get()
        assert run.status == RunStatus.FAILED and run.job.status == JobStatus.FAILED
        assert "always" in run.traceback
        assert "job_failed receiver" in caplog.text

    def test_rescue_survives_a_raising_receiver(self, worker):
        def boom(**kwargs):
            raise RuntimeError("receiver broke")

        tasks.slow.enqueue(0)
        job = Job.objects.get()
        run = job.runs.get()
        Run.objects.filter(pk=run.pk).update(
            status=RunStatus.RUNNING, started_at=timezone.now() - timedelta(hours=3)
        )
        Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING)
        signals.job_failed.connect(boom, dispatch_uid="t012-boom2")
        try:
            abandoned = rescue.rescue()
        finally:
            signals.job_failed.disconnect(dispatch_uid="t012-boom2")
        assert [r.pk for r in abandoned] == [run.pk]
        assert Job.objects.get().status == JobStatus.FAILED


class TestZeroArgumentRetries:
    def test_retried_when_arguments_are_not_recorded(self, settings):
        settings.OVERSEER_RECORD_ARGS = False
        tasks.zero_arg_flaky.enqueue()
        job = Job.objects.get()
        assert job.status == JobStatus.SUCCEEDED and job.attempts == 2

    def test_task_that_needs_arguments_is_still_refused(self, settings, worker):
        settings.OVERSEER_RECORD_ARGS = False
        tasks.child.enqueue(7)
        worker()
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
        DBTaskResult.objects.all().delete()
        from overseer.retry import retry_job

        with pytest.raises(ValueError, match="not recorded"):
            retry_job(job)


class TestWaitingRunsAreReconciled:
    def age(self, minutes=5):
        Run.objects.update(enqueued_at=timezone.now() - timedelta(minutes=minutes))

    def test_deleted_backend_row_marks_the_run_lost(self, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = 60
        tasks.unique_by_args.enqueue("lost@example.com")
        DBTaskResult.objects.all().delete()
        self.age()
        report = rescue.reconcile_waiting()
        assert report == {"lost": 1, "recorded": 0}
        run = Run.objects.get()
        assert run.status == RunStatus.CANCELLED and run.job.status == JobStatus.CANCELLED
        assert "RunLost" in run.exception_class
        assert stats.overview(15)["oldest_wait_seconds"] == 0
        assert [c.kind for c in alerts.detect()] == []
        # The unique key is free again, and the job can be retried from the dashboard.
        tasks.unique_by_args.enqueue("lost@example.com")
        assert Job.objects.filter(status=JobStatus.PENDING).count() == 1
        assert Job.objects.get(status=JobStatus.CANCELLED).can_retry

    def test_unique_enqueue_tolerates_a_lost_run(self):
        first = tasks.unique_by_args.enqueue("gone@example.com")
        DBTaskResult.objects.all().delete()
        second = tasks.unique_by_args.enqueue("gone@example.com")
        assert second.id != first.id
        assert Job.objects.filter(status=JobStatus.CANCELLED).count() == 1

    def test_finished_result_without_a_signal_is_recorded(self):
        result = tasks.plain.enqueue(4)
        DBTaskResult.objects.filter(pk=result.id).update(
            status="SUCCESSFUL",
            started_at=timezone.now(),
            finished_at=timezone.now(),
            return_value=8,
        )
        self.age()
        assert rescue.reconcile_waiting()["recorded"] == 1
        run = Run.objects.get()
        assert run.status == RunStatus.SUCCESSFUL and run.job.status == JobStatus.SUCCEEDED

    def test_recent_and_genuinely_waiting_runs_are_left_alone(self):
        tasks.plain.enqueue(1)
        assert rescue.reconcile_waiting() == {"lost": 0, "recorded": 0}
        self.age()
        assert rescue.reconcile_waiting() == {"lost": 0, "recorded": 0}
        assert Run.objects.get().status == RunStatus.READY

    def test_scheduler_rescue_pass_reconciles(self):
        tasks.plain.enqueue(1)
        DBTaskResult.objects.all().delete()
        self.age()
        rescue.rescue()
        assert Run.objects.get().status == RunStatus.CANCELLED


class TestUniqueOnBackendsWithoutResults:
    def test_recursive_unique_on_immediate_backend_runs(self):
        tasks.unique_recursive.enqueue(1)
        jobs = Job.objects.filter(task_path="tests.tasks.unique_recursive")
        assert set(jobs.values_list("status", flat=True)) == {JobStatus.SUCCEEDED}
        assert all(j.runs.get().status == RunStatus.SUCCESSFUL for j in jobs)

    def test_dummy_backend_forgetting_results(self):
        tasks.unique_dummy.enqueue(5)
        task_backends["dummy"].clear()
        tasks.unique_dummy.enqueue(5)  # must not raise
        assert Job.objects.filter(task_path="tests.tasks.unique_dummy").count() == 2


class TestSilentWorkers:
    def test_plain_workers_that_stopped_running_tasks_drop_out_of_the_counts(self, settings):
        settings.OVERSEER_SILENT_WORKER_AFTER = 600
        for i in range(3):
            Worker.objects.create(
                worker_id=f"old-{i}", last_seen_at=timezone.now() - timedelta(hours=2)
            )
        Worker.objects.create(worker_id="recent")
        Worker.objects.create(worker_id="beating", heartbeat_seconds=10)
        counts = stats.worker_counts()
        assert counts == {
            "workers_online": 1,
            "workers_total": 2,
            "workers_without_heartbeat": 1,
        }
        states = {w["worker_id"]: w["state"] for w in stats.workers()}
        assert states["old-0"] == "silent" and states["recent"] == "no heartbeat"


class TestScheduleSyncRevival:
    def test_broken_schedule_fixed_in_code_is_enabled_again(self):
        from overseer.scheduling import registry
        from overseer.scheduling.sync import sync_schedules

        sync_schedules()
        row = Job.objects.none()  # placeholder to keep the linter quiet
        del row
        from overseer.models import Schedule

        s = Schedule.objects.get(name="heartbeat")
        scheduler.disable_broken(s, ValueError("unknown timezone 'X/Y'"))
        assert Schedule.objects.get(name="heartbeat").enabled is False
        report = sync_schedules()
        assert "heartbeat" in report["enabled"]
        s.refresh_from_db()
        assert s.enabled and s.last_error == "" and s.next_run_at is not None
        assert registry.all_specs()["heartbeat"]

    def test_enable_all_option_for_rows_disabled_before_0_1_1(self):
        from django.core.management import call_command

        from overseer.models import Schedule
        from overseer.scheduling.sync import sync_schedules

        sync_schedules()
        Schedule.objects.filter(name="heartbeat").update(enabled=False)  # no marker: 0.1.0
        call_command("overseer_sync_schedules", verbosity=0)
        assert Schedule.objects.get(name="heartbeat").enabled is False  # looks like a pause
        call_command("overseer_sync_schedules", "--enable", verbosity=0)
        assert Schedule.objects.get(name="heartbeat").enabled is True


class TestPruneSilentWorkers:
    def test_silent_plain_workers_are_pruned_after_a_day(self):
        from overseer.prune import prune

        old = timezone.now() - timedelta(days=2)
        Worker.objects.create(worker_id="plain-gone", last_seen_at=old)
        Worker.objects.create(worker_id="plain-fresh")
        Worker.objects.create(worker_id="beat-quiet", heartbeat_seconds=10, last_seen_at=old)
        assert prune()["workers"] == 1  # heartbeat workers keep the 14-day retention
        assert set(Worker.objects.values_list("worker_id", flat=True)) == {
            "plain-fresh",
            "beat-quiet",
        }


class TestSecondReviewFindings:
    def test_task_with_optional_parameters_is_not_retried_blind(self, settings, worker):
        settings.OVERSEER_RECORD_ARGS = False
        tasks.optional_arg.enqueue(5)
        worker()
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
        DBTaskResult.objects.all().delete()
        from overseer.retry import retry_job

        with pytest.raises(ValueError, match="not recorded"):
            retry_job(job)

    def test_reconciliation_looks_runs_up_in_bulk(self, django_assert_max_num_queries):
        for i in range(30):
            tasks.plain.enqueue(i)
        Run.objects.update(enqueued_at=timezone.now() - timedelta(minutes=5))
        with django_assert_max_num_queries(4):
            assert rescue.reconcile_waiting() == {"lost": 0, "recorded": 0}

    def test_lost_running_key_holder_is_abandoned_and_the_key_moves_on(self, worker):
        tasks.unique_by_args.enqueue("hold@example.com")
        run = Run.objects.get()
        Run.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, started_at=timezone.now())
        Job.objects.filter(pk=run.job_id).update(status=JobStatus.RUNNING)
        DBTaskResult.objects.all().delete()
        result = tasks.unique_by_args.enqueue("hold@example.com")  # must not raise
        assert result is not None
        run.refresh_from_db()
        assert run.status == RunStatus.ABANDONED

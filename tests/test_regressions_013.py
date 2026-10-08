"""Regression tests for the bugs fixed in 0.1.3 (found reviewing 0.1.2)."""

import io
from datetime import timedelta

import pytest
from django.contrib.messages import get_messages
from django.core.checks import run_checks
from django.core.management import CommandError, call_command
from django.db import connection
from django.tasks import task_backends
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from overseer import alerts, metrics, rescue
from overseer.adapters.django_tasks_db import DatabaseAdapter
from overseer.models import Job, JobStatus, MetricBucket, Run, RunStatus, Schedule, Worker
from overseer.scheduling import registry, scheduler
from overseer.scheduling.sync import sync_schedules
from overseer.worker import HeartbeatWorker
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def make_worker(worker_id, heartbeat=0.05):
    return HeartbeatWorker(
        queue_names=["*"],
        interval=0.0,
        batch=True,
        backend_name="default",
        startup_delay=False,
        max_tasks=None,
        worker_id=worker_id,
        excluded_queue_names=[],
        heartbeat=heartbeat,
    )


def age_runs(minutes=5):
    Run.objects.update(enqueued_at=timezone.now() - timedelta(minutes=minutes))


def messages_of(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class TestWorkers:
    def test_reconciling_an_old_result_does_not_revive_a_stopped_worker(self):
        stopped = timezone.now() - timedelta(hours=3)
        Worker.objects.create(
            worker_id="hb-old", heartbeat_seconds=0.2, hostname="h1", pid=1,
            started_at=stopped - timedelta(hours=1), last_seen_at=stopped, stopped_at=stopped,
        )  # fmt: skip
        result = tasks.plain.enqueue(4)
        DBTaskResult.objects.filter(pk=result.id).update(
            status="SUCCESSFUL", started_at=stopped - timedelta(minutes=5),
            finished_at=stopped - timedelta(minutes=4), return_value=8, worker_ids=["hb-old"],
        )  # fmt: skip
        age_runs()
        assert rescue.reconcile_waiting()["recorded"] == 1
        assert Worker.objects.get(worker_id="hb-old").stopped_at == stopped
        assert "worker_offline" not in [c.kind for c in alerts.detect()]

    def test_a_worker_run_twice_records_both_stops(self):
        w = make_worker("twice")
        w.run()
        assert Worker.objects.get(worker_id="twice").stopped_at is not None
        tasks.plain.enqueue(1)
        w.run()
        row = Worker.objects.get(worker_id="twice")
        assert row.stopped_at is not None and row.tasks_processed == 1

    def test_beat_upgrades_a_row_the_recorder_created(self):
        Worker.objects.create(worker_id="hb-late", backend="default")
        make_worker("hb-late").beat()
        row = Worker.objects.get(worker_id="hb-late")
        assert row.heartbeat_seconds == 0.05 and row.pid

    def test_an_interrupted_attempt_is_not_counted_as_a_task_failure(self):
        tasks.exits_once.enqueue()
        job = Job.objects.get()
        statuses = [r.status for r in job.runs.order_by("attempt")]
        assert statuses == [RunStatus.ABANDONED, RunStatus.SUCCESSFUL]
        w = Worker.objects.get(worker_id=task_backends["immediate"].worker_id)
        assert w.tasks_failed == 0


class TestLostRuns:
    def test_a_lost_run_whose_task_runs_after_all_is_recorded(self, worker):
        result = tasks.plain.enqueue(7)
        run = Run.objects.get()
        rescue.mark_lost(run)
        assert Job.objects.get().status == JobStatus.CANCELLED
        worker()
        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL and run.exception_class == ""
        assert Job.objects.get().status == JobStatus.SUCCEEDED
        assert DBTaskResult.objects.get(pk=result.id).status == "SUCCESSFUL"

    def test_backend_rows_on_another_database_get_the_long_grace(self, monkeypatch, settings):
        monkeypatch.setattr(DatabaseAdapter, "storage_alias", lambda self: "elsewhere")
        settings.OVERSEER_STALE_AFTER = 3600
        tasks.plain.enqueue(1)
        DBTaskResult.objects.all().delete()  # not visible yet from Overseer's side
        age_runs(5)
        assert rescue.reconcile_waiting()["lost"] == 0
        assert Run.objects.get().status == RunStatus.READY
        age_runs(61)
        assert rescue.reconcile_waiting()["lost"] == 1

    def test_lost_retry_keeps_the_attempt_count(self, worker):
        tasks.flaky.enqueue(1)
        worker()  # attempt 1 fails, attempt 2 is deferred by the backoff
        job = Job.objects.get()
        assert job.attempts == 1 and job.runs.count() == 2
        DBTaskResult.objects.all().delete()
        age_runs()
        rescue.reconcile_waiting()
        job.refresh_from_db()
        assert job.status == JobStatus.CANCELLED and job.attempts == 2


class TestUnique:
    def test_a_finished_result_whose_signal_was_lost_does_not_block_the_key(self):
        first = tasks.unique_by_args.enqueue("stale@example.com")
        DBTaskResult.objects.filter(pk=first.id).update(
            status="SUCCESSFUL", started_at=timezone.now(), finished_at=timezone.now(),
            return_value="x",
        )  # fmt: skip
        second = tasks.unique_by_args.enqueue("stale@example.com")
        assert second.id != first.id
        statuses = list(Job.objects.order_by("created_at").values_list("status", flat=True))
        assert statuses == [JobStatus.SUCCEEDED, JobStatus.PENDING]


class TestDashboard:
    def failed_job(self, worker):
        tasks.always_fails.enqueue()
        worker()
        return Job.objects.get()

    @pytest.mark.parametrize(
        "target", ["failed", "overseer:jobs", "//evil.example/x", "https://evil.example/"]
    )
    def test_next_that_is_not_a_local_path_goes_to_the_default(self, staff_client, worker, target):
        job = self.failed_job(worker)
        resp = staff_client.post(reverse("overseer:job-dismiss", args=[job.pk]), {"next": target})
        assert resp.status_code == 302 and resp.url == reverse("overseer:failed")

    def test_local_next_is_kept(self, staff_client, worker):
        job = self.failed_job(worker)
        target = reverse("overseer:job", args=[job.pk])
        resp = staff_client.post(reverse("overseer:job-dismiss", args=[job.pk]), {"next": target})
        assert resp.url == target

    @pytest.mark.parametrize("limit", [None, 0, -3, "many", True])
    def test_retry_all_with_a_bad_limit_uses_the_default(
        self, staff_client, worker, settings, limit
    ):
        settings.OVERSEER_RETRY_ALL_LIMIT = limit
        self.failed_job(worker)
        resp = staff_client.post(reverse("overseer:failed-retry-all"))
        assert resp.status_code == 302
        assert messages_of(resp) == ["Retried 1 job(s)."]

    def test_retry_all_does_not_walk_jobs_of_a_task_that_is_gone(self, staff_client, settings):
        settings.OVERSEER_RETRY_ALL_LIMIT = 200
        now = timezone.now()
        jobs = Job.objects.bulk_create(
            Job(task_path="gone.module.task", task_name="t", backend="default",
                queue_name="default", status=JobStatus.FAILED, attempts=1, finished_at=now)
            for _ in range(500)
        )  # fmt: skip
        Run.objects.bulk_create(
            Run(job=j, result_id=f"gone-{i}", backend="default", status=RunStatus.FAILED,
                enqueued_at=now, finished_at=now)
            for i, j in enumerate(jobs)
        )  # fmt: skip
        with CaptureQueriesContext(connection) as ctx:
            resp = staff_client.post(reverse("overseer:failed-retry-all"))
        assert messages_of(resp) == ["Retried 0 job(s). 500 could not be retried."]
        assert len(ctx) < 30

    def test_actions_work_without_the_messages_framework(self, staff_client, worker, settings):
        job = self.failed_job(worker)
        settings.MIDDLEWARE = [
            m for m in settings.MIDDLEWARE if not m.endswith("MessageMiddleware")
        ]
        resp = staff_client.post(reverse("overseer:job-retry", args=[job.pk]))
        assert resp.status_code == 302
        assert job.runs.count() == 2
        assert "overseer.W004" in [c.id for c in run_checks()]

    def test_dashboard_setup_check_is_quiet_on_a_full_setup(self):
        assert "overseer.W004" not in [c.id for c in run_checks()]


class TestCommands:
    def broken_declaration(self, monkeypatch):
        sync_schedules()
        s = Schedule.objects.get(name="tests.tasks.scheduled_every_5")
        broken = registry.ScheduleSpec(name=s.name, task_path=s.task_path, cron="0 0 31 2 *")
        monkeypatch.setattr(registry, "all_specs", lambda: {s.name: broken})

    def test_sync_command_fails_when_a_schedule_has_an_error(self, monkeypatch):
        self.broken_declaration(monkeypatch)
        out = io.StringIO()
        with pytest.raises(CommandError):
            call_command("overseer_sync_schedules", stdout=out)
        assert "scheduled_every_5" in out.getvalue()

    def test_scheduler_once_fails_when_a_schedule_has_an_error(self, monkeypatch):
        self.broken_declaration(monkeypatch)
        with pytest.raises(CommandError):
            call_command("overseer_scheduler", "--once", stdout=io.StringIO())

    @pytest.mark.parametrize("raw", ["2026-13-45", "2026-02-30T10:00:00", "2026-13-01T00:00"])
    def test_rollup_rejects_impossible_dates(self, raw):
        with pytest.raises(CommandError, match="not a valid date"):
            call_command("overseer_rollup", "--since", raw, verbosity=0)


class TestScheduling:
    def test_sync_reports_a_repaired_row_as_updated(self):
        sync_schedules()
        s = Schedule.objects.get(name="tests.tasks.scheduled_every_5")
        Schedule.objects.filter(pk=s.pk).update(next_run_at=None)
        report = sync_schedules()
        assert s.name in report["updated"]
        assert Schedule.objects.get(pk=s.pk).next_run_at is not None


class TestAlertsAndMetrics:
    def test_window_label_counts_all_minutes(self, settings):
        settings.OVERSEER_ALERT_WINDOW_MINUTES = 1500
        settings.OVERSEER_ALERT_FAILURE_RATE = 0.1
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        for i in range(5):
            Run.objects.create(
                job=job, result_id=f"r{i}", backend="default", status=RunStatus.FAILED,
                finished_at=timezone.now(),
            )  # fmt: skip
        (cond,) = [c for c in alerts.detect() if c.kind == "failure_rate"]
        assert "1500 min" in cond.message

    def enqueued_total(self):
        return sum(b.enqueued for b in MetricBucket.objects.filter(task_path=""))

    def test_a_run_committed_late_is_still_counted(self):
        now = timezone.now()
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        Run.objects.create(job=job, result_id="a", backend="default", enqueued_at=now)
        metrics.rollup()
        Run.objects.create(
            job=job, result_id="b", backend="default", enqueued_at=now - timedelta(minutes=10)
        )
        metrics.rollup()
        assert self.enqueued_total() == 2

    def test_the_scheduler_recomputes_further_back_once_an_hour(self):
        now = timezone.now()
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        Run.objects.create(job=job, result_id="a", backend="default", enqueued_at=now)
        metrics.rollup()
        Run.objects.create(
            job=job, result_id="b", backend="default", enqueued_at=now - timedelta(hours=1)
        )
        s = scheduler.Scheduler(interval=0.01)
        s._step(sync=False, rescue_due=False, maintenance_due=True)
        assert self.enqueued_total() == 2


class TestReloaderStop:
    def test_the_reloader_parent_records_the_stop_it_causes(self, monkeypatch):
        from django.utils.autoreload import DJANGO_AUTORELOAD_ENV

        from overseer.management.commands import overseer_worker

        def killed_by_sigterm(main_func):
            # The child registered, then the parent got SIGTERM and killed it.
            Worker.objects.create(worker_id="reloading", heartbeat_seconds=10)
            raise SystemExit(0)

        monkeypatch.delenv(DJANGO_AUTORELOAD_ENV, raising=False)
        monkeypatch.setattr(overseer_worker, "run_with_reloader", killed_by_sigterm)
        with pytest.raises(SystemExit):
            call_command("overseer_worker", reload=True, worker_id="reloading", verbosity=0)
        assert Worker.objects.get(worker_id="reloading").stopped_at is not None

    def test_a_recorded_stop_is_kept(self):
        from overseer.worker import record_stop

        stopped = timezone.now() - timedelta(minutes=5)
        Worker.objects.create(worker_id="done", stopped_at=stopped)
        record_stop("done")
        assert Worker.objects.get(worker_id="done").stopped_at == stopped

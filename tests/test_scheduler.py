import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command
from django.db import connection, connections
from django.utils import timezone
from freezegun import freeze_time

from overseer.models import Job, JobSource, JobStatus, Schedule
from overseer.scheduling import registry, scheduler
from overseer.scheduling.sync import sync_schedules
from tests import tasks

pytestmark = pytest.mark.django_db


def at(*args):
    return datetime(*args, tzinfo=UTC)


class TestComputeNextRun:
    def test_cron_interval_and_timezone(self):
        s = Schedule(name="c", task_path="tests.tasks.plain", cron="0 9 * * *")
        assert scheduler.compute_next_run(s, at(2026, 3, 1, 10)) == at(2026, 3, 2, 9)
        s.timezone = "Asia/Tokyo"
        assert scheduler.compute_next_run(s, at(2026, 3, 1, 10)) == at(2026, 3, 2, 0)  # 09:00 JST
        i = Schedule(name="i", task_path="tests.tasks.plain", interval_seconds=90)
        assert scheduler.compute_next_run(i, at(2026, 3, 1, 10)) == at(2026, 3, 1, 10, 1, 30)
        with pytest.raises(ValueError):
            scheduler.compute_next_run(Schedule(name="x", task_path="tests.tasks.plain"))

    def test_settings_timezone_fallback(self, settings):
        settings.OVERSEER_SCHEDULER_TIMEZONE = "Europe/Berlin"
        s = Schedule(name="c", task_path="tests.tasks.plain", cron="0 9 * * *")
        assert scheduler.compute_next_run(s, at(2026, 6, 1, 10)) == at(2026, 6, 2, 7)


class TestSync:
    def test_creates_rows_from_declarations(self):
        with freeze_time("2026-01-01 12:03:00"):
            report = sync_schedules()
        assert set(report["created"]) >= {"tests.tasks.scheduled_every_5", "heartbeat"}
        s = Schedule.objects.get(name="tests.tasks.scheduled_every_5")
        assert s.cron == "*/5 * * * *" and s.kwargs == {"x": 1} and s.declared_in_code
        assert s.next_run_at == at(2026, 1, 1, 12, 5)
        hb = Schedule.objects.get(name="heartbeat")
        assert hb.interval_seconds == 60 and hb.next_run_at == at(2026, 1, 1, 12, 4)

    def test_second_sync_is_unchanged_and_keeps_state(self):
        sync_schedules()
        s = Schedule.objects.get(name="heartbeat")
        s.enabled = False
        s.runs_count = 7
        s.save()
        report = sync_schedules()
        assert "heartbeat" in report["unchanged"] and not report["created"]
        s.refresh_from_db()
        assert s.enabled is False and s.runs_count == 7

    def test_changed_declaration_updates_and_recomputes(self):
        sync_schedules()
        spec = registry.all_specs()["heartbeat"]
        try:
            registry.register(**{**vars(spec), "interval_seconds": 3600})
            with freeze_time("2026-01-01 12:00:00"):
                report = sync_schedules()
            assert "heartbeat" in report["updated"]
            s = Schedule.objects.get(name="heartbeat")
            assert s.interval_seconds == 3600 and s.next_run_at == at(2026, 1, 1, 13)
        finally:
            registry.register(**vars(spec))

    def test_removed_declaration_is_disabled_not_deleted(self):
        sync_schedules()
        spec = registry.all_specs()["heartbeat"]
        registry._schedules.pop("heartbeat")
        try:
            report = sync_schedules()
        finally:
            registry.register(**vars(spec))
        assert report["disabled"] == ["heartbeat"]
        assert Schedule.objects.get(name="heartbeat").enabled is False

    def test_hand_made_rows_untouched(self):
        manual = Schedule.objects.create(
            name="manual", task_path="tests.tasks.plain", cron="0 * * * *", args=[1]
        )
        sync_schedules()
        manual.refresh_from_db()
        assert manual.enabled and manual.declared_in_code is False

    def test_command(self, capsys):
        call_command("overseer_sync_schedules")
        out = capsys.readouterr().out
        assert "created:" in out and "Schedules synced." in out


class TestTick:
    @pytest.fixture
    def due(self):
        return Schedule.objects.create(
            name="due",
            task_path="tests.tasks.plain",
            interval_seconds=300,
            args=[5],
            next_run_at=timezone.now() - timedelta(seconds=1),
        )

    def test_fires_due_schedule(self, due):
        jobs = scheduler.tick()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.task_path == "tests.tasks.plain" and job.args == [5]
        assert job.source == JobSource.SCHEDULE and job.schedule_id == due.pk
        due.refresh_from_db()
        assert due.last_job_id == job.pk and due.runs_count == 1 and due.last_run_at
        assert due.next_run_at > timezone.now() + timedelta(seconds=290)
        assert scheduler.tick() == []  # not due again

    def test_skips_disabled_and_future(self, due):
        Schedule.objects.create(
            name="later",
            task_path="tests.tasks.plain",
            interval_seconds=10,
            next_run_at=timezone.now() + timedelta(hours=1),
        )
        due.enabled = False
        due.save()
        assert scheduler.tick() == []

    def test_overrides_queue_priority_backend(self):
        past = timezone.now() - timedelta(minutes=1)
        Schedule.objects.create(
            name="o",
            task_path="tests.tasks.plain",
            cron="* * * * *",
            args=[1],
            queue_name="reports",
            priority=7,
            next_run_at=past,
        )
        Schedule.objects.create(
            name="d",
            task_path="tests.tasks.plain",
            cron="* * * * *",
            backend="dummy",
            next_run_at=past,
        )
        jobs = {j.schedule.name: j for j in scheduler.tick()}
        assert jobs["o"].queue_name == "reports" and jobs["o"].priority == 7
        assert jobs["d"].backend == "dummy"

    def test_missed_occurrences_fire_once(self):
        s = Schedule.objects.create(
            name="old",
            task_path="tests.tasks.plain",
            interval_seconds=60,
            next_run_at=timezone.now() - timedelta(days=3),
        )
        assert len(scheduler.tick()) == 1
        s.refresh_from_db()
        assert s.next_run_at > timezone.now()

    def test_enqueue_failure_advances_schedule(self, caplog):
        s = Schedule.objects.create(
            name="broken",
            task_path="tests.tasks.does_not_exist",
            interval_seconds=60,
            next_run_at=timezone.now() - timedelta(seconds=1),
        )
        assert scheduler.tick() == []
        s.refresh_from_db()
        assert s.next_run_at > timezone.now() and s.runs_count == 0
        assert "could not enqueue" in caplog.text

    def test_run_now_does_not_move_next_run(self, due):
        due.next_run_at = timezone.now() + timedelta(hours=1)
        due.save()
        job = scheduler.run_now(due)
        due.refresh_from_db()
        assert job.source == JobSource.MANUAL and due.runs_count == 1
        assert due.next_run_at > timezone.now() + timedelta(minutes=59)

    def test_scheduled_job_runs_end_to_end(self, due, worker):
        (job,) = scheduler.tick()
        worker()
        assert Job.objects.get(pk=job.pk).status == JobStatus.SUCCEEDED
        assert tasks.CALLS == [("plain", 5)]


class TestSchedulerLoop:
    def test_once_syncs_and_fires(self, capsys):
        Schedule.objects.create(
            name="due",
            task_path="tests.tasks.plain",
            interval_seconds=60,
            args=[1],
            next_run_at=timezone.now() - timedelta(seconds=1),
        )
        call_command("overseer_scheduler", once=True)
        assert "ran 1 tick" in capsys.readouterr().out
        assert Schedule.objects.filter(name="heartbeat").exists()  # synced
        assert Job.objects.filter(args=[1]).count() == 1

    def test_loop_stops_after_max_ticks(self, monkeypatch):
        monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)
        assert scheduler.Scheduler(interval=0.01).run(max_ticks=3) == 3

    def test_signal_stops_loop(self, monkeypatch):
        s = scheduler.Scheduler(interval=0.01)

        def fake_sleep(_):
            s._stop(15, None)

        monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)
        assert s.run() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
class TestConcurrentTicks:
    @pytest.fixture(autouse=True)
    def require_postgres(self):
        if connection.vendor != "postgresql":
            pytest.skip("row locking is only real on PostgreSQL")

    def test_parallel_schedulers_fire_each_schedule_once(self):
        for i in range(5):
            Schedule.objects.create(
                name=f"s{i}",
                task_path="tests.tasks.plain",
                interval_seconds=3600,
                args=[i],
                next_run_at=timezone.now() - timedelta(seconds=1),
            )
        barrier = threading.Barrier(6)

        def run_tick():
            try:
                barrier.wait()
                return len(scheduler.tick())
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=6) as pool:
            counts = list(pool.map(lambda _: run_tick(), range(6)))
        assert sum(counts) == 5, counts
        assert Job.objects.count() == 5
        assert all(s.runs_count == 1 for s in Schedule.objects.all())

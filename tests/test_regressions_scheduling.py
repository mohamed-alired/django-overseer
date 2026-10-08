"""Regression tests for the scheduler, cron, sync, alert and retention bugs fixed in 0.1.1."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import CommandError, call_command
from django.dispatch import receiver
from django.utils import timezone
from freezegun import freeze_time

from overseer import alerts, prune, signals
from overseer.models import Job, JobStatus, MetricBucket, Run, RunStatus, Schedule
from overseer.scheduling import registry, scheduler
from overseer.scheduling.cron import parse
from overseer.scheduling.sync import sync_schedules

pytestmark = pytest.mark.django_db

NY = "America/New_York"


def at(*args):
    return datetime(*args, tzinfo=UTC)


class TestCronAroundDaylightSaving:
    # 2026-11-01, New York: 01:00-02:00 local happens twice (05:00-06:00Z, then 06:00-07:00Z).
    def test_second_pass_never_returns_the_past(self):
        after = at(2026, 11, 1, 6, 30)
        assert parse("*/5 * * * *").next_after(after, tz=NY) == at(2026, 11, 1, 6, 35)
        assert parse("* * * * *").next_after(after, tz=NY) == at(2026, 11, 1, 6, 31)

    def test_every_hour_jobs_follow_real_time_through_both_passes(self):
        cron = parse("*/5 * * * *")
        assert cron.next_after(at(2026, 11, 1, 5, 50), tz=NY) == at(2026, 11, 1, 5, 55)
        assert cron.next_after(at(2026, 11, 1, 5, 55), tz=NY) == at(2026, 11, 1, 6, 0)

    def test_fixed_hour_jobs_fire_once_in_the_repeated_hour(self):
        cron = parse("30 1 * * *")
        assert cron.next_after(at(2026, 11, 1, 5, 0), tz=NY) == at(2026, 11, 1, 5, 30)
        assert cron.next_after(at(2026, 11, 1, 5, 30), tz=NY) == at(2026, 11, 2, 6, 30)
        assert cron.next_after(at(2026, 11, 1, 6, 30), tz=NY) == at(2026, 11, 2, 6, 30)

    def test_time_in_the_spring_gap_fires_shifted_even_just_after_the_gap(self):
        # 2026-03-08, New York: 02:00-03:00 local does not exist (07:00Z is 03:00 EDT).
        cron = parse("30 2 * * *")
        assert cron.next_after(at(2026, 3, 8, 6, 0), tz=NY) == at(2026, 3, 8, 7, 30)
        assert cron.next_after(at(2026, 3, 8, 7, 0, 7), tz=NY) == at(2026, 3, 8, 7, 30)

    def test_star_step_in_day_fields_is_unrestricted_like_vixie(self):
        # "*/2" in day-of-month counts as "*" for the OR rule: odd days that are Sundays.
        cron = parse("0 0 */2 * 7")
        assert cron.next_after(at(2026, 1, 1), tz="UTC") == at(2026, 1, 11)

    def test_impossible_dates_are_rejected_at_parse_time(self):
        with pytest.raises(ValueError, match="names a day"):
            parse("0 0 31 2 *")
        assert parse("0 0 29 2 *")  # leap days exist
        assert parse("0 0 31 2 mon")  # the weekday can still match

    def test_scheduler_does_not_fire_every_tick_in_the_repeated_hour(self):
        Schedule.objects.create(
            name="dst", task_path="tests.tasks.plain", cron="*/5 * * * *", args=[1],
            timezone=NY, next_run_at=at(2026, 11, 1, 6, 30),
        )  # fmt: skip
        with freeze_time(at(2026, 11, 1, 6, 30)) as clock:
            for _ in range(60):
                scheduler.tick()
                clock.tick(timedelta(seconds=1))
        assert Job.objects.count() == 1
        assert Schedule.objects.get().next_run_at == at(2026, 11, 1, 6, 35)


class TestBrokenSchedules:
    def make(self, name, **fields):
        fields.setdefault("task_path", "tests.tasks.plain")
        fields.setdefault("next_run_at", timezone.now() - timedelta(seconds=1))
        return Schedule.objects.create(name=name, args=[1], **fields)

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            ({"cron": "0 0 31 2 *"}, "names a day"),
            ({"cron": "nonsense"}, "invalid cron"),
            ({"cron": "* * * * *", "timezone": "Europe/Pariss"}, "unknown timezone"),
            ({}, "needs a cron expression"),
            ({"interval_seconds": 0}, "needs a cron expression"),
        ],
    )
    def test_a_broken_row_is_disabled_and_the_others_still_fire(self, fields, reason):
        broken = self.make("broken", **fields)
        good = self.make("good", interval_seconds=60)
        jobs = scheduler.tick()
        assert [j.schedule_id for j in jobs] == [good.pk]
        broken.refresh_from_db()
        assert broken.enabled is False and broken.next_run_at is None
        assert reason in broken.last_error

    def test_enqueue_error_is_recorded_and_cleared(self):
        s = self.make("gone", interval_seconds=60, task_path="tests.tasks.missing")
        scheduler.tick()
        s.refresh_from_db()
        assert s.enabled and "ImportError" in s.last_error and s.runs_count == 0
        Schedule.objects.filter(pk=s.pk).update(
            task_path="tests.tasks.plain", next_run_at=timezone.now()
        )
        scheduler.tick()
        s.refresh_from_db()
        assert s.last_error == "" and s.runs_count == 1

    def test_hand_made_row_without_next_run_gets_one(self):
        s = Schedule.objects.create(name="new", task_path="tests.tasks.plain", interval_seconds=60)
        with freeze_time("2026-01-01 12:00:00"):
            assert scheduler.tick() == []
        s.refresh_from_db()
        assert s.next_run_at == at(2026, 1, 1, 12, 1)

    # transaction=True: after a failed iteration the loop recycles the connection, which
    # must not happen inside a test transaction (PostgreSQL would lose the test's data).
    @pytest.mark.django_db(transaction=True)
    def test_loop_survives_any_error(self, monkeypatch):
        calls = []

        def flaky_tick(now=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return []

        monkeypatch.setattr(scheduler, "tick", flaky_tick)
        assert scheduler.Scheduler(interval=0.01).run(max_ticks=1) == 1
        assert len(calls) == 2

    def test_run_now_counts_atomically(self):
        s = self.make("count", interval_seconds=60)
        stale = Schedule.objects.get(pk=s.pk)
        scheduler.tick()  # runs_count 1, behind ``stale``'s back
        scheduler.run_now(stale)
        assert Schedule.objects.get(pk=s.pk).runs_count == 2

    def test_model_clean_validates(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            Schedule(name="x", task_path="tests.tasks.plain", cron="0 0 30 2 *").clean()


class TestSyncLifecycle:
    def test_removed_then_restored_declaration_is_enabled_again(self):
        sync_schedules()
        spec = registry.all_specs()["heartbeat"]
        registry._schedules.pop("heartbeat")
        try:
            sync_schedules()
        finally:
            registry.register(**vars(spec))
        assert Schedule.objects.get(name="heartbeat").enabled is False
        report = sync_schedules()
        assert report["enabled"] == ["heartbeat"]
        row = Schedule.objects.get(name="heartbeat")
        assert row.enabled and not row.missing_from_code and row.next_run_at

    def test_paused_by_a_person_stays_paused(self):
        sync_schedules()
        Schedule.objects.filter(name="heartbeat").update(enabled=False)
        sync_schedules()
        assert Schedule.objects.get(name="heartbeat").enabled is False

    def test_empty_registry_disables_nothing(self, monkeypatch):
        sync_schedules()
        monkeypatch.setattr(registry, "_schedules", {})
        report = sync_schedules()
        assert report["disabled"] == []
        assert Schedule.objects.filter(declared_in_code=True, enabled=False).count() == 0

    def test_hand_made_row_with_a_declared_name_is_left_alone(self):
        manual = Schedule.objects.create(
            name="heartbeat", task_path="tests.tasks.plain", cron="0 * * * *", args=[9]
        )
        report = sync_schedules()
        assert report["conflicts"] == ["heartbeat"]
        manual.refresh_from_db()
        assert manual.declared_in_code is False and manual.task_path == "tests.tasks.plain"
        assert manual.args == [9]

    def test_tuple_args_sync_idempotently(self):
        spec = registry.register(
            name="tuple-args", task_path="tests.tasks.plain", interval_seconds=60, args=(1,)
        )
        try:
            assert "tuple-args" in sync_schedules()["created"]
            assert "tuple-args" in sync_schedules()["unchanged"]
        finally:
            registry._schedules.pop(spec.name)


class TestAlerting:
    def test_queue_wait_counts_runs_nobody_started(self, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = 60
        job = Job.objects.create(
            task_path="t", task_name="t", backend="default", queue_name="stuck"
        )
        Run.objects.create(
            job=job, result_id="r1", backend="default", status=RunStatus.READY,
            enqueued_at=timezone.now() - timedelta(hours=2),
        )  # fmt: skip
        Run.objects.create(
            job=job, result_id="r2", backend="default", status=RunStatus.READY,
            enqueued_at=timezone.now() - timedelta(hours=2),
            run_after=timezone.now() + timedelta(hours=1),  # deferred: not waiting yet
        )  # fmt: skip
        (cond,) = [c for c in alerts.detect() if c.kind == "queue_wait"]
        assert cond.key == "stuck" and cond.value > 7000

    def test_bad_notifier_and_raising_receiver_do_not_break_alerting(self, settings, caplog):
        settings.OVERSEER_NOTIFIERS = ["no.such.notifier", lambda alert: None]
        settings.OVERSEER_ALERT_QUEUE_DEPTH = 0
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        Run.objects.create(job=job, result_id="r", backend="default", enqueued_at=timezone.now())

        @receiver(signals.alert_fired, weak=False)
        def explode(sender, alert, **kwargs):
            raise RuntimeError("receiver failed")

        try:
            with caplog.at_level(logging.ERROR, logger="overseer"):
                fired = alerts.evaluate()
        finally:
            signals.alert_fired.disconnect(explode)
        assert [a.kind for a in fired] == ["queue_depth"]
        assert "no.such.notifier" in caplog.text and "receiver" in caplog.text


class TestRetention:
    def test_zero_days_means_zero_days(self):
        old = timezone.now() - timedelta(days=1)
        job = Job.objects.create(
            task_path="t", task_name="t", backend="d", queue_name="q",
            status=JobStatus.SUCCEEDED, finished_at=old,
        )  # fmt: skip
        MetricBucket.objects.create(bucket_start=old, queue_name="q")
        report = prune.prune(days=0, metrics_days=0)
        assert report["jobs"] == 1 and report["metric_buckets"] == 1
        assert not Job.objects.filter(pk=job.pk).exists()

    def test_negative_days_are_refused(self):
        with pytest.raises(ValueError):
            prune.prune(days=-1)
        with pytest.raises(CommandError):
            call_command("overseer_prune", "--days", "-1")


class TestCommandArguments:
    def test_rollup_since_accepts_naive_datetimes_and_dates(self):
        call_command("overseer_rollup", "--since", "2026-01-01T00:00:00")
        call_command("overseer_rollup", "--since", "2026-01-01")

    def test_rollup_since_rejects_garbage(self):
        with pytest.raises(CommandError, match="not an ISO date"):
            call_command("overseer_rollup", "--since", "yesterday")

    def test_scheduler_interval_must_be_positive(self):
        with pytest.raises(CommandError):
            call_command("overseer_scheduler", "--once", "--interval", "0")

    def test_worker_heartbeat_must_be_positive(self):
        with pytest.raises(CommandError):
            call_command("overseer_worker", "--batch", "--heartbeat", "0")

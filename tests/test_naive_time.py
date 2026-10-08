"""Projects with ``USE_TZ = False`` (naive local datetimes everywhere)."""

from datetime import datetime, timedelta

import pytest
from django.conf import settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from overseer import alerts, metrics
from overseer.models import Job, JobStatus, Schedule
from overseer.scheduling import scheduler
from overseer.scheduling.sync import sync_schedules
from tests import tasks

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(settings.USE_TZ, reason="needs USE_TZ = False (tests.settings_naive)"),
]


def test_cron_schedules_sync_and_compute_naive_next_runs():
    report = sync_schedules()
    assert report["errors"] == []
    s = Schedule.objects.get(name="tests.tasks.scheduled_every_5")
    assert timezone.is_naive(s.next_run_at)
    nxt = scheduler.compute_next_run(s, datetime(2026, 7, 1, 12, 3))
    assert nxt == datetime(2026, 7, 1, 12, 5) and timezone.is_naive(nxt)


def test_tick_fires_a_due_cron_schedule(worker):
    Schedule.objects.create(
        name="naive-cron",
        task_path="tests.tasks.plain",
        cron="* * * * *",
        args=[3],
        next_run_at=timezone.now() - timedelta(minutes=1),
    )
    jobs = scheduler.tick()
    assert len(jobs) == 1
    worker()
    assert Job.objects.get().status == JobStatus.SUCCEEDED
    assert Schedule.objects.get(name="naive-cron").next_run_at > timezone.now()


def test_dashboard_toggle_and_pages(staff_client, worker):
    sync_schedules()
    s = Schedule.objects.get(name="tests.tasks.scheduled_every_5")
    Schedule.objects.filter(pk=s.pk).update(enabled=False, next_run_at=None)
    resp = staff_client.post(reverse("overseer:schedule-toggle", args=[s.pk]))
    assert resp.status_code == 302
    s.refresh_from_db()
    assert s.enabled and s.next_run_at is not None
    tasks.plain.enqueue(1)
    tasks.always_fails.enqueue()
    worker()
    for name in [
        "overview",
        "queues",
        "tasks",
        "jobs",
        "failed",
        "schedules",
        "workers",
        "metrics",
    ]:
        assert staff_client.get(reverse(f"overseer:{name}")).status_code == 200, name


def test_rollup_alerts_and_since(worker):
    tasks.plain.enqueue(1)
    worker()
    assert metrics.rollup() > 0
    assert alerts.evaluate() == []
    call_command("overseer_rollup", "--since", "2026-01-01T00:00:00+00:00", verbosity=0)
    call_command("overseer_rollup", "--since", "2026-01-01", verbosity=0)


# Europe/Paris, 2026-10-25: at 03:00 CEST the clocks go back to 02:00 CET, so the naive
# times 02:00-02:59 happen twice. Naive storage cannot tell the two passes apart.


def test_next_cron_run_during_the_repeated_hour_is_after_the_current_time():
    s = Schedule(name="c", task_path="tests.tasks.plain", cron="*/5 * * * *")
    after = datetime(2026, 10, 25, 2, 56)
    assert scheduler.compute_next_run(s, after) > after


def test_a_cron_schedule_fires_once_per_slot_in_the_repeated_hour():
    for name, cron, start in (
        ("every5", "*/5 * * * *", datetime(2026, 10, 25, 2, 55)),
        ("hourly", "0 * * * *", datetime(2026, 10, 25, 2, 0)),
    ):
        Schedule.objects.create(
            name=name, task_path="tests.tasks.plain", args=[1], cron=cron, next_run_at=start
        )
        fired, now = 0, start
        while now < datetime(2026, 10, 25, 3, 0):
            fired += len(scheduler.tick(now))
            now += timedelta(seconds=10)
        assert fired == 1, name
        Schedule.objects.all().delete()


def test_interval_across_the_repeated_hour_never_lands_on_the_current_time():
    s = Schedule(name="i", task_path="tests.tasks.plain", interval_seconds=3600)
    after = datetime(2026, 10, 25, 2, 30)
    assert scheduler.compute_next_run(s, after) == datetime(2026, 10, 25, 3, 30)
    # Away from the change an interval is plain wall-clock arithmetic.
    assert scheduler.compute_next_run(s, datetime(2026, 7, 1, 2, 30)) == datetime(2026, 7, 1, 3, 30)


def test_schedule_with_its_own_timezone():
    s = Schedule(
        name="ny", task_path="tests.tasks.plain", cron="0 9 * * *", timezone="America/New_York"
    )
    assert scheduler.compute_next_run(s, datetime(2026, 7, 1, 12, 0)) == datetime(2026, 7, 1, 15, 0)

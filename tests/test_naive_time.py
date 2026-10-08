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

"""Overseer's tables on a non-default database alias, selected by a router."""

import pytest
from django.conf import settings
from django.db import connections

from overseer import db, metrics, retry
from overseer.models import Job, JobStatus, Schedule
from overseer.scheduling import scheduler
from overseer.scheduling.sync import sync_schedules
from tests import tasks

pytestmark = [
    pytest.mark.django_db(databases=["default", "ops"], transaction=True),
    pytest.mark.skipif("ops" not in settings.DATABASES, reason="tests.settings_multidb only"),
]


def test_tables_live_on_the_routed_alias():
    assert db.alias_for(Job) == "ops" and db.alias_for(Schedule) == "ops"
    assert db.features_for(Job) is connections["ops"].features


def test_sync_tick_retry_and_rollup_use_the_routed_alias(worker):
    report = sync_schedules()
    assert report["errors"] == []
    assert Schedule.objects.using("ops").filter(declared_in_code=True).exists()
    s = Schedule.objects.get(name="heartbeat")
    Schedule.objects.filter(pk=s.pk).update(next_run_at=scheduler.timezone.now())
    assert len(scheduler.tick()) == 1
    tasks.always_fails.enqueue()
    worker()
    failed = Job.objects.get(task_path="tests.tasks.always_fails")
    assert failed.status == JobStatus.FAILED
    retry.retry_job(failed)
    assert failed.runs.count() == 2
    assert metrics.rollup() > 0
    default_tables = connections["default"].introspection.table_names()
    assert not [t for t in default_tables if t.startswith("overseer_")]  # nothing leaked

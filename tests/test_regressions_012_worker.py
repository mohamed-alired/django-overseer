"""Regression tests for the worker, interruption and alert-threshold bugs fixed in 0.1.2."""

import logging
import os

import pytest
from django.utils.autoreload import DJANGO_AUTORELOAD_ENV

from overseer import alerts, logs
from overseer.management.commands.overseer_worker import WORKER_ID_ENV, stable_worker_id
from overseer.models import Job, JobStatus, RunStatus, Worker
from overseer.worker import HeartbeatWorker
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def make_worker(worker_id="hb-test", heartbeat=0.05):
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


class TestHeartbeatRow:
    def test_beat_registers_when_the_row_is_missing(self):
        worker = make_worker()
        worker.beat()  # no row yet: registration failed at startup, or prune removed it
        row = Worker.objects.get(worker_id="hb-test")
        assert row.heartbeat_seconds == 0.05 and row.pid == os.getpid() and row.queues == ["*"]
        assert row.is_online() is True

    def test_stop_is_recorded_once(self):
        worker = make_worker()
        worker.run()
        row = Worker.objects.get(worker_id="hb-test")
        assert row.stopped_at is not None
        stopped_at = row.stopped_at
        worker._mark_stopped()  # the atexit hook after a normal stop: a no-op
        assert Worker.objects.get(worker_id="hb-test").stopped_at == stopped_at


class TestReloadKeepsOneWorkerId:
    def test_parent_publishes_and_child_reuses_the_id(self, monkeypatch):
        monkeypatch.delenv(WORKER_ID_ENV, raising=False)
        monkeypatch.delenv(DJANGO_AUTORELOAD_ENV, raising=False)
        assert stable_worker_id("first") == "first"
        assert os.environ[WORKER_ID_ENV] == "first"
        monkeypatch.setenv(DJANGO_AUTORELOAD_ENV, "true")
        assert stable_worker_id("random-after-restart") == "first"


class TestInterruptedTasks:
    def test_forced_stop_is_abandonment_and_retried(self):
        tasks.exits_once.enqueue()
        job = Job.objects.get()
        assert job.status == JobStatus.SUCCEEDED
        assert [r.status for r in job.runs.order_by("attempt")] == [
            RunStatus.ABANDONED,
            RunStatus.SUCCESSFUL,
        ]
        assert "SystemExit" in job.runs.order_by("attempt").first().exception_class


class TestAlertThresholds:
    def test_none_thresholds_do_not_crash_detection(self, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = None
        settings.OVERSEER_ALERT_FAILURE_RATE = None
        settings.OVERSEER_ALERT_QUEUE_DEPTH = None
        tasks.plain.enqueue(1)
        assert isinstance(alerts.detect(), list)


class TestCommandLogging:
    def test_overseer_logger_gets_a_console_handler(self):
        import io

        logger = logging.getLogger("overseer")
        saved_handlers, saved_level = list(logger.handlers), logger.level
        saved_propagate = logger.propagate
        try:
            for h in saved_handlers:
                logger.removeHandler(h)
            logger.propagate = False  # pytest's own root handlers must not count
            stream = io.StringIO()
            logs.configure(stream, 1)
            logger.info("hello from overseer")
            assert "hello from overseer" in stream.getvalue()
        finally:
            for h in list(logger.handlers):
                logger.removeHandler(h)
            for h in saved_handlers:
                logger.addHandler(h)
            logger.setLevel(saved_level)
            logger.propagate = saved_propagate

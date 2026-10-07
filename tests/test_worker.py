import os
import socket
import time

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from overseer.models import Job, JobStatus, Worker
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def run_worker(**kwargs):
    kwargs.setdefault("queue_name", "*")
    call_command(
        "overseer_worker",
        batch=True,
        startup_delay=False,
        interval=0.0,
        verbosity=0,
        **kwargs,
    )


class TestOverseerWorker:
    def test_registers_row_and_marks_stopped(self):
        result = tasks.plain.enqueue(1)
        run_worker(worker_id="w-test", heartbeat=0.05)
        row = Worker.objects.get(worker_id="w-test")
        assert row.hostname == socket.gethostname() and row.pid == os.getpid()
        assert row.queues == ["*"] and row.backend == "default"
        assert row.stopped_at is not None and row.tasks_processed == 1
        assert row.current_run is None
        from overseer.models import Job

        assert Job.objects.get(runs__result_id=str(result.id)).status == JobStatus.SUCCEEDED

    def test_heartbeat_advances_last_seen_during_a_long_task(self):
        tasks.slow.enqueue(0.6)
        before = timezone.now()
        run_worker(worker_id="w-beat", heartbeat=0.1)
        row = Worker.objects.get(worker_id="w-beat")
        assert row.stopped_at is not None
        assert row.last_seen_at >= before
        assert row.tasks_processed == 1

    def test_restart_reuses_row_and_clears_stopped(self):
        run_worker(worker_id="w-again", heartbeat=0.05)
        assert Worker.objects.get(worker_id="w-again").stopped_at is not None
        tasks.plain.enqueue(2)
        run_worker(worker_id="w-again", heartbeat=0.05)
        assert Worker.objects.count() == 1
        assert Worker.objects.get(worker_id="w-again").tasks_processed == 1

    def test_exclude_queues_validation(self):
        with pytest.raises(CommandError, match="exclude-queues"):
            run_worker(queue_name="default", exclude_queues="emails")

    def test_heartbeat_thread_stops_with_worker(self):
        import threading

        run_worker(worker_id="w-threads", heartbeat=0.05)
        time.sleep(0.2)
        assert not [t for t in threading.enumerate() if t.name.startswith("overseer-heartbeat")]


class TestRegisterRetries:
    def test_retries_transient_database_errors(self, monkeypatch):
        from django.db import OperationalError

        from overseer import worker as worker_module

        calls = []
        real = worker_module.HeartbeatWorker._register

        def flaky(self):
            calls.append(1)
            if len(calls) < 3:
                raise OperationalError("database is locked")
            return real(self)

        monkeypatch.setattr(worker_module.HeartbeatWorker, "_register", flaky)
        monkeypatch.setattr(worker_module.time, "sleep", lambda s: None)
        run_worker(worker_id="w-retry", heartbeat=0.05)
        assert len(calls) == 3
        assert Worker.objects.get(worker_id="w-retry").stopped_at is not None

    def test_gives_up_and_keeps_running(self, monkeypatch):
        from django.db import OperationalError

        from overseer import worker as worker_module

        def broken(self):
            raise OperationalError("database is locked")

        monkeypatch.setattr(worker_module.HeartbeatWorker, "_register", broken)
        monkeypatch.setattr(worker_module.time, "sleep", lambda s: None)
        tasks.plain.enqueue(4)
        run_worker(worker_id="w-unregistered", heartbeat=0.05)
        # The row was created by the recorders when the task ran, so the worker is visible.
        assert Worker.objects.get(worker_id="w-unregistered").tasks_processed == 1
        assert Job.objects.get().status == JobStatus.SUCCEEDED

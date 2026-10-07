import os
import socket
import time

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from overseer.models import JobStatus, Worker
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

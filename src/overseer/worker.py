"""``overseer_worker``: the reference database worker plus heartbeats and a Worker row."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

from django.db import OperationalError, close_old_connections, connections
from django.utils import timezone
from django_tasks_db.management.commands.db_worker import Worker as DBWorker

from .models import Worker

logger = logging.getLogger("overseer")

REGISTER_ATTEMPTS = 5


class HeartbeatWorker(DBWorker):
    """A ``db_worker`` that keeps a ``Worker`` row alive while it runs.

    The row records host, pid, queues and ``last_seen_at``; the recorders fill in the
    current run and counters. ``stopped_at`` is set when the worker exits, so the
    dashboard can tell a clean stop from a crash.
    """

    def __init__(self, *args, heartbeat: float = 10.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.heartbeat = heartbeat
        self._stop_heartbeat = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self) -> Worker | None:
        """Create or revive this worker's row. A transient database error (a locked SQLite
        file, a restarting server) is retried, then logged: the recorders will create the
        row on the first task anyway, so a worker never refuses to start over it."""
        for attempt in range(1, REGISTER_ATTEMPTS + 1):
            try:
                return self._register()
            except OperationalError:
                if attempt == REGISTER_ATTEMPTS:
                    logger.exception("Could not register worker %s", self.worker_id)
                    return None
                time.sleep(0.5 * attempt)
            finally:
                close_old_connections()
        return None  # pragma: no cover

    def _register(self) -> Worker:
        now = timezone.now()
        row, _ = Worker.objects.update_or_create(
            worker_id=self.worker_id,
            defaults={
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "backend": self.backend_name,
                "queues": list(self.queue_names),
                "started_at": now,
                "last_seen_at": now,
                "stopped_at": None,
            },
        )
        return row

    def beat(self) -> None:
        Worker.objects.filter(worker_id=self.worker_id).update(
            last_seen_at=timezone.now(), stopped_at=None
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(self.heartbeat):
            try:
                self.beat()
            except Exception:  # pragma: no cover - never let the heartbeat kill the worker
                logger.exception("Overseer heartbeat failed for worker %s", self.worker_id)
            finally:
                close_old_connections()
        connections.close_all()

    def run(self) -> None:
        self.register()
        self._thread = threading.Thread(
            target=self._heartbeat_loop, name=f"overseer-heartbeat-{self.worker_id}", daemon=True
        )
        self._thread.start()
        try:
            super().run()
        finally:
            self._stop_heartbeat.set()
            self._thread.join(timeout=self.heartbeat + 1)
            Worker.objects.filter(worker_id=self.worker_id).update(
                stopped_at=timezone.now(), last_seen_at=timezone.now(), current_run=None
            )
            logger.info("Worker %s stopped after %d task(s)", self.worker_id, self._run_tasks)

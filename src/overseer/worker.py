"""``overseer_worker``: the reference database worker plus heartbeats and a Worker row."""

from __future__ import annotations

import atexit
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


def record_stop(worker_id: str) -> None:
    """Mark ``worker_id`` stopped unless it already recorded its stop."""
    now = timezone.now()
    try:
        Worker.objects.filter(worker_id=worker_id, stopped_at__isnull=True).update(
            stopped_at=now, last_seen_at=now, current_run=None
        )
    except Exception:  # noqa: BLE001 - the database may be the reason we are stopping
        logger.warning("Could not record the stop of worker %s", worker_id)


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
        self._stopped = False
        self._atexit_registered = False

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
                "heartbeat_seconds": self.heartbeat,
            },
        )
        return row

    def beat(self) -> None:
        updated = Worker.objects.filter(
            worker_id=self.worker_id, heartbeat_seconds__isnull=False
        ).update(last_seen_at=timezone.now(), stopped_at=None)
        if not updated:
            # Registration failed at startup, the row was pruned, or the recorder created
            # it without heartbeat details: (re)register so the worker is reported with
            # its heartbeat, host and pid.
            self._register()

    def _heartbeat_loop(self) -> None:
        failing = False
        while not self._stop_heartbeat.wait(self.heartbeat):
            try:
                self.beat()
            except Exception as exc:  # never let the heartbeat kill the worker
                if failing:  # one traceback per outage, then a line per missed beat
                    logger.warning("Heartbeat for worker %s still failing: %s", self.worker_id, exc)
                else:
                    logger.exception("Overseer heartbeat failed for worker %s", self.worker_id)
                failing = True
            else:
                if failing:
                    logger.info("Heartbeat for worker %s recovered", self.worker_id)
                failing = False
            finally:
                close_old_connections()
        connections.close_all()

    def _mark_stopped(self) -> None:
        """Record the stop once; called from ``run`` and, for the autoreloader, at exit."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_heartbeat.set()
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat + 1)
        try:
            Worker.objects.filter(worker_id=self.worker_id).update(
                stopped_at=timezone.now(), last_seen_at=timezone.now(), current_run=None
            )
        except Exception:  # noqa: BLE001 - the database may be the reason we are stopping
            logger.warning("Could not record the stop of worker %s", self.worker_id)
        logger.info("Worker %s stopped after %d task(s)", self.worker_id, self._run_tasks)

    def run(self) -> None:
        # A fresh start each time, so a supervisor can call run() on the same instance again.
        self._stopped = False
        self._stop_heartbeat = threading.Event()
        self.register()
        self._thread = threading.Thread(
            target=self._heartbeat_loop, name=f"overseer-heartbeat-{self.worker_id}", daemon=True
        )
        self._thread.start()
        # Under ``--reload`` Django runs this in a daemon thread and the autoreloader exits
        # the process with ``sys.exit``; ``finally`` never runs there, ``atexit`` does.
        if not self._atexit_registered:
            atexit.register(self._mark_stopped)
            self._atexit_registered = True
        try:
            super().run()
        finally:
            self._mark_stopped()

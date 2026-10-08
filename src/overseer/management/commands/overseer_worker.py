"""Run the reference database worker with Overseer heartbeats.

Accepts every ``db_worker`` option plus ``--heartbeat``.
"""

import os

from django.core.management.base import CommandError
from django.utils.autoreload import DJANGO_AUTORELOAD_ENV, run_with_reloader
from django_tasks_db.management.commands.db_worker import Command as DBWorkerCommand

from overseer import logs
from overseer.worker import HeartbeatWorker, record_stop

WORKER_ID_ENV = "OVERSEER_WORKER_ID"


def stable_worker_id(worker_id: str) -> str:
    """Under ``--reload`` the autoreloader restarts the process on every code change; keep
    one worker id across restarts (passed through the environment) so each restart reuses
    its Worker row instead of leaving a trail of never-stopped ones."""
    if os.environ.get(DJANGO_AUTORELOAD_ENV) == "true":
        return os.environ.get(WORKER_ID_ENV) or worker_id
    os.environ[WORKER_ID_ENV] = worker_id
    return worker_id


class Command(DBWorkerCommand):
    help = "Run a database task worker that reports to the Overseer dashboard."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--heartbeat",
            type=float,
            default=10.0,
            help="Seconds between heartbeats to the Worker table (default 10).",
        )

    def handle(
        self,
        *,
        verbosity,
        queue_name,
        interval,
        batch,
        backend_name,
        startup_delay,
        reload,
        max_tasks,
        worker_id,
        exclude_queues,
        heartbeat,
        **options,
    ):
        if heartbeat <= 0:
            raise CommandError("--heartbeat must be a positive number of seconds")
        self.configure_logging(verbosity)
        logs.configure(self.stdout, verbosity)
        if reload and batch:
            reload = False
        if reload:
            worker_id = stable_worker_id(worker_id)
        queue_names = queue_name.split(",")
        excluded = exclude_queues.split(",") if exclude_queues else []
        if excluded and "*" not in queue_names:
            raise CommandError("--exclude-queues can only be used with --queue-name=*")
        worker = HeartbeatWorker(
            queue_names=queue_names,
            interval=interval,
            batch=batch,
            backend_name=backend_name,
            startup_delay=startup_delay,
            max_tasks=max_tasks,
            worker_id=worker_id,
            excluded_queue_names=excluded,
            heartbeat=heartbeat,
        )
        if reload:
            if os.environ.get(DJANGO_AUTORELOAD_ENV) == "true":
                worker.configure_signals()
                run_with_reloader(worker.run)
                return
            try:
                run_with_reloader(worker.run)
            finally:
                # The reloader's parent process, leaving (SIGTERM, Ctrl-C): it kills the
                # worker process on its way out, often before that one records its stop.
                record_stop(worker_id)
        else:
            worker.configure_signals()
            worker.run()

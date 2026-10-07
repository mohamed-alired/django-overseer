"""Run the reference database worker with Overseer heartbeats.

Accepts every ``db_worker`` option plus ``--heartbeat``.
"""

import os

from django.core.management.base import CommandError
from django.utils.autoreload import DJANGO_AUTORELOAD_ENV, run_with_reloader
from django_tasks_db.management.commands.db_worker import Command as DBWorkerCommand

from overseer.worker import HeartbeatWorker


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
        if reload and batch:
            reload = False
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
        else:
            worker.configure_signals()
            worker.run()

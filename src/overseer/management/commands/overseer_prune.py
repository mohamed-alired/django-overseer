from django.core.management.base import BaseCommand, CommandError

from overseer.prune import prune


class Command(BaseCommand):
    help = "Delete finished jobs, metrics, alerts and workers older than the retention settings."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Override OVERSEER_RETENTION_DAYS.")
        parser.add_argument(
            "--metrics-days", type=int, help="Override OVERSEER_METRICS_RETENTION_DAYS."
        )

    def handle(self, *args, **options):
        try:
            report = prune(days=options["days"], metrics_days=options["metrics_days"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        for key, count in report.items():
            self.stdout.write(f"{key}: {count}")
        self.stdout.write(self.style.SUCCESS("Pruned."))

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime

from overseer.metrics import rollup


class Command(BaseCommand):
    help = "Roll Run rows up into per-minute MetricBucket rows (also done by the scheduler loop)."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="ISO datetime to recompute from (default: recent).")

    def handle(self, *args, **options):
        since = parse_datetime(options["since"]) if options["since"] else None
        written = rollup(since=since)
        self.stdout.write(self.style.SUCCESS(f"Wrote {written} metric bucket(s)."))

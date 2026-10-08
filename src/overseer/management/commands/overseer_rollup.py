from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from overseer.metrics import rollup


class Command(BaseCommand):
    help = "Roll Run rows up into per-minute MetricBucket rows (also done by the scheduler loop)."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="ISO datetime to recompute from (default: recent).")

    def handle(self, *args, **options):
        since = None
        if options["since"]:
            raw = options["since"]
            try:
                since = parse_datetime(raw)
                day = parse_date(raw) if since is None else None
            except ValueError as exc:  # well-formed but impossible, such as month 13
                raise CommandError(f"--since: {raw!r} is not a valid date: {exc}") from None
            if since is None and day is not None:
                since = timezone.datetime.combine(day, timezone.datetime.min.time())
            if since is None:
                raise CommandError(f"--since: {raw!r} is not an ISO date or datetime")
            if settings.USE_TZ and timezone.is_naive(since):
                since = timezone.make_aware(since)
            elif not settings.USE_TZ and timezone.is_aware(since):
                since = timezone.make_naive(since)
        written = rollup(since=since)
        self.stdout.write(self.style.SUCCESS(f"Wrote {written} metric bucket(s)."))

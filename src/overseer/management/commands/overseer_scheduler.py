from django.core.management.base import BaseCommand

from overseer.scheduling.scheduler import Scheduler


class Command(BaseCommand):
    help = (
        "Run the Overseer scheduler: enqueue tasks declared with overseer.schedule (and "
        "schedules created in the dashboard) when they are due. Safe to run on several hosts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval", type=float, default=None, help="Seconds between checks (default 1)."
        )
        parser.add_argument(
            "--once", action="store_true", help="Sync schedules, fire what is due, then exit."
        )

    def handle(self, *args, **options):
        scheduler = Scheduler(interval=options["interval"])
        ticks = scheduler.run(once=options["once"])
        if options["once"]:
            self.stdout.write(self.style.SUCCESS(f"Scheduler ran {ticks} tick(s)."))

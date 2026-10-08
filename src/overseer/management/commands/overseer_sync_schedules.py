from django.core.management.base import BaseCommand

from overseer.scheduling.sync import sync_schedules


class Command(BaseCommand):
    help = "Mirror overseer.schedule declarations into the Schedule table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--enable",
            action="store_true",
            help="Also enable every declared schedule that is disabled, whoever disabled it "
            "(for schedules disabled by versions before 0.1.1, which kept no marker).",
        )

    def handle(self, *args, **options):
        report = sync_schedules(enable_all=options["enable"])
        for key, names in report.items():
            if names:
                self.stdout.write(f"{key}: {', '.join(names)}")
        self.stdout.write(self.style.SUCCESS("Schedules synced."))

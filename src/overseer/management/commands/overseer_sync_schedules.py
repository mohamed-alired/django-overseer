from django.core.management.base import BaseCommand

from overseer.scheduling.sync import sync_schedules


class Command(BaseCommand):
    help = "Mirror overseer.schedule declarations into the Schedule table."

    def handle(self, *args, **options):
        report = sync_schedules()
        for key, names in report.items():
            if names:
                self.stdout.write(f"{key}: {', '.join(names)}")
        self.stdout.write(self.style.SUCCESS("Schedules synced."))

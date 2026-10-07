from django.core.management.base import BaseCommand

from overseer.rescue import rescue


class Command(BaseCommand):
    help = "Mark running attempts that exceeded their timeout as abandoned and apply retries."

    def handle(self, *args, **options):
        runs = rescue()
        self.stdout.write(self.style.SUCCESS(f"Abandoned {len(runs)} stale run(s)."))

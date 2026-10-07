from django.core.management.base import BaseCommand

from overseer.alerts import evaluate


class Command(BaseCommand):
    help = "Evaluate alert conditions once (also done by the scheduler loop)."

    def handle(self, *args, **options):
        fired = evaluate()
        for alert in fired:
            self.stdout.write(f"{alert.kind} {alert.key}: {alert.message}")
        self.stdout.write(self.style.SUCCESS(f"{len(fired)} alert(s) fired."))

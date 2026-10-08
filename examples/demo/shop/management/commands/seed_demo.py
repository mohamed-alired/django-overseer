from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from shop import tasks


class Command(BaseCommand):
    help = "Create the demo users and enqueue a batch of example jobs."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="overseer-demo")
        parser.add_argument("--orders", type=int, default=30)

    def handle(self, *args, password, orders, **options):
        User = get_user_model()
        admin, _ = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True}
        )
        admin.set_password(password)
        admin.save()
        for order_id in range(1001, 1001 + orders):
            tasks.charge_card.enqueue(order_id, amount=round(19.99 + order_id % 7 * 5, 2))
            tasks.send_receipt.enqueue(order_id)
        tasks.send_receipt.enqueue(1001)  # a duplicate: handed the pending job back
        for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
            tasks.build_sales_report.enqueue(day)
        tasks.import_catalog.enqueue("catalog-october.csv")
        self.stdout.write(
            self.style.SUCCESS(
                f"Enqueued {orders * 2 + 5} jobs. Log in at http://127.0.0.1:8000/overseer/ "
                f"as admin / {password}"
            )
        )

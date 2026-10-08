"""Example tasks covering what Overseer adds: retries, unique jobs, schedules and failures."""

import random
import time

from django.tasks import task

import overseer


@overseer.task(retries=4, retry_on=(ConnectionError,), backoff="exponential", backoff_base=2)
def charge_card(order_id, amount):
    """Talks to a flaky payment provider: about one call in three times out and is retried."""
    time.sleep(0.2)
    if random.random() < 0.35:
        raise ConnectionError(f"Payment provider timed out charging order {order_id}")
    return {"order": order_id, "charged": amount}


@overseer.task(unique=True, queue_name="emails", tags=("mail",))
def send_receipt(order_id):
    """One receipt per order: enqueueing it again while the first is pending is a no-op."""
    time.sleep(0.1)
    return f"receipt sent for order {order_id}"


@overseer.task(queue_name="reports", timeout=120)
def build_sales_report(day):
    time.sleep(1.0)
    return {"day": day, "orders": random.randint(80, 140)}


@task
def import_catalog(filename):
    """Fails without retrying: bad data does not get better by trying again."""
    raise ValueError(f"{filename}, row 42: price 'twelve' is not a number")


@overseer.schedule("*/5 * * * *")
@task
def sync_inventory():
    time.sleep(0.3)
    return "inventory synced"


@overseer.schedule("0 3 * * *", name="nightly-cleanup")
@task
def nightly_cleanup():
    from overseer.prune import prune

    return prune()


@overseer.schedule(every=60, name="warehouse-heartbeat")
@task
def warehouse_heartbeat():
    return "ok"

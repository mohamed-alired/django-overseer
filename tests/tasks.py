"""Tasks used by the test suite."""

from django.tasks import task

import overseer

CALLS = []


@task
def plain(x):
    CALLS.append(("plain", x))
    return x * 2


@task
def always_fails():
    raise RuntimeError("always")


@overseer.task(retries=2, backoff="constant", backoff_base=10, jitter=False)
def flaky(fail_times, marker="flaky"):
    """Fails ``fail_times`` times (tracked in CALLS), then succeeds."""
    failures = sum(1 for c in CALLS if c == (marker, "fail"))
    if failures < fail_times:
        CALLS.append((marker, "fail"))
        raise RuntimeError(f"boom {failures + 1}")
    CALLS.append((marker, "ok"))
    return "done"


@overseer.task(retries=3, retry_on=(ValueError,), backoff="linear", backoff_base=5, jitter=False)
def picky(kind):
    if kind == "value":
        raise ValueError("retryable")
    if kind == "type":
        raise TypeError("not retryable")
    return kind


@overseer.task(unique=True, queue_name="emails", tags=("mail",))
def unique_by_args(email):
    CALLS.append(("unique", email))
    return email


@overseer.task(unique=lambda report_id, **kwargs: f"report:{report_id}")
def unique_by_callable(report_id, force=False):
    return report_id


@overseer.task(timeout=30, takes_context=True)
def with_context(context, value):
    return {"attempt": context.attempt, "value": value}


@overseer.schedule("*/5 * * * *", kwargs={"x": 1})
@overseer.task(queue_name="reports")
def scheduled_every_5(x):
    CALLS.append(("scheduled", x))
    return x


@overseer.schedule(every=60, name="heartbeat")
@task
def scheduled_interval():
    return "tick"


@task
def slow(seconds):
    import time

    time.sleep(seconds)
    return seconds

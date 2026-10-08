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


@overseer.task(retries=2, backoff="constant", backoff_base=0, jitter=False)
def flaky_fast(fail_times, marker="flaky_fast"):
    """Like ``flaky`` but retries immediately; for real-worker integration tests."""
    return flaky.call(fail_times, marker=marker)


# --- 0.1.1 regression tasks -------------------------------------------------------------


@overseer.task(unique=True, retries=2, backoff="constant", backoff_base=0, jitter=False)
def unique_flaky(n):
    """Unique and retrying: fails on its first call, then succeeds."""
    CALLS.append(("unique_flaky", n))
    if sum(1 for c in CALLS if c[0] == "unique_flaky") < 2:
        raise RuntimeError("first attempt fails")
    return n


@overseer.task(retries=1, backoff="constant", backoff_base=0, jitter=False)
def needs_arg(x):
    CALLS.append(("needs_arg", x))
    if sum(1 for c in CALLS if c[0] == "needs_arg") < 2:
        raise RuntimeError("fail once")
    return x


@task
def child(x):
    CALLS.append(("child", x))
    return x


@overseer.task(retries=1, backoff="constant", backoff_base=0, jitter=False, backend="immediate")
def parent_immediate(x):
    """Runs inline (immediate backend), enqueues a child, fails on its first call."""
    CALLS.append(("parent", x))
    child.enqueue(x)
    if sum(1 for c in CALLS if c[0] == "parent") < 2:
        raise RuntimeError("parent fails once")
    return x


@overseer.task(unique=True, backend="immediate")
def unique_parent_immediate(x):
    child.enqueue(x)
    return x


# --- 0.1.2 regression tasks -------------------------------------------------------------


@overseer.task(retries=2, backoff="constant", backoff_base=0, jitter=False, backend="immediate")
def zero_arg_flaky():
    CALLS.append(("zero_arg", None))
    if sum(1 for c in CALLS if c[0] == "zero_arg") < 2:
        raise RuntimeError("first attempt fails")
    return "ok"


@overseer.task(unique=True, backend="immediate")
def unique_recursive(n):
    CALLS.append(("recursive", n))
    if n == 1 and sum(1 for c in CALLS if c[0] == "recursive") == 1:
        unique_recursive.enqueue(1)  # same key while the first run is still active
    return n


@overseer.task(unique=True, backend="dummy")
def unique_dummy(n):
    return n

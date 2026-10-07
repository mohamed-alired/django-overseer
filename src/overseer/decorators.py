"""``overseer.task`` and ``overseer.schedule``: thin wrappers around ``django.tasks.task``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from django.tasks import Task as DjangoTask
from django.tasks import task as django_task
from django.tasks.base import (
    DEFAULT_TASK_BACKEND_ALIAS,
    DEFAULT_TASK_PRIORITY,
    DEFAULT_TASK_QUEUE_NAME,
)

from . import conf, registry
from .registry import TaskPolicy
from .scheduling import registry as schedule_registry


@dataclass(frozen=True, slots=True, kw_only=True)
class OverseerTask(DjangoTask):
    """A Django ``Task`` whose ``enqueue`` honours the ``unique`` policy."""

    def _unique_key(self, args, kwargs):
        policy = registry.get_policy(self.module_path)
        if not policy.unique:
            return ""
        if callable(policy.unique):
            return str(policy.unique(*args, **kwargs))[:255]
        if isinstance(policy.unique, str):
            return policy.unique[:255]
        payload = json.dumps([args, kwargs], sort_keys=True, default=str)
        return f"{self.module_path}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"

    def _existing_result(self, key):
        """The result of the active job holding ``key``, or None when there is none."""
        from .models import Job, JobStatus

        existing = (
            Job.objects.filter(unique_key=key, status__in=[JobStatus.PENDING, JobStatus.RUNNING])
            .order_by("-created_at")
            .first()
        )
        if existing is None:
            return None, None
        run = existing.runs.order_by("-attempt").first()
        if run is None:
            return existing, None
        return existing, self.get_backend().get_result(run.result_id)

    def enqueue(self, *args, **kwargs):
        from . import recorders

        key = self._unique_key(args, kwargs)
        if not key:
            # Explicit base call: ``slots=True`` dataclasses recreate the class, which breaks
            # the zero-argument ``super()``.
            return DjangoTask.enqueue(self, *args, **kwargs)

        from django.db import IntegrityError, transaction

        from .models import Job, JobStatus

        existing, result = self._existing_result(key)
        if result is not None:
            return result
        policy = registry.get_policy(self.module_path)
        record_args = conf.get_setting("OVERSEER_RECORD_ARGS")
        with transaction.atomic():
            try:
                with transaction.atomic():
                    job = Job.objects.create(
                        task_path=self.module_path,
                        task_name=self.name,
                        backend=self.backend,
                        queue_name=self.queue_name,
                        priority=self.priority,
                        args=recorders.json_safe(list(args)) if record_args else [],
                        kwargs=recorders.json_safe(dict(kwargs)) if record_args else {},
                        status=JobStatus.PENDING,
                        max_retries=policy.retries,
                        unique_key=key,
                        tags=list(policy.tags),
                    )
            except IntegrityError:
                # Lost the race: another process just reserved this key.
                existing, result = self._existing_result(key)
                if result is not None:
                    return result
                if existing is None:  # pragma: no cover - it finished in between; start over
                    return self.enqueue(*args, **kwargs)
                # Its enqueue is committed but not yet recorded; attach a run to its job.
                job = existing
            with recorders.enqueue_context(unique_key=key, job=job):
                return DjangoTask.enqueue(self, *args, **kwargs)


def task(
    function=None,
    *,
    retries: int | None = None,
    backoff: str | None = None,
    backoff_base: float | None = None,
    backoff_max: float | None = None,
    jitter: bool | None = None,
    retry_on: tuple[type[BaseException], ...] | None = None,
    timeout: int | None = None,
    tags: tuple[str, ...] | list[str] = (),
    unique=False,
    priority: int = DEFAULT_TASK_PRIORITY,
    queue_name: str = DEFAULT_TASK_QUEUE_NAME,
    backend: str = DEFAULT_TASK_BACKEND_ALIAS,
    takes_context: bool = False,
):
    """Declare a task exactly like ``django.tasks.task``, plus Overseer's retry policy.

    ``retries``: attempts after the first; ``backoff``: exponential (default), linear or
    constant, in seconds from ``backoff_base`` up to ``backoff_max``; ``retry_on``: exception
    classes that trigger a retry (default: any ``Exception``); ``timeout``: seconds after
    which a running attempt is considered abandoned; ``unique``: True to collapse identical
    pending enqueues, a string key, or a callable building the key from the arguments.
    """

    def wrap(f):
        django_decorated = django_task(
            priority=priority, queue_name=queue_name, backend=backend, takes_context=takes_context
        )(f)
        t = OverseerTask(
            func=django_decorated.func,
            priority=django_decorated.priority,
            queue_name=django_decorated.queue_name,
            backend=django_decorated.backend,
            takes_context=django_decorated.takes_context,
            run_after=django_decorated.run_after,  # required in 6.0, defaulted in 6.1
        )
        defaults = registry.get_policy("__defaults__")
        policy = TaskPolicy(
            retries=defaults.retries if retries is None else retries,
            backoff=defaults.backoff if backoff is None else backoff,
            backoff_base=defaults.backoff_base if backoff_base is None else backoff_base,
            backoff_max=defaults.backoff_max if backoff_max is None else backoff_max,
            jitter=defaults.jitter if jitter is None else jitter,
            retry_on=tuple(retry_on) if retry_on else (Exception,),
            timeout=defaults.timeout if timeout is None else timeout,
            tags=tuple(tags),
            unique=unique,
            declared=True,
            backend=t.backend,
        )
        registry.register(t.module_path, policy)
        return t

    return wrap(function) if function is not None else wrap


def schedule(
    cron: str | None = None,
    *,
    every: int | None = None,
    name: str | None = None,
    args: list | tuple = (),
    kwargs: dict | None = None,
    queue_name: str | None = None,
    priority: int | None = None,
    backend: str | None = None,
    timezone: str | None = None,
):
    """Enqueue the decorated task on a schedule: ``@overseer.schedule("*/5 * * * *")`` or
    ``@overseer.schedule(every=300)``. Apply above ``overseer.task`` / ``django.tasks.task``."""
    if (cron is None) == (every is None):
        raise ValueError("schedule() needs exactly one of a cron expression or every=seconds")

    def wrap(t):
        if not isinstance(t, DjangoTask):
            raise TypeError("overseer.schedule must decorate a Task (apply it above @task)")
        schedule_registry.register(
            name=name or t.module_path,
            task_path=t.module_path,
            cron=cron or "",
            interval_seconds=every,
            args=list(args),
            kwargs=dict(kwargs or {}),
            queue_name=queue_name or "",
            priority=priority,
            backend=backend or "",
            timezone=timezone or "",
        )
        return t

    return wrap

"""Per-task policies (retries, timeout, tags, uniqueness) keyed by the task's module path."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import conf

BACKOFF_STRATEGIES = ("exponential", "linear", "constant")


@dataclass(frozen=True)
class TaskPolicy:
    retries: int = 0
    backoff: str = "exponential"
    backoff_base: float = 30.0
    backoff_max: float = 3600.0
    jitter: bool = True
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    timeout: int | None = None
    tags: tuple[str, ...] = ()
    unique: bool | str | Callable[..., str] = False
    declared: bool = field(default=False, compare=False)
    backend: str = field(default="default", compare=False)

    def __post_init__(self):
        if self.backoff not in BACKOFF_STRATEGIES:
            raise ValueError(f"backoff must be one of {BACKOFF_STRATEGIES}, got {self.backoff!r}")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")


_policies: dict[str, TaskPolicy] = {}


def register(task_path: str, policy: TaskPolicy) -> None:
    _policies[task_path] = policy


def get_policy(task_path: str) -> TaskPolicy:
    """The declared policy for ``task_path``, or one built from ``OVERSEER_DEFAULT_*`` settings."""
    try:
        return _policies[task_path]
    except KeyError:
        return TaskPolicy(
            retries=conf.get_setting("OVERSEER_DEFAULT_RETRIES"),
            backoff=conf.get_setting("OVERSEER_DEFAULT_BACKOFF"),
            backoff_base=conf.get_setting("OVERSEER_DEFAULT_BACKOFF_BASE"),
            backoff_max=conf.get_setting("OVERSEER_DEFAULT_BACKOFF_MAX"),
            jitter=conf.get_setting("OVERSEER_DEFAULT_JITTER"),
            timeout=conf.get_setting("OVERSEER_DEFAULT_TIMEOUT"),
        )


def all_policies() -> dict[str, TaskPolicy]:
    return dict(_policies)


def clear() -> None:  # tests
    _policies.clear()

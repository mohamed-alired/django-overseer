"""Backend adapters: optional operations on the underlying task store.

Overseer records everything through signals, which any backend provides. Cancelling a
pending task or resetting a stuck one, however, needs backend knowledge. Adapters add
those where possible; the generic adapter reports them as unsupported.
"""

from __future__ import annotations

from django.tasks import task_backends
from django.utils.module_loading import import_string

from ..exceptions import AdapterUnsupported

ADAPTERS = {
    "django_tasks_db.backend.DatabaseBackend": "overseer.adapters.django_tasks_db.DatabaseAdapter",
}


class BaseAdapter:
    supports_cancel = False
    supports_reset = False

    def __init__(self, alias: str):
        self.alias = alias
        self.backend = task_backends[alias]

    def cancel(self, run) -> bool:
        """Remove a not-yet-started task from the backend. Returns True if it was removed."""
        raise AdapterUnsupported(f"backend {self.alias!r} cannot cancel tasks")

    def reset(self, run) -> bool:
        """Mark a stuck task failed in the backend so workers never pick it up again."""
        raise AdapterUnsupported(f"backend {self.alias!r} cannot reset tasks")

    def queue_depth(self, queue_name: str | None = None) -> int | None:
        """Number of tasks waiting in the backend, or None when unknown."""
        return None


class GenericAdapter(BaseAdapter):
    pass


def get_adapter(alias: str) -> BaseAdapter:
    backend = task_backends[alias]
    path = f"{type(backend).__module__}.{type(backend).__qualname__}"
    for klass in type(backend).__mro__:
        key = f"{klass.__module__}.{klass.__qualname__}"
        if key in ADAPTERS:
            return import_string(ADAPTERS[key])(alias)
    if path in ADAPTERS:  # pragma: no cover
        return import_string(ADAPTERS[path])(alias)
    return GenericAdapter(alias)

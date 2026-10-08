"""django-overseer: dashboard, retries and schedules for Django's Tasks framework."""

__version__ = "0.1.2"


def __getattr__(name):
    # Lazy so that importing ``overseer`` never pulls in Django before setup.
    if name in {"task", "schedule"}:
        from . import decorators

        return getattr(decorators, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["__version__", "schedule", "task"]

from decimal import Decimal

from django.core.checks import Error, Tags, Warning, register
from django.tasks import task_backends

from . import conf, registry


@register(Tags.compatibility)
def check_backends_can_defer(app_configs, **kwargs):
    """Retries with a delay need ``supports_defer``; warn per backend that lacks it."""
    warnings = []
    # Only policies that actually wait between attempts need a deferring backend.
    aliases = {
        p.backend for p in registry.all_policies().values() if p.retries and p.backoff_base > 0
    }
    if conf.get_setting("OVERSEER_DEFAULT_RETRIES") and conf.get_setting(
        "OVERSEER_DEFAULT_BACKOFF_BASE"
    ):
        aliases.add("default")
    for alias in sorted(aliases):
        try:
            backend = task_backends[alias]
        except Exception:  # unknown alias: Django's own checks report it
            continue
        if not backend.supports_defer:
            warnings.append(
                Warning(
                    f"Task backend {alias!r} cannot defer tasks, so Overseer retries on it run "
                    "immediately instead of after the configured backoff.",
                    id="overseer.W001",
                )
            )
    return warnings


@register(Tags.compatibility)
def check_settings(app_configs, **kwargs):
    errors = []
    backoff = conf.get_setting("OVERSEER_DEFAULT_BACKOFF")
    if backoff not in registry.BACKOFF_STRATEGIES:
        errors.append(
            Error(
                f"OVERSEER_DEFAULT_BACKOFF must be one of {registry.BACKOFF_STRATEGIES}.",
                id="overseer.E001",
            )
        )
    perm = conf.get_setting("OVERSEER_PERMISSION")
    if perm and "." not in perm:
        errors.append(
            Error("OVERSEER_PERMISSION must be '<app_label>.<codename>'.", id="overseer.E002")
        )
    for name in ("OVERSEER_RETENTION_DAYS", "OVERSEER_METRICS_RETENTION_DAYS"):
        if conf.get_setting(name) < 1:
            errors.append(Error(f"{name} must be at least 1.", id="overseer.E003"))
    for name in NUMERIC_SETTINGS:
        value = conf.get_setting(name)
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal) or value < 0:
            errors.append(Error(f"{name} must be a non-negative number.", id="overseer.E004"))
        elif name == "OVERSEER_RETRY_ALL_LIMIT" and value < 1:
            errors.append(Error(f"{name} must be at least 1.", id="overseer.E004"))
    token = conf.get_setting("OVERSEER_HEALTH_TOKEN")
    if token is not None and not isinstance(token, str):
        errors.append(Error("OVERSEER_HEALTH_TOKEN must be a string or None.", id="overseer.E005"))
    elif token and len(token) < 16:
        errors.append(
            Warning(
                "OVERSEER_HEALTH_TOKEN is shorter than 16 characters; use a longer secret.",
                id="overseer.W003",
            )
        )
    return errors


NUMERIC_SETTINGS = (
    "OVERSEER_DEFAULT_RETRIES",
    "OVERSEER_DEFAULT_BACKOFF_BASE",
    "OVERSEER_DEFAULT_BACKOFF_MAX",
    "OVERSEER_STALE_AFTER",
    "OVERSEER_WORKER_OFFLINE_AFTER",
    "OVERSEER_SILENT_WORKER_AFTER",
    "OVERSEER_MAX_TRACEBACK_CHARS",
    "OVERSEER_REFRESH_SECONDS",
    "OVERSEER_RETRY_ALL_LIMIT",
    "OVERSEER_ALERT_WINDOW_MINUTES",
    "OVERSEER_ALERT_FAILURE_RATE",
    "OVERSEER_ALERT_WAIT_SECONDS",
    "OVERSEER_ALERT_QUEUE_DEPTH",
    "OVERSEER_ALERT_COOLDOWN_MINUTES",
    "OVERSEER_SCHEDULER_INTERVAL",
    "OVERSEER_RESCUE_INTERVAL",
    "OVERSEER_MAINTENANCE_INTERVAL",
)


@register(Tags.database)
def check_unique_enforcement(app_configs, databases=None, **kwargs):
    """``unique=True`` relies on a partial unique index; warn where the database has none."""
    from django.db import connections

    warnings = []
    for alias in databases or []:
        if not connections[alias].features.supports_partial_indexes:
            warnings.append(
                Warning(
                    f"Database {alias!r} does not support partial unique indexes, so Overseer "
                    "cannot guarantee that two identical unique=True tasks are never active at "
                    "once there.",
                    id="overseer.W002",
                )
            )
    return warnings

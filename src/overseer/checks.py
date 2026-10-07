from django.core.checks import Error, Tags, Warning, register
from django.tasks import task_backends

from . import conf, registry


@register(Tags.compatibility)
def check_backends_can_defer(app_configs, **kwargs):
    """Retries with a delay need ``supports_defer``; warn per backend that lacks it."""
    warnings = []
    aliases = {p.backend for p in registry.all_policies().values() if p.retries}
    if conf.get_setting("OVERSEER_DEFAULT_RETRIES"):
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
    return errors

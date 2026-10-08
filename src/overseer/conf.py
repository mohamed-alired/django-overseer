"""Lazy access to ``OVERSEER_*`` settings; every value is read at call time."""

from django.conf import settings

DEFAULTS = {
    # Retry defaults, used when a task is not declared with ``overseer.task``.
    "OVERSEER_DEFAULT_RETRIES": 0,
    "OVERSEER_DEFAULT_BACKOFF": "exponential",  # exponential | linear | constant
    "OVERSEER_DEFAULT_BACKOFF_BASE": 30.0,  # seconds
    "OVERSEER_DEFAULT_BACKOFF_MAX": 3600.0,
    "OVERSEER_DEFAULT_JITTER": True,
    "OVERSEER_DEFAULT_TIMEOUT": None,  # seconds; None = no timeout
    # A running run with no timeout is treated as abandoned after this many seconds.
    "OVERSEER_STALE_AFTER": 3600,
    # A worker not seen for this many seconds is shown as offline.
    "OVERSEER_WORKER_OFFLINE_AFTER": 120,
    # Retention, applied by ``overseer_prune``.
    "OVERSEER_RETENTION_DAYS": 14,
    "OVERSEER_METRICS_RETENTION_DAYS": 30,
    # Store task args/kwargs on the Job (set False for sensitive payloads).
    "OVERSEER_RECORD_ARGS": True,
    "OVERSEER_MAX_TRACEBACK_CHARS": 20_000,
    # Dashboard access: staff plus this permission codename on Job.
    "OVERSEER_PERMISSION": "overseer.view_dashboard",
    "OVERSEER_REFRESH_SECONDS": 5,
    # Lets uptime checks call /api/health/ with "Authorization: Bearer <token>".
    "OVERSEER_HEALTH_TOKEN": None,
    # "Retry all" retries at most this many failed jobs per click.
    "OVERSEER_RETRY_ALL_LIMIT": 200,
    # Alerts.
    "OVERSEER_ALERT_WINDOW_MINUTES": 5,
    "OVERSEER_ALERT_FAILURE_RATE": 0.25,
    "OVERSEER_ALERT_WAIT_SECONDS": 60,
    "OVERSEER_ALERT_QUEUE_DEPTH": 1000,
    "OVERSEER_ALERT_COOLDOWN_MINUTES": 15,
    "OVERSEER_NOTIFIERS": [],  # dotted paths to callables(alert)
    "OVERSEER_ALERT_EMAILS": [],
    "OVERSEER_SLACK_WEBHOOK_URL": None,
    # Scheduler.
    "OVERSEER_SCHEDULER_INTERVAL": 1.0,
    # How often the scheduler loop looks for abandoned runs (seconds).
    "OVERSEER_RESCUE_INTERVAL": 30.0,
    # How often the scheduler loop rolls up metrics and evaluates alerts (seconds).
    "OVERSEER_MAINTENANCE_INTERVAL": 60.0,
    "OVERSEER_SCHEDULER_TIMEZONE": None,  # None = settings.TIME_ZONE
    "OVERSEER_AUTODISCOVER": True,  # import <app>.tasks for every installed app
    "OVERSEER_TASK_MODULES": [],  # extra dotted modules to import at startup
}


def get_setting(name):
    return getattr(settings, name, DEFAULTS[name])

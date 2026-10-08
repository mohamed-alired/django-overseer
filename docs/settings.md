# Settings reference

Every setting is optional and read when it is used, so `override_settings` works in tests.

## Retry policy defaults

These apply to tasks declared with plain `django.tasks.task`, and fill any argument left out
of `overseer.task(...)`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_DEFAULT_RETRIES` | `0` | attempts after the first |
| `OVERSEER_DEFAULT_BACKOFF` | `"exponential"` | `exponential`, `linear` or `constant` |
| `OVERSEER_DEFAULT_BACKOFF_BASE` | `30.0` | seconds |
| `OVERSEER_DEFAULT_BACKOFF_MAX` | `3600.0` | cap in seconds |
| `OVERSEER_DEFAULT_JITTER` | `True` | multiply the delay by a random factor between 0.8 and 1.2 |
| `OVERSEER_DEFAULT_TIMEOUT` | `None` | seconds before a running attempt is considered abandoned |

Delay before attempt *n* (n ≥ 2):

- exponential: `min(base * 2 ** (n - 2), max)`
- linear: `min(base * (n - 1), max)`
- constant: `min(base, max)`

## Rescue and workers

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_STALE_AFTER` | `3600` | a running attempt whose task has no `timeout` is abandoned after this many seconds |
| `OVERSEER_WORKER_OFFLINE_AFTER` | `120` | an `overseer_worker` silent for this long, or for three heartbeat intervals if longer, is offline (and alerts, unless it stopped cleanly). Workers without heartbeats (plain `db_worker`) are never judged offline |
| `OVERSEER_SILENT_WORKER_AFTER` | `3600` | a worker without heartbeats (plain `db_worker`) that has not run a task for this long leaves the worker counts and shows as "silent"; such rows are pruned after a day |
| `OVERSEER_RESCUE_INTERVAL` | `30.0` | seconds between rescue passes in the scheduler loop; a pass also marks waiting runs whose backend task disappeared as lost, and records results whose signal never arrived |

## Retention

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_RETENTION_DAYS` | `14` | `overseer_prune` deletes finished jobs (with their runs), resolved alerts and silent workers older than this (the setting must be at least 1; `overseer_prune --days 0` deletes everything finished) |
| `OVERSEER_METRICS_RETENTION_DAYS` | `30` | metric buckets older than this are deleted |
| `OVERSEER_RECORD_ARGS` | `True` | store task args and kwargs on the `Job` |
| `OVERSEER_MAX_TRACEBACK_CHARS` | `20000` | tracebacks are truncated to this length |

## Dashboard

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_PERMISSION` | `"overseer.view_dashboard"` | permission required in addition to `is_staff`; `None` means any staff user |
| `OVERSEER_REFRESH_SECONDS` | `5` | panel auto-refresh interval; `0` disables |
| `OVERSEER_HEALTH_TOKEN` | `None` | when set, `/api/health/` also accepts `Authorization: Bearer <token>` (that endpoint only) |
| `OVERSEER_RETRY_ALL_LIMIT` | `200` | failed jobs retried per "Retry all" click |

Actions always require `overseer.manage_jobs` (retry, cancel, dismiss) or
`overseer.manage_schedules` (pause, resume, run now, sync).

## Alerts

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_ALERT_WINDOW_MINUTES` | `5` | window over which failure rate and waits are measured |
| `OVERSEER_ALERT_FAILURE_RATE` | `0.25` | per-queue failure rate that fires `failure_rate` (needs 5+ finished runs) |
| `OVERSEER_ALERT_WAIT_SECONDS` | `60` | a run that waited longer than this to start, or is still waiting that long, fires `queue_wait`; a run still waiting that long makes `/api/health/` return 503 |
| `OVERSEER_ALERT_QUEUE_DEPTH` | `1000` | more waiting runs than this fires `queue_depth` |
| `OVERSEER_ALERT_COOLDOWN_MINUTES` | `15` | an alert key does not fire again within this time |
| `OVERSEER_NOTIFIERS` | `[]` | dotted paths or callables; each receives the `Alert` |
| `OVERSEER_ALERT_EMAILS` | `[]` | recipients of the alert e-mail, sent from `DEFAULT_FROM_EMAIL`; when empty the mail goes to `ADMINS` via `mail_admins()` |
| `OVERSEER_SLACK_WEBHOOK_URL` | `None` | incoming webhook URL |
| `OVERSEER_MAINTENANCE_INTERVAL` | `60.0` | seconds between rollup + alert evaluation in the scheduler loop |

## Scheduler

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_SCHEDULER_INTERVAL` | `1.0` | seconds between checks for due schedules |
| `OVERSEER_SCHEDULER_TIMEZONE` | `None` | IANA name used for cron expressions without their own `timezone`; `None` means `TIME_ZONE` |

## Discovery

| Setting | Default | Meaning |
| --- | --- | --- |
| `OVERSEER_AUTODISCOVER` | `True` | import `tasks` from every installed app at startup |
| `OVERSEER_TASK_MODULES` | `[]` | extra dotted module paths to import at startup |

Discovery matters because policies and schedules are Python declarations: the worker, the
scheduler and the web process each need to import them.

## System checks

- `overseer.E001`–`E003`: invalid `OVERSEER_DEFAULT_BACKOFF`, a `OVERSEER_PERMISSION` that
  is not `app_label.codename`, or a retention setting below 1.
- `overseer.E004`/`E005`: a numeric setting that is not a number, or an `OVERSEER_HEALTH_TOKEN`
  that is not a string (`""` and `None` both mean "no token"); `W003` when the token is
  shorter than 16 characters.
- `overseer.W002`: the database has no partial unique indexes (MySQL, MariaDB, Oracle), so
  `unique=True` cannot be guaranteed there.
- `overseer.W001`: a task with delayed retries (`backoff_base` above 0) uses a backend
  whose `supports_defer` is false, so backoff delays cannot be honoured and retries run
  immediately.

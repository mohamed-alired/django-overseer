# Changelog

## [0.1.1] - 2026-10-08

Bug fixes from a full review of 0.1.0. Run `python manage.py migrate` (migration 0002 adds
`Schedule.last_error`, `Schedule.missing_from_code` and `Worker.heartbeat_seconds`).

### Retries and unique tasks
- Unique tasks are retried again. A retry went through the uniqueness check, got the
  failed attempt back, and left the job pending with its key locked for good.
- Retries work with `OVERSEER_RECORD_ARGS = False`: the arguments come from the backend's
  copy of the failed attempt. Before, the task was called with no arguments.
- Tasks enqueued by a task that the immediate backend runs inline get their own jobs; they
  were recorded as attempts of the parent (and could make it retry).
- `aenqueue()` honours `unique=True`; scheduled unique jobs keep their schedule and source.
- Manual retry only applies to failed or cancelled jobs, and explains why when the task no
  longer imports or another active job holds the unique key (instead of a 500).
- A unique job left without runs by a failed recorder no longer holds its key.
- Recorders run in a savepoint; worker counters are updated atomically.

### Scheduler and cron
- In the repeated autumn hour, cron no longer computes a next run in the past, which made
  a `*/5` schedule fire on every scheduler tick for up to an hour. Daylight-saving handling
  now follows vixie-cron and is checked against a brute-force matcher.
- Times inside the spring gap fire shifted even when the scheduler starts just after it.
- `*/N` in a day field counts as unrestricted for the day-of-month OR day-of-week rule.
- One schedule that can never fire (impossible date, unknown timezone, no interval) no
  longer stops every schedule and crashes the scheduler: it is disabled, with the reason
  on the Schedules page. Enqueue errors are shown there too. Declarations are validated at
  import; the scheduler loop survives any error.
- Sync re-enables a declaration that comes back, leaves hand-made rows and pauses alone,
  disables nothing when no declarations are loaded, and is idempotent for tuple arguments.
- Hand-made schedules without `next_run_at` get one; "run now" counts atomically.

### Alerts, health and retention
- Plain `db_worker` processes no longer raise false `worker_offline` alerts or make the
  health check fail: only `overseer_worker` (heartbeats) can be judged offline.
- `/api/health/` returns 503 when a ready task waits longer than
  `OVERSEER_ALERT_WAIT_SECONDS`, and accepts `Authorization: Bearer <OVERSEER_HEALTH_TOKEN>`.
- `queue_wait` also fires for runs no worker has picked up.
- An unimportable notifier or a raising `alert_fired` receiver no longer breaks alerting.
- `overseer_prune --days 0` is honoured, negative days are refused, open alerts are kept.
- Metric rollups upsert, so two schedulers cannot collide; `overseer_rollup --since`
  accepts dates and naive datetimes and rejects garbage.

### Dashboard and API
- Actions only redirect to a `next` URL on the same site (open redirect).
- Jobs pagination keeps the filters (links rendered as `??`); the time-window selector
  labels read correctly and keep other filters.
- Cancelled runs no longer count as processed or dilute failure rates.
- "Retry all" skips jobs it cannot retry and works in batches
  (`OVERSEER_RETRY_ALL_LIMIT`); "Dismiss" reports when nothing was dismissed.
- The JSON API validates `per_page` and answers unknown job ids with a JSON 404.
- The jobs list counts without joining runs; queue depths take one query per backend.
- Job detail refreshes status and actions; an expired session no longer looks fresh.
- Documentation: removed a claim about an `ENQUEUE_ON_COMMIT` setting that Django's tasks
  framework does not have.

## [0.1.0] - 2026-10-07

First release.

- Recording of every `django.tasks` enqueue, start and finish into `Job`, `Run` and
  `Worker` rows through the framework's signals; works with any backend.
- `overseer.task`: retries with exponential, linear or constant backoff, jitter, cap,
  `retry_on` filters, per-task timeouts, tags, and `unique` enqueues enforced by a database
  constraint.
- `overseer.schedule`: cron expressions (dependency-free parser, timezone aware) and fixed
  intervals, synced into the `Schedule` table and safe across several scheduler processes.
- `overseer_scheduler`, `overseer_worker` (heartbeats), `overseer_rescue`,
  `overseer_rollup`, `overseer_alerts`, `overseer_prune` and `overseer_sync_schedules`
  management commands.
- Rescue of abandoned runs with backend reset for `django-tasks-db`.
- Dashboard: overview, queues, tasks, jobs, job detail, failed, schedules, workers, metrics
  and alerts pages with retry / cancel / dismiss / pause / run-now actions, auto refresh and
  a JSON API with a health endpoint.
- Alerts for failure rate, queue wait, queue depth and offline workers with cooldowns,
  resolution, e-mail, Slack and custom notifiers, plus `alert_fired`, `alert_resolved` and
  `job_failed` signals.
- Per-minute metric rollups and retention pruning.
- System checks for settings and non-deferring backends.
- Test suite in four layers (unit, real-process integration, HTTP end-to-end, performance
  budgets) on SQLite and PostgreSQL, Django 6.0 and 6.1.

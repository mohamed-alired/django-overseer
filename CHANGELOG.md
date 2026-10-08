# Changelog

## [Unreleased]

- A job page with a long traceback line was wider than the window and scrolled sideways;
  tracebacks now wrap, and wide tables scroll inside their panel.
- Screenshots in the README.

## [0.1.4] - 2026-10-08

- `overseer_worker --reload` (the default when `DEBUG` is on) recorded no stop when it got
  SIGTERM: Django's autoreloader kills the worker process on its way out. The reloader's
  parent process now records it.
- An example project, `examples/demo`, to try the dashboard locally.

## [0.1.3] - 2026-10-08

Fixes from a review of 0.1.2. No new migration.

### Scheduling
- With `USE_TZ = False`, a cron schedule fired on every scheduler tick during the hour that
  repeats when daylight saving time ends, because its next run came out earlier than the
  current time. Next runs are now always later than the current time.
- Interval schedules add real (UTC) time across daylight-saving changes.
- `overseer_sync_schedules`, and `overseer_scheduler --once`, exit with an error when a
  schedule cannot be synced, so deploy scripts notice. A row that sync repaired is reported
  as updated.
- The scheduler recomputes the last two hours of metrics once an hour, and every rollup
  goes back 15 minutes, so runs committed late (long transactions) are counted.

### Runs and workers
- A run marked lost whose task then ran after all (its backend row became visible late, on
  another database for instance) is reopened and its real outcome recorded. When the
  backend's rows live in another database than Overseer's, waiting runs are only called
  lost after `OVERSEER_STALE_AFTER`.
- A lost retry keeps the job's attempt count.
- `unique=True` no longer hands back a result that finished while its signal was lost: the
  outcome is recorded and a new task is enqueued.
- Recording a result late (the rescue pass) no longer marks a stopped worker as running
  again, which raised a false "worker offline" alert.
- An attempt interrupted by a forced stop no longer counts as a failure of the worker.
- `HeartbeatWorker.run()` can be called more than once: each run heartbeats and records
  its stop. A worker row created by a task before the worker registered gets its
  heartbeat settings on the next beat.

### Dashboard
- A `next` value that is not a path on the site (a bare word) gave a server error; it now
  falls back to the default page.
- "Retry all" no longer walks every job of a task that no longer exists on each click, and
  a missing or invalid `OVERSEER_RETRY_ALL_LIMIT` falls back to 200 instead of failing.
- Actions no longer fail after the fact when the messages framework is not installed; the
  new system check `overseer.W004` names any missing part of the dashboard's setup
  (messages app, middleware and context processor, `request` context processor).
- Pages with several auto-refreshing regions (the job page) fetch the page once per
  refresh instead of once per region.
- Alert messages for an `OVERSEER_ALERT_WINDOW_MINUTES` over a day showed the wrong number
  of minutes.
- `overseer_rollup --since` with an impossible date (month 13, 30 February) reports it
  instead of failing with a traceback.

## [0.1.2] - 2026-10-08

Fixes from a review of 0.1.1, two of them regressions introduced there. No new migration.

### Regressions in 0.1.1
- Metric rollups, and with them alert evaluation, failed on MySQL, MariaDB and Oracle
  because the upsert they used is not available there. The rollup now falls back to a
  plain insert on those databases, and CI runs the suite on MariaDB.
- A `job_failed` receiver that raised rolled back the recording of the failure, leaving
  the job "running" until rescue abandoned it an hour later. Receivers are now called
  robustly and their errors logged.
- Tasks that take no arguments could not be retried with `OVERSEER_RECORD_ARGS = False`.

### Reliability
- Waiting runs whose backend task disappeared (deleted row, restored database) are marked
  lost by the rescue pass, which also records results whose signal never arrived. Health,
  `queue_wait` and unique keys no longer stay stuck on them.
- `unique=True` no longer breaks on backends that cannot look results up (the immediate
  backend re-enqueueing its own key, the dummy backend after a restart): the key is simply
  not enforced there, with a warning.
- "Retry all" no longer gets stuck behind jobs it cannot retry; those do not use up the
  per-click budget.
- The failure-rate alert excludes cancelled runs, as the dashboard already did.
- A schedule disabled by Overseer because it could not fire is enabled again once its
  declaration is fixed. `overseer_sync_schedules --enable` re-enables declared schedules
  that versions before 0.1.1 disabled without a marker.
- Plain `db_worker` rows (a new random id per start, never stopped) leave the worker counts
  once silent for `OVERSEER_SILENT_WORKER_AFTER` (new setting, an hour) and are pruned
  after a day.
- Cron is as fast as 0.1.0 again away from daylight-saving changes.

### Also
- Projects with `USE_TZ = False` could not use cron schedules at all (every one was
  reported as an error and disabled). Naive local datetimes are now handled throughout,
  including `overseer_rollup --since`.
- `overseer_worker --reload` (the default when `DEBUG` is on) keeps one worker id across
  restarts and records each stop, instead of leaving a never-stopped "offline" worker
  behind on every code change.
- Overseer's tables can live on a non-default database alias through a router: locking
  transactions now open on that alias (PostgreSQL raised a transaction error before).
- A task interrupted by a forced worker stop (`SystemExit`) is recorded as abandoned and
  retried under its policy instead of counting as a task failure.
- A heartbeat whose row was pruned or never created re-creates it, so the worker is not
  stuck as "no heartbeat"; a database outage logs one traceback, then one line per beat.
- `overseer_worker` and `overseer_scheduler` log to the console when the project has no
  logging configuration for the `overseer` logger.

### Dashboard and API
- The job page heading refreshes with the rest of the page.
- Cancelling a job whose backend alias was removed and a NUL byte in a filter on
  PostgreSQL no longer give a 500; a `None` alert threshold is treated as 0 (and flagged
  by system check `E004`).
- The health token's `Bearer` scheme is case-insensitive; 401 responses carry
  `WWW-Authenticate` and health responses `Cache-Control: no-store`.
- The Metrics page window labels read like the other pages'.
- New system checks: `E004`/`E005` for mistyped settings, `W002` where the database cannot
  enforce unique tasks (MySQL, MariaDB, Oracle), `W003` for a short health token.
- Documentation corrected where it promised columns the Queues, Tasks and Job pages do not
  have, and `?minutes=` on pages that do not take it.

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

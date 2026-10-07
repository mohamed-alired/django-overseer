# Changelog

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

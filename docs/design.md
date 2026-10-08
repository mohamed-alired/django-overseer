# Design

## Goals

1. Work with any `django.tasks` backend, using only the public signals and `Task` API.
2. Never interfere with task execution: no monkeypatching of workers, no wrapping of task
   functions, no changes to what `enqueue()` returns.
3. Never break the host application: a failure inside Overseer is logged and swallowed.
4. Stay a plain Django app: models, views, templates, management commands; nothing to build.

## Data model

| Model | One row per | Notes |
| --- | --- | --- |
| `Job` | logical unit of work | task path, arguments, policy snapshot (`max_retries`), status `PENDING → RUNNING → SUCCEEDED / FAILED / CANCELLED`, `attempts`, `unique_key`, `source` (enqueue, retry, schedule, manual), `dismissed` |
| `Run` | attempt | backend result id, `attempt` number, `READY → RUNNING → SUCCESSFUL / FAILED / ABANDONED / CANCELLED`, timings (`wait_ms`, `duration_ms`), worker id, exception class and traceback, return value, `retry_of` chain |
| `Worker` | worker process | id, host, pid, queues, heartbeats, current run, counters, `stopped_at` (clean stop vs. crash) |
| `Schedule` | schedule | cron or interval, task path and arguments, `next_run_at`, `declared_in_code` |
| `MetricBucket` | minute × queue × task (`""` = all tasks) | counts, runtime sum / p50 / p95 / max, wait sum / max |
| `Alert` | fired condition | kind, key, value, threshold, `resolved_at` |

A unique partial index on `Job.unique_key` for pending and running jobs makes `unique=True`
safe under concurrency: the enqueue reserves the `Job` row first, so the loser of a race
gets an `IntegrityError` and returns the winner's result instead of enqueueing twice.

## Recording

`overseer.recorders` connects to the three signals in `AppConfig.ready()`:

- `task_enqueued` creates a `Job` (unless the enqueue context names one: retries and
  schedules attach to an existing job) and a `READY` run. It is idempotent on the result id.
- `task_started` marks the run `RUNNING`, records the wait, and touches the `Worker` row.
- `task_finished` records status, duration, traceback and return value, then either marks
  the job `SUCCEEDED` or hands it to the retry logic.

The enqueue context is a `contextvars.ContextVar` set by whoever enqueues with extra
knowledge (`enqueue_retry`, `enqueue_schedule`, `OverseerTask.enqueue`). Every backend sends
`task_enqueued` synchronously from `enqueue()`, so the context is still set when the
recorder reads it. The recorder marks it consumed: a context describes exactly one enqueue,
so tasks enqueued later in the same scope, for example by a task body that the immediate
backend runs inline, are recorded as jobs of their own.

Each recorder runs in a savepoint, so a database error inside Overseer cannot break a
transaction the caller has open.

Recorders are wrapped so that any exception is logged with the result id and swallowed.

## Retries

Retry decisions are made in the `task_finished` receiver, inside the worker. The next
attempt is a real `Task.enqueue` of `task.using(run_after=...)`, so it goes through the
backend like any other task and is visible immediately, with no polling process in between.
It deliberately bypasses `OverseerTask.enqueue`: the job already holds its unique key, and
the uniqueness check would hand back the failed attempt. The arguments come from the
backend's copy of the failed attempt (exact, and present even with
`OVERSEER_RECORD_ARGS = False`), falling back to the ones recorded on the job. The job is marked `PENDING`
*before* the enqueue so that a synchronous backend (the immediate backend) running the retry
inline sets the final status last. If the enqueue itself fails, the job is marked `FAILED`.

Backends without `supports_defer` get the retry immediately with a warning; the system check
`overseer.W001` points this out at startup.

## Rescue

A worker that is killed mid-task leaves a `RUNNING` run in Overseer and in the backend. The
rescue pass compares each running run's `started_at` with the task's `timeout` (or
`OVERSEER_STALE_AFTER`). Past the deadline the run becomes `ABANDONED`, the backend adapter
resets the backend row (so the backend will not re-run it either), and the retry policy
decides whether to enqueue another attempt or fail the job.

## Scheduler

`tick()` selects due schedules with `select_for_update(skip_locked=True)`, enqueues each
under an enqueue context carrying the schedule id, and advances `next_run_at` strictly after
*now*. Several schedulers therefore never fire the same occurrence twice, and a schedule that
was missed fires once rather than replaying every missed occurrence.

The cron parser is dependency-free, supports the five standard fields with steps, ranges,
lists, names and aliases, the vixie-cron "day-of-month OR day-of-week" rule, and evaluates in
the schedule's timezone. Around daylight-saving changes wall-clock order and real-time order
disagree, so `next_after()` starts a few hours before the reference time in wall-clock time,
resolves each matching minute to its real instants (two in a repeated hour, for schedules
that run every hour; one, shifted past the gap, for a time that does not exist) and keeps
the earliest instant after the reference, compared in UTC. It is checked against a
brute-force matcher over a year of start times in two zones.

A schedule that can never fire is disabled with the reason in `Schedule.last_error`, inside
its own savepoint, so one bad row never rolls back the others' enqueues.

## Metrics

`rollup()` aggregates finished runs into per-minute buckets for every queue × task plus an
all-tasks row per queue. It recomputes the last few minutes each time so late-finishing runs
land in the right bucket, and includes the current partial minute so the dashboard is live.
Percentiles use the nearest-rank method.

## Dashboard

Plain class-based views over `overseer.stats`, one template per page, a small CSS file and a
dependency-free script that re-fetches panels marked `data-refresh-id` and swaps them in
place. Actions are POST forms with CSRF and Django messages. The JSON API returns the same
`stats` dictionaries.

Access is `is_staff` plus `OVERSEER_PERMISSION`; superusers pass. Mutations need the
`manage_jobs` or `manage_schedules` permission on top.

Query counts are flat: the perf tests assert budgets per page with thousands of jobs, runs
and buckets, so adding an N+1 fails the build.

## Backend adapters

`overseer.adapters.get_adapter(alias)` resolves an adapter by the backend class's MRO.
`BaseAdapter` reports `cancel`, `reset` and `queue_depth` as unsupported; the
`django-tasks-db` adapter implements them against `DBTaskResult`. Adding a backend means one
small class and one entry in `ADAPTERS`.

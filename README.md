# django-overseer

**Dashboard, retries, schedules and alerts for Django's built-in Tasks framework.**

Django 6 ships a Tasks framework (`django.tasks`) and a database-backed worker
(`django-tasks-db`), but no way to *see* what your tasks are doing, no retries, no cron and
no alarm when a queue backs up. Overseer adds the operational layer that every other
ecosystem takes for granted (Sidekiq's web UI, Celery's Flower, Oban's dashboard) without
adding a broker, a JavaScript build or a second framework.

![The Overseer overview page: throughput, failure rate, runtime, waiting and running counts, runs per minute and per-queue figures](https://raw.githubusercontent.com/mohamed-alired/django-overseer/main/docs/images/overview.png)

- **Dashboard**: overview, queues, tasks, jobs with filters and search, job detail with the
  full attempt chain and tracebacks, failed jobs with retry / dismiss, schedules, workers,
  per-minute metrics and alerts. Plain Django views and templates, auto-refreshing, no
  external assets.
- **Retries**: `@overseer.task(retries=3, backoff="exponential")` with constant, linear or
  exponential backoff, jitter, a cap, `retry_on=` exception filters and per-task timeouts.
  Retries are real re-enqueues through `django.tasks`, deferred with `run_after` on backends
  that support it.
- **Unique tasks**: `unique=True` (or a key callable) collapses identical pending enqueues,
  through `enqueue()` and `aenqueue()` alike, enforced by a database constraint so
  concurrent enqueues cannot both win. A unique task's retries keep its key.
- **Schedules**: `@overseer.schedule("*/5 * * * *")` or `every=300`, synced into the database,
  pausable and triggerable from the dashboard, with a scheduler that is safe to run on
  several hosts at once (`SELECT ... FOR UPDATE SKIP LOCKED`).
- **Rescue**: runs whose worker died are detected by timeout, marked abandoned, reset in the
  backend and retried under the task's policy.
- **Workers**: `overseer_worker` wraps `db_worker` with heartbeats, so the dashboard shows
  which workers are alive, on which host, processing what.
- **Alerts**: failure rate, queue wait, queue depth and offline workers, with cooldowns,
  automatic resolution, e-mail, Slack and custom notifiers, plus Django signals.
- **JSON API** and a `/api/health/` endpoint for your own monitoring.
- **Retention**: per-minute metric rollups and pruning of old jobs, runs and alerts.

Everything is recorded through the three `django.tasks` signals, so Overseer works with
**any** task backend. Cancelling pending tasks and resetting stuck ones needs backend
knowledge; an adapter ships for `django-tasks-db` and the interface is open for others.


### Screenshots

The dashboard is its own set of pages at `/overseer/`, separate from the Django admin.
These come from the [example project](https://github.com/mohamed-alired/django-overseer/tree/main/examples/demo).

**A job's attempts.** A card charge that succeeded on its fifth attempt after four
timeouts, retried with exponential backoff; each failed attempt keeps its traceback:

![Job detail page for a charge_card job on attempt 5 of 5, with the first failed attempt and its traceback](https://raw.githubusercontent.com/mohamed-alired/django-overseer/main/docs/images/job.png)

**Failed jobs**, with retry and dismiss for each job or for all of them:

![Failed jobs page with Retry all, Dismiss all and per-job Retry and Dismiss buttons](https://raw.githubusercontent.com/mohamed-alired/django-overseer/main/docs/images/failed.png)

**Schedules**, with pause and run-now:

![Schedules page listing cron and interval schedules with next run, last run, Pause and Run now](https://raw.githubusercontent.com/mohamed-alired/django-overseer/main/docs/images/schedules.png)

## Requirements

- Python 3.12+
- Django 6.0 or 6.1 (tested against `main` too)
- A `django.tasks` backend. The reference setup is `django-tasks-db` (`pip install
  "django-overseer[db]"`).

## Install

```bash
pip install "django-overseer[db]"
```

```python
# settings.py
INSTALLED_APPS = [
    ...
    "django_tasks_db",
    "overseer",
]

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.backend.DatabaseBackend",
        "QUEUES": ["default", "emails"],
    }
}
```

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("overseer/", include("overseer.urls")),
]
```

```bash
python manage.py migrate
```

Then run the processes you need:

```bash
python manage.py overseer_worker --queue-name='*'   # a worker with heartbeats
python manage.py overseer_scheduler                 # schedules, rescue, metrics, alerts
```

The dashboard uses the parts of Django that `startproject` enables by default: sessions,
authentication, `django.contrib.messages` (app, `MessageMiddleware` and the `messages`
context processor) and the `django.template.context_processors.request` context processor.
The system check `overseer.W004` names whichever is missing.

Open `/overseer/` as a staff user with the `overseer.view_dashboard` permission (superusers
always have it). `overseer.manage_jobs` allows retry / cancel / dismiss and
`overseer.manage_schedules` allows pausing and triggering schedules.

To try it first, [`examples/demo`](https://github.com/mohamed-alired/django-overseer/tree/main/examples/demo) is a small shop project with example
tasks, schedules and an admin login.

## Declaring tasks

`overseer.task` is `django.tasks.task` plus a policy. It accepts the same arguments
(`priority`, `queue_name`, `backend`, `takes_context`) and returns a normal `Task`, so
`.enqueue()`, `.using()`, `.call()` and `.get_result()` all work as documented by Django.

```python
import overseer
from django.tasks import task


@overseer.task(retries=3, backoff="exponential", backoff_base=30, backoff_max=600)
def charge_card(order_id):
    ...


@overseer.task(retries=5, retry_on=(ConnectionError, TimeoutError), timeout=120)
def sync_crm(account_id):
    ...


@overseer.task(unique=True, queue_name="emails", tags=("mail",))
def send_receipt(email):
    ...


@overseer.task(unique=lambda report_id, **kwargs: f"report:{report_id}")
def build_report(report_id, force=False):
    ...


@overseer.schedule("0 2 * * *", name="nightly-cleanup", timezone="Europe/Paris")
@overseer.task(retries=1)
def nightly_cleanup():
    ...


@overseer.schedule(every=300)
@task  # schedules work on plain django.tasks tasks as well
def refresh_rates():
    ...
```

| Argument | Meaning |
| --- | --- |
| `retries` | attempts *after* the first (default `OVERSEER_DEFAULT_RETRIES`, 0) |
| `backoff` | `"exponential"` (default), `"linear"` or `"constant"` |
| `backoff_base` / `backoff_max` | seconds; the delay before attempt *n* is `base * 2**(n-2)` capped at `max` for exponential, `base * (n-1)` for linear, `base` for constant |
| `jitter` | multiply the delay by a random factor between 0.8 and 1.2 (default on) |
| `retry_on` | tuple of exception classes that trigger a retry (default: any `Exception`) |
| `timeout` | seconds after which a running attempt is treated as abandoned and rescued |
| `unique` | `True` (key from task path + arguments), a string, or a callable receiving the task arguments |
| `tags` | labels shown in the dashboard |

Tasks declared with plain `django.tasks.task` are recorded too, with the
`OVERSEER_DEFAULT_*` policy. Task modules named `tasks.py` in installed apps are imported at
startup (`OVERSEER_AUTODISCOVER`), so schedules and policies exist in every process.

Policy only ever applies on failure. Overseer never changes what your task does, when it
runs, or what the backend returns.

## Schedules

`overseer.schedule` registers a declaration. `overseer_scheduler` (or `python manage.py
overseer_sync_schedules`) writes it to the `Schedule` table, from which the dashboard can
pause, resume or trigger it. Rows created in the dashboard or admin, with
`declared_in_code=False`, are never touched by sync, even when a declaration has the same
name. A declaration removed from the code disables its row rather than deleting it, so its
history stays, and the row is enabled again if the declaration comes back. A schedule you
paused stays paused. Declarations are validated when they are imported: a bad cron
expression or timezone fails at startup, not at 3 a.m.

A schedule that can never fire (an impossible date such as `0 0 31 2 *`, an unknown
timezone, a row with neither cron nor interval) is disabled by the scheduler with the reason
shown on the Schedules page; it never blocks the other schedules. An enqueue that fails
(the task no longer imports, for example) is shown there too and retried at the next run.

Cron expressions are the standard five fields with ranges, steps, lists, month and weekday
names, `@hourly`-style aliases and the usual "day-of-month OR day-of-week" rule. The
expression is evaluated in the schedule's `timezone`, else `OVERSEER_SCHEDULER_TIMEZONE`,
else `TIME_ZONE`. A schedule that was missed while no scheduler was running fires once and
continues from now; missed occurrences are not replayed.

Daylight-saving changes follow vixie-cron: in the repeated autumn hour, schedules whose
hour field is `*` keep firing by real time, while schedules pinned to hours (`30 1 * * *`,
`0 */2 * * *`) fire once. A time that does not exist in the spring
(`30 2 * * *` in New York on the changeover day) fires at the same offset past the gap. A
day field starting with `*`, including `*/2`, counts as unrestricted for the
day-of-month-or-day-of-week rule.

The scheduler loop also runs the rescue every `OVERSEER_RESCUE_INTERVAL` seconds and the
metrics rollup and alert evaluation every `OVERSEER_MAINTENANCE_INTERVAL` seconds. Run
several schedulers for availability; schedules are claimed with row locks, so each fires
once.

## Commands

| Command | What it does |
| --- | --- |
| `overseer_worker` | `db_worker` with `--heartbeat` seconds (default 10) and a `Worker` row. Accepts every `db_worker` option (`--queue-name`, `--exclude-queues`, `--interval`, `--batch`, `--max-tasks`, `--worker-id`, `--backend`, `--no-startup-delay`, `--reload`). SIGTERM/SIGINT finishes the current task then exits. |
| `overseer_scheduler [--interval S] [--once]` | syncs schedules, fires what is due, rescues abandoned runs, rolls up metrics, evaluates alerts |
| `overseer_sync_schedules` | one-off sync of code declarations into the `Schedule` table |
| `overseer_rescue` | one-off pass over running attempts that exceeded their timeout |
| `overseer_rollup [--since ISO]` | recompute per-minute metric buckets |
| `overseer_alerts` | one-off alert evaluation and notification |
| `overseer_prune [--days N] [--metrics-days N]` | delete finished jobs, runs, resolved alerts, metric buckets and workers not seen within the retention (`--days 0` means everything finished) |

If you run `db_worker` directly instead of `overseer_worker`, tasks, retries, schedules and
the dashboard all work: the worker shows up from the task signals. What you lose is
liveness. A plain `db_worker` is only seen when it runs a task and never reports a clean
stop, so the dashboard shows it as "no heartbeat" and the `worker_offline` alert ignores
it. Use `overseer_worker` when you want to know a worker died.

## Dashboard

| Page | Content |
| --- | --- |
| Overview | throughput, failure rate, runtimes, waiting / running / scheduled counts, workers online, runs-per-minute chart, queue table, recent failures, open alerts |
| Queues | per queue: waiting, scheduled, running, processed, failed, average wait and runtime, and the backend's own depth when the adapter supports it |
| Tasks | per task path: processed, succeeded, failed, failure rate, average and maximum runtime, last run |
| Jobs | filter by status, queue, task, worker; free-text search over task path, job id, result id and unique key |
| Job | arguments, attempts allowed, every attempt with timing, worker, exception and traceback, return value; retry / cancel / dismiss |
| Failed | open failures with the last error; retry all / dismiss all |
| Schedules | next and last run, run count, pause / resume / run now / sync |
| Workers | host, pid, queues, last seen, current run, counters; a heartbeat worker is offline after `OVERSEER_WORKER_OFFLINE_AFTER` or three missed beats, whichever is longer; a plain `db_worker` shows "no heartbeat", or "silent" once it has not run anything for `OVERSEER_SILENT_WORKER_AFTER` |
| Metrics | per-minute runs and p95 runtime for the last 15 minutes to 24 hours, per queue |
| Alerts | open and resolved alerts |

Overview, Queues, Tasks and Metrics accept `?minutes=15|60|360|1440`. Panels refresh every
`OVERSEER_REFRESH_SECONDS` seconds (there is a pause button). The same data is available as
JSON under `/overseer/api/...` for the logged-in user, plus `/overseer/api/health/`, which
returns HTTP 503 when a ready task has waited longer than `OVERSEER_ALERT_WAIT_SECONDS`,
that is, when tasks are not being picked up, whatever kind of worker you run. Set
`OVERSEER_HEALTH_TOKEN` to let an uptime checker call it without a session:

```bash
curl -H "Authorization: Bearer $OVERSEER_HEALTH_TOKEN" https://example.com/overseer/api/health/
```

The token opens the health endpoint only.

## Alerts

`overseer_scheduler` (or `overseer_alerts`) evaluates four conditions over the last
`OVERSEER_ALERT_WINDOW_MINUTES` minutes:

| Kind | Fires when |
| --- | --- |
| `failure_rate` | a queue's failure rate exceeds `OVERSEER_ALERT_FAILURE_RATE` (after at least 5 finished runs) |
| `queue_wait` | a run has waited more than `OVERSEER_ALERT_WAIT_SECONDS` to start, counting runs no worker has picked up yet |
| `queue_depth` | more than `OVERSEER_ALERT_QUEUE_DEPTH` runs are waiting |
| `worker_offline` | an `overseer_worker` that did not stop cleanly has not sent a heartbeat for `OVERSEER_WORKER_OFFLINE_AFTER` seconds (or three heartbeat intervals, if longer) |

An alert fires once, then not again for `OVERSEER_ALERT_COOLDOWN_MINUTES`, and resolves
itself when the condition clears. Notifications go to `OVERSEER_ALERT_EMAILS` (or `ADMINS` when that is empty),
`OVERSEER_SLACK_WEBHOOK_URL` and every callable in `OVERSEER_NOTIFIERS` (`callable(alert)`);
a failing notifier is logged, never raised. The signals `overseer.signals.alert_fired`,
`alert_resolved` and `job_failed` (`job`, `run`) let you hook anything else.

## Settings

All settings are optional. See [docs/settings.md](docs/settings.md) for the full reference.

```python
OVERSEER_DEFAULT_RETRIES = 0           # policy for tasks not declared with overseer.task
OVERSEER_DEFAULT_BACKOFF = "exponential"
OVERSEER_DEFAULT_BACKOFF_BASE = 30.0
OVERSEER_DEFAULT_BACKOFF_MAX = 3600.0
OVERSEER_DEFAULT_JITTER = True
OVERSEER_DEFAULT_TIMEOUT = None
OVERSEER_STALE_AFTER = 3600            # a running attempt with no timeout is abandoned after this
OVERSEER_WORKER_OFFLINE_AFTER = 120
OVERSEER_SILENT_WORKER_AFTER = 3600    # a plain db_worker silent this long leaves the counts
OVERSEER_RETENTION_DAYS = 14
OVERSEER_METRICS_RETENTION_DAYS = 30
OVERSEER_RECORD_ARGS = True            # False to keep task arguments out of the database
OVERSEER_MAX_TRACEBACK_CHARS = 20_000
OVERSEER_PERMISSION = "overseer.view_dashboard"   # None: any staff user
OVERSEER_REFRESH_SECONDS = 5
OVERSEER_HEALTH_TOKEN = None           # lets uptime checks call /api/health/ with a Bearer token
OVERSEER_RETRY_ALL_LIMIT = 200         # failed jobs retried per "Retry all" click
OVERSEER_ALERT_WINDOW_MINUTES = 5
OVERSEER_ALERT_FAILURE_RATE = 0.25
OVERSEER_ALERT_WAIT_SECONDS = 60
OVERSEER_ALERT_QUEUE_DEPTH = 1000
OVERSEER_ALERT_COOLDOWN_MINUTES = 15
OVERSEER_NOTIFIERS = []                # dotted paths or callables taking an Alert
OVERSEER_ALERT_EMAILS = []
OVERSEER_SLACK_WEBHOOK_URL = None
OVERSEER_SCHEDULER_INTERVAL = 1.0
OVERSEER_RESCUE_INTERVAL = 30.0
OVERSEER_MAINTENANCE_INTERVAL = 60.0
OVERSEER_SCHEDULER_TIMEZONE = None     # None: TIME_ZONE
OVERSEER_AUTODISCOVER = True           # import <app>.tasks for every installed app
OVERSEER_TASK_MODULES = []             # extra modules to import at startup
```

`python manage.py check` warns when a task declares delayed retries on a backend that cannot
defer (`supports_defer` is false): retries then run immediately instead of after the
backoff.

## How it works

See [docs/design.md](docs/design.md). In short: `task_enqueued`, `task_started` and
`task_finished` create and update `Job` (one logical unit of work), `Run` (one attempt) and
`Worker` rows. Failure handling happens in the `task_finished` receiver, inside the worker
process, so a retry is enqueued by the same process that saw the failure and is visible
immediately. Recorders never raise: a bug in Overseer cannot break your worker.

## Production notes

- Run `overseer_prune` daily (a schedule works: `@overseer.schedule("0 3 * * *")` on a task
  that calls `overseer.prune.prune()`), or the tables grow forever.
- Put the dashboard behind your usual staff authentication; it is a normal Django app under
  your `LOGIN_URL`.
- Set `OVERSEER_RECORD_ARGS = False` if task arguments may contain secrets or personal
  data; tracebacks are still stored, capped at `OVERSEER_MAX_TRACEBACK_CHARS`. Retries
  then take the arguments from the backend's own copy of the failed attempt, so a manual
  retry is no longer possible once the backend has deleted that row.
- On SQLite with several processes (workers, the scheduler, the web app) writing to one
  file, configure the database as Django recommends for concurrent writers:
  `"OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20, "init_command": "PRAGMA
  journal_mode=WAL;"}`. Without it a writer that already read in the same transaction can
  fail with "database is locked" instead of waiting.
- `USE_TZ = False` projects are supported; cron expressions are evaluated in the
  schedule's timezone and stored as naive local times like everything else.
- Overseer's tables may live on another database alias through a router (`db_for_read`,
  `db_for_write` and `allow_migrate` for the `overseer` and task-backend apps).
- `overseer_worker --reload` (on by default when `DEBUG` is on) keeps one worker id across
  the autoreloader's restarts, so the dashboard shows one worker, not one per code change.
- `unique=True` is enforced by a partial unique index. MySQL, MariaDB and Oracle have no
  partial indexes, so there Overseer can only reduce duplicates, not rule them out; system
  check `overseer.W002` says so at startup.
- A waiting run whose backend task disappeared (database restored, row deleted by hand)
  is marked lost by the scheduler's rescue pass, so health, alerts and unique keys move on;
  the job can be retried from the dashboard.
- Recording happens in the same database transaction as the enqueue: with
  `django-tasks-db`, a task enqueued inside a transaction that rolls back leaves neither a
  backend row nor an Overseer job. A backend that runs tasks elsewhere (the immediate
  backend runs them inline) has done its work by then, and only the record is rolled back.

## Development

```bash
pip install -e ".[dev]"
pytest                                         # SQLite, in memory
OVERSEER_SQLITE_FILE=/tmp/o.sqlite3 pytest tests/test_integration.py   # real subprocesses
pytest --ds=tests.settings_postgres           # everything, including the concurrency tests
ruff check src tests && ruff format --check src tests
```

The suite has four layers: unit tests for every module, integration tests that spawn real
`overseer_worker` and `overseer_scheduler` processes against a shared database,
end-to-end tests that drive the dashboard over HTTP with `requests`, and performance
guards that assert flat query counts and time budgets with thousands of rows.

## License

MIT

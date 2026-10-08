# Overseer demo

A small shop project with example tasks, for trying the dashboard locally. It is not a
production configuration (fixed secret key, `DEBUG = True`, SQLite).

```bash
cd examples/demo
python -m venv .venv && . .venv/bin/activate
pip install "django-overseer[db]"
python manage.py migrate
python manage.py seed_demo            # user admin / overseer-demo, plus ~65 jobs
```

Then, in three terminals:

```bash
python manage.py runserver                        # http://127.0.0.1:8000/overseer/
python manage.py overseer_worker --queue-name='*' # runs the jobs, with heartbeats
python manage.py overseer_scheduler               # schedules, stuck-run rescue, metrics, alerts
```

What to look at:

- **Jobs / Failed**: `charge_card` times out about one call in three and is retried with
  exponential backoff; a job that runs out of retries lands in Failed, where it can be
  retried or dismissed. `import_catalog` fails once and is not retried (a `ValueError`
  is not in its `retry_on`).
- **Unique jobs**: `send_receipt` is `unique=True`; the seed enqueues order 1001 twice and
  gets one job.
- **Schedules**: `sync_inventory` every five minutes, `warehouse-heartbeat` every minute,
  `nightly-cleanup` at 03:00. Pause one or press "Run now".
- **Workers**: stop the worker with Ctrl-C and it shows as stopped; kill it with `kill -9`
  and it turns offline, which raises an alert.
- **Metrics / Alerts**: the failure rate of `charge_card` is above the demo's 20 % alert
  threshold, so an alert opens after the first scheduler pass.

Run `python manage.py seed_demo` again for another batch.

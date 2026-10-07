#!/usr/bin/env bash
# Smoke test: install django-overseer into a brand-new Django project, run a worker and
# the scheduler, enqueue tasks and hit every dashboard page over HTTP. Exits non-zero
# on the first problem. Needs django-overseer and django-tasks-db installed already.
set -euo pipefail

WORK=$(mktemp -d)
cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        for log in worker.log scheduler.log server.log; do
            [ -f "$log" ] && { echo "--- $log ---"; cat "$log"; }
        done
    fi
    kill $(jobs -p) 2>/dev/null || true
    rm -rf "$WORK"
    exit "$status"
}
trap cleanup EXIT
cd "$WORK"

django-admin startproject demo .
python manage.py startapp shop
python - <<'PY'
import re, pathlib
p = pathlib.Path("demo/settings.py")
s = p.read_text()
assert "INSTALLED_APPS = [" in s, s
s = s.replace("INSTALLED_APPS = [", 'INSTALLED_APPS = [\n    "django_tasks_db",\n    "overseer",\n    "shop",', 1)
s += '''
TASKS = {"default": {"BACKEND": "django_tasks_db.backend.DatabaseBackend", "QUEUES": ["default", "emails"]}}
OVERSEER_WORKER_OFFLINE_AFTER = 30
# Several processes write to this one SQLite file: the options the README recommends.
DATABASES["default"]["OPTIONS"] = {
    "timeout": 20,
    "transaction_mode": "IMMEDIATE",
    "init_command": "PRAGMA journal_mode=WAL;",
}
'''
p.write_text(s)
p = pathlib.Path("demo/urls.py")
s = p.read_text()
assert "urlpatterns = [" in s, s
s = "from django.urls import include\n" + s
s = s.replace("urlpatterns = [", 'urlpatterns = [\n    path("overseer/", include("overseer.urls")),', 1)
p.write_text(s)
PY

cat > shop/tasks.py <<'PY'
import overseer
from django.tasks import task


@overseer.task(retries=2, backoff="constant", backoff_base=0, jitter=False)
def charge(order_id, fail=False):
    if fail:
        raise RuntimeError(f"card declined for order {order_id}")
    return {"order": order_id, "charged": True}


@overseer.task(unique=True, queue_name="emails")
def send_receipt(email):
    return email


@overseer.schedule(every=2)
@task
def heartbeat():
    return "ok"
PY

python manage.py check
grep -q '"overseer"' demo/settings.py && grep -q "overseer.urls" demo/urls.py
python manage.py migrate -v0
python manage.py overseer_sync_schedules
echo "from django.contrib.auth.models import User; User.objects.create_superuser('admin', 'a@x.com', 'pw')" | python manage.py shell

python manage.py overseer_worker --queue-name='*' --interval 0.2 --no-startup-delay --heartbeat 1 > worker.log 2>&1 &
python manage.py overseer_scheduler --interval 0.2 > scheduler.log 2>&1 &
python manage.py runserver 127.0.0.1:8765 --noreload > server.log 2>&1 &

python - <<'PY'
import os, time, django
os.environ["DJANGO_SETTINGS_MODULE"] = "demo.settings"
django.setup()
from shop.tasks import charge, send_receipt
charge.enqueue(1)
charge.enqueue(2, fail=True)
send_receipt.enqueue("a@example.com")
send_receipt.enqueue("a@example.com")  # collapsed by unique=True
PY

sleep 6
python - <<'PY'
import os, django
os.environ["DJANGO_SETTINGS_MODULE"] = "demo.settings"
django.setup()
from overseer.models import Job, Run, Schedule, Worker
jobs = {j.task_name: j for j in Job.objects.exclude(task_name="heartbeat")}
assert jobs["charge"].status in {"SUCCEEDED", "FAILED"}, jobs
ok = [j for j in Job.objects.filter(task_name="charge") if j.status == "SUCCEEDED"]
bad = [j for j in Job.objects.filter(task_name="charge") if j.status == "FAILED"]
assert len(ok) == 1 and len(bad) == 1, [(j.args, j.status) for j in Job.objects.filter(task_name="charge")]
assert bad[0].attempts == 3, bad[0].attempts
assert Job.objects.filter(task_name="send_receipt").count() == 1, "unique enqueue was not collapsed"
assert Job.objects.filter(task_name="heartbeat", status="SUCCEEDED").count() >= 2, "scheduler did not fire"
assert Worker.objects.filter(stopped_at__isnull=True).count() == 1
assert Schedule.objects.get(task_path="shop.tasks.heartbeat").runs_count >= 2
print("jobs ok:", Job.objects.count(), "runs:", Run.objects.count())
PY

python - <<'PY'
import re, requests, sys
s = requests.Session()
base = "http://127.0.0.1:8765"
r = s.get(base + "/admin/login/")
token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text).group(1)
r = s.post(base + "/admin/login/", data={"username": "admin", "password": "pw", "csrfmiddlewaretoken": token}, headers={"Referer": base + "/admin/login/"})
assert "sessionid" in s.cookies, "login failed"
for page in ["", "queues/", "tasks/", "jobs/", "failed/", "schedules/", "workers/", "metrics/", "alerts/"]:
    r = s.get(f"{base}/overseer/{page}")
    assert r.status_code == 200, (page, r.status_code, r.text[:200])
for api in ["overview", "queues", "tasks", "jobs", "workers", "schedules", "metrics", "alerts", "health"]:
    r = s.get(f"{base}/overseer/api/{api}/")
    assert r.status_code == 200, (api, r.status_code, r.text[:200])
health = s.get(f"{base}/overseer/api/health/").json()
assert health["ok"] and health["workers_online"] == 1, health
failed = s.get(f"{base}/overseer/failed/").text
assert "card declined" in failed or "RuntimeError" in failed
print("dashboard ok")
PY
echo "fresh project OK"

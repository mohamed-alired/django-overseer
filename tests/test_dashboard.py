import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from overseer import metrics, rescue
from overseer.models import Alert, Job, JobStatus, Run, RunStatus, Schedule, Worker
from tests import tasks

pytestmark = pytest.mark.django_db

PAGES = [
    "overview",
    "queues",
    "tasks",
    "jobs",
    "failed",
    "schedules",
    "workers",
    "metrics",
    "alerts",
]
API = ["overview", "queues", "tasks", "jobs", "workers", "schedules", "metrics", "alerts", "health"]


@pytest.fixture
def populated(worker, settings):
    """A realistic state: successes, a failure with retries exhausted, a pending job,
    a schedule, a worker and an alert."""
    settings.OVERSEER_THROTTLE = None
    tasks.plain.enqueue(1)
    tasks.plain.enqueue(2)
    failed = tasks.picky.enqueue("type")
    worker()
    pending = tasks.plain.using(queue_name="emails").enqueue(3)
    schedule = Schedule.objects.create(
        name="nightly",
        task_path="tests.tasks.plain",
        cron="0 2 * * *",
        args=[9],
        next_run_at=timezone.now() + timedelta(hours=1),
    )
    Worker.objects.create(
        worker_id="w-offline",
        last_seen_at=timezone.now() - timedelta(hours=1),
        heartbeat_seconds=10,
    )
    Worker.objects.create(worker_id="w-online", heartbeat_seconds=10)
    Alert.objects.create(
        kind="failure_rate", key="default", message="boom", value=0.5, threshold=0.25
    )
    metrics.rollup()
    return {
        "failed": Job.objects.get(runs__result_id=str(failed.id)),
        "pending": Job.objects.get(runs__result_id=str(pending.id)),
        "schedule": schedule,
    }


class TestAccess:
    @pytest.mark.parametrize("name", PAGES)
    def test_anonymous_redirected_to_login(self, name):
        resp = Client().get(reverse(f"overseer:{name}"))
        assert resp.status_code == 302 and "login" in resp["Location"]

    def test_non_staff_forbidden(self):
        user = get_user_model().objects.create_user("u", "u@example.com", "pw")
        user.user_permissions.add(Permission.objects.get(codename="view_dashboard"))
        c = Client()
        c.force_login(user)
        assert c.get(reverse("overseer:overview")).status_code == 403

    def test_staff_without_permission_forbidden(self):
        user = get_user_model().objects.create_user("s", "s@example.com", "pw", is_staff=True)
        c = Client()
        c.force_login(user)
        assert c.get(reverse("overseer:overview")).status_code == 403
        assert c.get(reverse("overseer:api-overview")).status_code == 403

    def test_permission_setting_can_be_disabled(self, settings):
        settings.OVERSEER_PERMISSION = ""
        user = get_user_model().objects.create_user("s", "s@example.com", "pw", is_staff=True)
        c = Client()
        c.force_login(user)
        assert c.get(reverse("overseer:overview")).status_code == 200

    def test_superuser_allowed(self):
        admin = get_user_model().objects.create_superuser("root", "r@example.com", "pw")
        c = Client()
        c.force_login(admin)
        assert c.get(reverse("overseer:failed")).status_code == 200

    @pytest.mark.parametrize("name", API)
    def test_api_anonymous_401(self, name):
        resp = Client().get(reverse(f"overseer:api-{name}"))
        assert resp.status_code == 401 and resp.json()["detail"]


class TestPages:
    @pytest.mark.parametrize("name", PAGES)
    def test_render_empty(self, staff_client, name):
        resp = staff_client.get(reverse(f"overseer:{name}"))
        assert resp.status_code == 200
        assert b"Overseer" in resp.content

    @pytest.mark.parametrize("name", PAGES)
    def test_render_populated(self, staff_client, populated, name):
        resp = staff_client.get(reverse(f"overseer:{name}"))
        assert resp.status_code == 200

    def test_overview_numbers(self, staff_client, populated):
        resp = staff_client.get(reverse("overseer:overview"))
        ov = resp.context["overview"]
        assert ov["processed"] == 3 and ov["succeeded"] == 2 and ov["failed"] == 1
        assert ov["waiting"] == 1 and ov["failed_jobs_open"] == 1 and ov["open_alerts"] == 1
        # The db_worker that ran the tasks sends no heartbeats; it is counted apart.
        assert ov["workers_total"] == 3 and ov["workers_online"] == 1
        assert ov["workers_without_heartbeat"] == 1
        assert b"picky" in resp.content and b"boom" in resp.content

    def test_window_param(self, staff_client, populated):
        assert (
            staff_client.get(reverse("overseer:overview") + "?minutes=1440").context["minutes"]
            == 1440
        )
        assert (
            staff_client.get(reverse("overseer:overview") + "?minutes=7").context["minutes"] == 60
        )
        assert (
            staff_client.get(reverse("overseer:overview") + "?minutes=x").context["minutes"] == 60
        )

    def test_queues_and_tasks(self, staff_client, populated):
        queues = {
            q["queue_name"]: q
            for q in staff_client.get(reverse("overseer:queues")).context["queues"]
        }
        assert queues["default"]["processed"] == 3 and queues["default"]["failed"] == 1
        assert queues["emails"]["waiting"] == 1 and queues["emails"]["backend_depth"] == 1
        tasks_ = {
            t["task_path"]: t for t in staff_client.get(reverse("overseer:tasks")).context["tasks"]
        }
        assert tasks_["tests.tasks.plain"]["succeeded"] == 2
        assert tasks_["tests.tasks.picky"]["failure_rate"] == 1.0

    def test_jobs_filters_and_pagination(self, staff_client, populated):
        url = reverse("overseer:jobs")
        assert staff_client.get(url).context["page"].paginator.count == 4
        assert staff_client.get(url + "?status=FAILED").context["page"].paginator.count == 1
        assert staff_client.get(url + "?queue=emails").context["page"].paginator.count == 1
        assert (
            staff_client.get(url + "?task=tests.tasks.picky").context["page"].paginator.count == 1
        )
        assert staff_client.get(url + "?q=picky").context["page"].paginator.count == 1
        worker_id = Run.objects.exclude(worker_id="").first().worker_id
        assert staff_client.get(url + f"?worker={worker_id}").context["page"].paginator.count == 3
        for i in range(60):
            tasks.plain.enqueue(i)
        page2 = staff_client.get(url + "?page=2").context["page"]
        assert page2.number == 2 and page2.paginator.count == 64

    def test_job_detail(self, staff_client, populated):
        job = populated["failed"]
        resp = staff_client.get(reverse("overseer:job", args=[job.pk]))
        assert resp.status_code == 200
        assert b"builtins.TypeError" in resp.content and b"not retryable" in resp.content
        assert b"Retry now" in resp.content
        assert (
            staff_client.get(
                reverse("overseer:job", args=["00000000-0000-0000-0000-000000000000"])
            ).status_code
            == 404
        )

    def test_metrics_page_with_queue(self, staff_client, populated):
        resp = staff_client.get(reverse("overseer:metrics") + "?queue=default&minutes=15")
        assert resp.status_code == 200 and resp.context["totals"]["processed"] == 3

    def test_manage_buttons_hidden_without_permission(self, populated):
        user = get_user_model().objects.create_user("v", "v@example.com", "pw", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_dashboard"))
        c = Client()
        c.force_login(user)
        resp = c.get(reverse("overseer:failed"))
        assert resp.status_code == 200 and b"Retry all" not in resp.content


class TestActions:
    def test_retry_failed_job(self, staff_client, populated):
        job = populated["failed"]
        resp = staff_client.post(
            reverse("overseer:job-retry", args=[job.pk]), {"next": "/overseer/"}
        )
        assert resp.status_code == 302 and resp["Location"] == "/overseer/"
        job.refresh_from_db()
        assert job.status == JobStatus.PENDING and job.runs.count() == 2

    def test_retry_active_job_is_refused(self, staff_client, populated):
        job = populated["pending"]
        resp = staff_client.post(reverse("overseer:job-retry", args=[job.pk]), follow=True)
        assert "already has an active run" in resp.content.decode()

    def test_cancel_pending_job(self, staff_client, populated):
        from django_tasks_db.models import DBTaskResult

        job = populated["pending"]
        run = job.runs.get()
        resp = staff_client.post(reverse("overseer:job-cancel", args=[job.pk]), follow=True)
        assert b"Job cancelled." in resp.content
        job.refresh_from_db()
        assert job.status == JobStatus.CANCELLED
        assert Run.objects.get(pk=run.pk).status == RunStatus.CANCELLED
        assert not DBTaskResult.objects.filter(id=run.result_id).exists()

    def test_cancel_running_job_is_refused(self, staff_client, populated):
        job = populated["pending"]
        job.runs.update(status=RunStatus.RUNNING, started_at=timezone.now())
        resp = staff_client.post(reverse("overseer:job-cancel", args=[job.pk]), follow=True)
        assert b"waiting to start" in resp.content

    def test_dismiss_and_bulk_actions(self, staff_client, populated):
        job = populated["failed"]
        staff_client.post(reverse("overseer:job-dismiss", args=[job.pk]))
        assert Job.objects.get(pk=job.pk).dismissed is True
        assert staff_client.get(reverse("overseer:failed")).context["total"] == 0
        Job.objects.filter(pk=job.pk).update(dismissed=False)
        resp = staff_client.post(reverse("overseer:failed-retry-all"), follow=True)
        assert b"Retried 1 job(s)." in resp.content
        tasks.picky.enqueue("type")
        from tests.conftest import work

        work()
        resp = staff_client.post(reverse("overseer:failed-dismiss-all"), follow=True)
        assert b"Dismissed 2 job(s)." in resp.content  # the retried job failed again + the new one

    def test_schedule_toggle_run_sync(self, staff_client, populated):
        s = populated["schedule"]
        staff_client.post(reverse("overseer:schedule-toggle", args=[s.pk]))
        assert Schedule.objects.get(pk=s.pk).enabled is False
        Schedule.objects.filter(pk=s.pk).update(next_run_at=timezone.now() - timedelta(days=1))
        staff_client.post(reverse("overseer:schedule-toggle", args=[s.pk]))
        s.refresh_from_db()
        assert s.enabled is True and s.next_run_at > timezone.now()
        resp = staff_client.post(reverse("overseer:schedule-run", args=[s.pk]), follow=True)
        assert b"Enqueued plain." in resp.content
        assert Job.objects.filter(schedule=s).count() == 1
        resp = staff_client.post(reverse("overseer:schedules-sync"), follow=True)
        assert b"Schedules synced" in resp.content
        assert Schedule.objects.filter(name="heartbeat").exists()

    def test_actions_require_manage_permission(self, populated):
        user = get_user_model().objects.create_user("v", "v@example.com", "pw", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_dashboard"))
        c = Client()
        c.force_login(user)
        job = populated["failed"]
        assert c.post(reverse("overseer:job-retry", args=[job.pk])).status_code == 403
        assert c.post(reverse("overseer:failed-retry-all")).status_code == 403
        assert c.post(reverse("overseer:schedules-sync")).status_code == 403

    def test_actions_reject_get(self, staff_client, populated):
        job = populated["failed"]
        assert staff_client.get(reverse("overseer:job-retry", args=[job.pk])).status_code == 405


class TestApi:
    @pytest.mark.parametrize("name", API)
    def test_json_shape(self, staff_client, populated, name):
        resp = staff_client.get(reverse(f"overseer:api-{name}"))
        assert resp.status_code in (200, 503)
        assert resp["Content-Type"].startswith("application/json")
        json.loads(resp.content)

    def test_overview_and_jobs(self, staff_client, populated):
        data = staff_client.get(reverse("overseer:api-overview")).json()
        assert data["processed"] == 3 and data["failed"] == 1 and "generated_at" in data
        data = staff_client.get(reverse("overseer:api-jobs") + "?status=FAILED").json()
        assert data["count"] == 1 and data["results"][0]["task_name"] == "picky"
        job = staff_client.get(reverse("overseer:api-job", args=[data["results"][0]["id"]])).json()
        assert job["runs"][0]["exception_class"] == "builtins.TypeError"
        assert (
            staff_client.get(
                reverse("overseer:api-job", args=["00000000-0000-0000-0000-000000000000"])
            ).status_code
            == 404
        )

    def test_metrics_and_health(self, staff_client, populated):
        data = staff_client.get(
            reverse("overseer:api-metrics") + "?minutes=15&queue=default"
        ).json()
        assert data["minutes"] == 15 and len(data["points"]) == 15
        health = staff_client.get(reverse("overseer:api-health"))
        assert health.status_code == 200 and health.json()["ok"] is True
        # The pending task has now waited far longer than OVERSEER_ALERT_WAIT_SECONDS.
        Run.objects.filter(status="READY").update(enqueued_at=timezone.now() - timedelta(hours=2))
        health = staff_client.get(reverse("overseer:api-health"))
        assert health.status_code == 503 and health.json()["waiting"] == 1
        assert health.json()["oldest_wait_seconds"] > 7000

    def test_workers_and_schedules(self, staff_client, populated):
        workers = staff_client.get(reverse("overseer:api-workers")).json()["workers"]
        assert {w["state"] for w in workers} == {"online", "offline", "no heartbeat"}
        schedules = staff_client.get(reverse("overseer:api-schedules")).json()["schedules"]
        assert schedules[0]["name"] == "nightly" and schedules[0]["cron"] == "0 2 * * *"


class TestTemplateFilters:
    def test_filters(self):
        from overseer.templatetags.overseer_extras import ago, bar_width, ms, pct, short_id

        assert ms(None) == "–" and ms(850) == "850 ms" and ms(2300) == "2.3 s"
        assert ms(245_000) == "4m 05s" and ms(4_320_000) == "1h 12m"
        assert pct(0.1234) == "12.3%" and pct("x") == "–"
        now = timezone.now()
        assert ago(now - timedelta(seconds=12)).endswith("s ago")
        assert ago(now - timedelta(minutes=3)) == "3m ago"
        assert ago(now - timedelta(hours=2)) == "2h ago"
        assert ago(now - timedelta(days=5)) == "5d ago"
        assert ago(now + timedelta(minutes=3)).startswith("in ")
        assert ago(None) == "–"
        assert short_id("abcdefghijk") == "abcdefgh"
        assert bar_width(5, 10) == 50 and bar_width(1, 0) == 0 and bar_width("x", 1) == 0


class TestAbandonedInDashboard:
    def test_abandoned_run_shows_on_failed_page(self, staff_client, settings, populated):
        settings.OVERSEER_STALE_AFTER = 1
        job = populated["pending"]
        job.runs.update(status=RunStatus.RUNNING, started_at=timezone.now() - timedelta(minutes=5))
        rescue.rescue()
        resp = staff_client.get(reverse("overseer:failed"))
        assert b"RunAbandoned" in resp.content

"""End-to-end: a real HTTP server (``live_server``), a real HTTP client (``requests``),
real sessions, CSRF and redirects, driven like a browser would."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import requests
from django.urls import reverse
from django.utils import timezone

from overseer.models import Job, JobStatus, Run, Schedule
from overseer.scheduling.sync import sync_schedules
from tests import tasks
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

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
APIS = ["overview", "queues", "tasks", "jobs", "workers", "schedules", "metrics", "alerts"]


class Browser:
    """A ``requests`` session that logs in through the real admin login form."""

    def __init__(self, base_url):
        self.base = base_url
        self.http = requests.Session()

    def url(self, name, **kwargs):
        return self.base + reverse(f"overseer:{name}", kwargs=kwargs or None)

    def login(self, username, password):
        login_url = self.base + "/admin/login/"
        page = self.http.get(login_url)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
        resp = self.http.post(
            login_url,
            data={"username": username, "password": password, "csrfmiddlewaretoken": token},
            headers={"Referer": login_url},
            allow_redirects=False,
        )
        assert resp.status_code == 302, resp.text[:500]
        assert "sessionid" in self.http.cookies

    def get(self, name, **params):
        kwargs = {k: params.pop(k) for k in ("pk",) if k in params}
        return self.http.get(self.url(name, **kwargs), params=params)

    def post(self, name, data=None, **kwargs):
        url = self.url(name, **kwargs)
        token = self.http.cookies.get("csrftoken")
        if token is None:
            self.get("overview")
            token = self.http.cookies.get("csrftoken")
        return self.http.post(
            url,
            data={**(data or {}), "csrfmiddlewaretoken": token},
            headers={"Referer": url},
            allow_redirects=False,
        )


@pytest.fixture
def browser(live_server, staff_user):
    b = Browser(live_server.url)
    b.login(staff_user.username, PASSWORD)
    return b


@pytest.fixture
def anonymous(live_server):
    return Browser(live_server.url)


class TestEndToEnd:
    def test_every_page_and_endpoint_renders_over_http(self, browser, worker):
        tasks.plain.enqueue(1)
        tasks.flaky.enqueue(5)
        tasks.unique_by_args.enqueue("e2e@example.com")
        worker()
        sync_schedules()
        for name in PAGES:
            resp = browser.get(name)
            assert resp.status_code == 200, (name, resp.text[:300])
            assert "<title>" in resp.text and "Overseer" in resp.text
        for name in APIS:
            resp = browser.get(f"api-{name}")
            assert resp.status_code == 200, name
            assert resp.headers["Content-Type"].startswith("application/json")
            resp.json()
        job = Job.objects.get(task_path="tests.tasks.plain")
        page = browser.get("job", pk=job.pk)
        assert page.status_code == 200 and "tests.tasks.plain" in page.text
        api = browser.get("api-job", pk=job.pk).json()
        assert api["status"] == JobStatus.SUCCEEDED and api["runs"][0]["return_value"] == 2

    def test_static_assets_are_served(self, browser):
        page = browser.get("overview")
        css = re.search(r'href="([^"]+overseer\.css)"', page.text).group(1)
        js = re.search(r'src="([^"]+overseer\.js)"', page.text).group(1)
        assert css.startswith("/static/") and js.startswith("/static/")

    def test_failed_job_retry_flow(self, browser, worker):
        tasks.always_fails.enqueue()
        worker()
        job = Job.objects.get()
        assert job.status == JobStatus.FAILED
        failed_page = browser.get("failed")
        assert "always_fails" in failed_page.text
        assert "RuntimeError" in failed_page.text

        resp = browser.post("job-retry", {"next": browser.url("failed")}, pk=job.pk)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/failed/")
        job.refresh_from_db()
        assert job.status == JobStatus.PENDING
        after = browser.get("failed")
        assert "Retry enqueued as attempt 2" in after.text  # flashed message survived redirect
        worker()
        job.refresh_from_db()
        assert job.status == JobStatus.FAILED and job.attempts == 2
        detail = browser.get("job", pk=job.pk)
        assert detail.text.count("always") >= 2  # both tracebacks shown

        resp = browser.post("failed-dismiss-all")
        assert resp.status_code == 302
        job.refresh_from_db()
        assert job.dismissed is True
        assert browser.get("api-overview").json()["failed_jobs_open"] == 0

    def test_cancel_pending_job(self, browser):
        tasks.plain.enqueue(9)
        job = Job.objects.get()
        resp = browser.post("job-cancel", pk=job.pk)
        assert resp.status_code == 302
        job.refresh_from_db()
        assert job.status == JobStatus.CANCELLED
        assert browser.get("api-job", pk=job.pk).json()["status"] == JobStatus.CANCELLED

    def test_schedule_lifecycle(self, browser, worker):
        resp = browser.post("schedules-sync")
        assert resp.status_code == 302
        schedule = Schedule.objects.get(name="heartbeat")
        assert schedule.enabled
        resp = browser.post("schedule-toggle", pk=schedule.pk)
        assert resp.status_code == 302
        schedule.refresh_from_db()
        assert schedule.enabled is False
        page = browser.get("schedules")
        assert "heartbeat" in page.text
        resp = browser.post("schedule-run", pk=schedule.pk)
        assert resp.status_code == 302
        worker()
        schedule.refresh_from_db()
        assert schedule.runs_count == 1
        assert Job.objects.get(schedule=schedule).status == JobStatus.SUCCEEDED
        rows = browser.get("api-schedules").json()["schedules"]
        assert any(r["name"] == "heartbeat" and r["runs_count"] == 1 for r in rows)

    def test_health_reflects_waiting_tasks(self, browser, worker, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = 60
        tasks.plain.enqueue(1)
        resp = browser.get("api-health")
        assert resp.status_code == 200 and resp.json()["waiting"] == 1  # just enqueued
        Run.objects.update(enqueued_at=timezone.now() - timedelta(minutes=5))
        resp = browser.get("api-health")
        assert resp.status_code == 503
        assert resp.json() == {**resp.json(), "ok": False, "waiting": 1}
        worker()
        resp = browser.get("api-health")
        assert resp.status_code == 200 and resp.json()["waiting"] == 0

    def test_health_accepts_the_bearer_token(self, anonymous, settings):
        url = anonymous.url("api-health")
        assert anonymous.http.get(url).status_code == 401
        settings.OVERSEER_HEALTH_TOKEN = "s3cret-token"
        bad = anonymous.http.get(url, headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        good = anonymous.http.get(url, headers={"Authorization": "Bearer s3cret-token"})
        assert good.status_code == 200 and good.json()["ok"] is True
        # The token opens the health endpoint only.
        other = anonymous.http.get(
            anonymous.url("api-overview"), headers={"Authorization": "Bearer s3cret-token"}
        )
        assert other.status_code == 401

    def test_anonymous_is_redirected_and_api_denied(self, anonymous):
        resp = anonymous.http.get(anonymous.url("overview"), allow_redirects=False)
        assert resp.status_code == 302 and "login/?next=/overseer/" in resp.headers["Location"]
        assert anonymous.http.get(anonymous.url("api-overview")).status_code == 401
        assert anonymous.http.get(anonymous.url("api-health")).status_code == 401

    def test_csrf_is_enforced_on_actions(self, browser, worker):
        tasks.always_fails.enqueue()
        worker()
        job = Job.objects.get()
        naked = requests.Session()
        naked.cookies.set("sessionid", browser.http.cookies["sessionid"])
        resp = naked.post(browser.url("job-retry", pk=job.pk), allow_redirects=False)
        assert resp.status_code == 403
        job.refresh_from_db()
        assert job.status == JobStatus.FAILED

    def test_window_and_filters_round_trip(self, browser, worker):
        tasks.plain.enqueue(1)
        tasks.unique_by_args.enqueue("f@example.com")
        worker()
        plain = Job.objects.get(task_path="tests.tasks.plain")
        mail = Job.objects.get(task_path="tests.tasks.unique_by_args")
        page = browser.get("jobs", queue="emails")
        assert reverse("overseer:job", kwargs={"pk": mail.pk}) in page.text
        assert reverse("overseer:job", kwargs={"pk": plain.pk}) not in page.text
        page = browser.get("jobs", q="plain")
        assert reverse("overseer:job", kwargs={"pk": plain.pk}) in page.text
        assert reverse("overseer:job", kwargs={"pk": mail.pk}) not in page.text
        api = browser.get("api-jobs", status=JobStatus.SUCCEEDED, per_page=1).json()
        assert api["count"] == 2 and len(api["results"]) == 1
        page = browser.get("overview", minutes=1440)
        assert 'value="1440"' in page.text or "1440" in page.text

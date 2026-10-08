"""Regression tests for the dashboard, API and settings bugs fixed in 0.1.2."""

import re

import pytest
from django.contrib.messages import get_messages
from django.core.checks import run_checks
from django.urls import reverse
from django.utils import timezone

from overseer import alerts, stats
from overseer.models import Job, JobStatus, Run, RunStatus
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def messages_of(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class TestFailureRateAlert:
    def test_cancelled_runs_do_not_dilute_the_alert(self, settings):
        settings.OVERSEER_ALERT_FAILURE_RATE = 0.25
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        now = timezone.now()
        statuses = [RunStatus.FAILED] * 4 + [RunStatus.SUCCESSFUL] + [RunStatus.CANCELLED] * 10
        for i, status in enumerate(statuses):
            Run.objects.create(
                job=job, result_id=f"r{i}", backend="default", status=status,
                enqueued_at=now, finished_at=now,
            )  # fmt: skip
        assert stats.overview(5)["failure_rate"] == 0.8
        (cond,) = [c for c in alerts.detect() if c.kind == "failure_rate"]
        assert cond.value == 0.8


class TestRetryAll:
    def test_unretryable_jobs_do_not_use_up_the_budget(self, staff_client, worker, settings):
        settings.OVERSEER_RETRY_ALL_LIMIT = 2
        for _ in range(3):
            tasks.always_fails.enqueue()
        worker()
        old = list(Job.objects.order_by("created_at")[:2])
        Job.objects.filter(pk__in=[j.pk for j in old]).update(task_path="gone.module.task")
        resp = staff_client.post(reverse("overseer:failed-retry-all"))
        (message,) = messages_of(resp)
        assert "Retried 1 job(s)." in message and "2 could not be retried" in message
        assert "more remain" not in message
        assert Job.objects.filter(status=JobStatus.PENDING).count() == 1


class TestCancel:
    def test_unknown_backend_alias_gives_a_message(self, staff_client):
        tasks.plain.enqueue(1)
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(backend="vanished")
        resp = staff_client.post(reverse("overseer:job-cancel", args=[job.pk]))
        assert resp.status_code == 302
        assert any("Could not cancel" in m for m in messages_of(resp))


class TestCraftedInput:
    @pytest.mark.parametrize("name", ["q", "status", "queue", "task", "worker"])
    def test_nul_bytes_in_filters_are_dropped(self, staff_client, name):
        tasks.plain.enqueue(1)
        for url in (reverse("overseer:jobs"), reverse("overseer:api-jobs")):
            resp = staff_client.get(url, {name: "pla\x00in"})
            assert resp.status_code == 200
        resp = staff_client.get(reverse("overseer:metrics"), {"queue": "de\x00fault"})
        assert resp.status_code == 200

    def test_wait_seconds_none_does_not_break_health(self, staff_client, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = None
        assert staff_client.get(reverse("overseer:api-health")).status_code == 200


class TestHealthToken:
    TOKEN = "a-token-that-is-long-enough"

    def test_scheme_is_case_insensitive_and_whitespace_tolerant(self, client, settings):
        settings.OVERSEER_HEALTH_TOKEN = self.TOKEN
        url = reverse("overseer:api-health")
        for header in (f"bearer {self.TOKEN}", f"BEARER  {self.TOKEN} ", f"Bearer {self.TOKEN}"):
            assert client.get(url, headers={"Authorization": header}).status_code == 200

    def test_401_advertises_bearer_and_health_is_not_cached(self, client, settings):
        settings.OVERSEER_HEALTH_TOKEN = self.TOKEN
        url = reverse("overseer:api-health")
        denied = client.get(url, headers={"Authorization": "Bearer nope"})
        assert denied.status_code == 401 and denied["WWW-Authenticate"].startswith("Bearer")
        ok = client.get(url, headers={"Authorization": f"Bearer {self.TOKEN}"})
        assert ok["Cache-Control"] == "no-store"

    def test_non_string_token_never_matches(self, client, settings):
        settings.OVERSEER_HEALTH_TOKEN = 12345
        resp = client.get(reverse("overseer:api-health"), headers={"Authorization": "Bearer 12345"})
        assert resp.status_code == 401


class TestSettingsChecks:
    def test_bad_types_are_reported_not_crashed(self, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = None
        settings.OVERSEER_RETRY_ALL_LIMIT = "many"
        settings.OVERSEER_HEALTH_TOKEN = "short"
        ids = sorted({c.id for c in run_checks() if c.id.startswith("overseer.E")})
        assert ids == ["overseer.E004", "overseer.E005"]

    def test_partial_index_warning(self, settings):
        from django.db import connection

        from overseer import checks

        original = connection.features.supports_partial_indexes
        try:
            connection.features.supports_partial_indexes = False
            (warning,) = checks.check_unique_enforcement(None, databases=["default"])
            assert warning.id == "overseer.W002"
        finally:
            connection.features.supports_partial_indexes = original


class TestTemplates:
    def test_job_heading_status_refreshes(self, staff_client, worker):
        tasks.always_fails.enqueue()
        worker()
        job = Job.objects.get()
        html = staff_client.get(reverse("overseer:job", args=[job.pk])).content.decode()
        h1 = re.search(r"<h1>(.*?)</h1>", html, re.S).group(1)
        assert 'data-refresh-id="job-status"' in h1 and "badge FAILED" in h1

    def test_metrics_window_labels(self, staff_client):
        html = staff_client.get(reverse("overseer:metrics")).content.decode()
        assert "Last hour" in html and "Last 24 hours" in html and "1440 min" not in html


class TestCronSpeed:
    def test_fast_path_away_from_transitions(self):
        import time
        from datetime import UTC, datetime

        from overseer.scheduling.cron import parse

        cron = parse("* * * * *")
        start = time.perf_counter()
        after = datetime(2026, 6, 1, tzinfo=UTC)
        for _ in range(2000):
            after = cron.next_after(after, tz="America/New_York")
        elapsed = time.perf_counter() - start
        assert after == datetime(2026, 6, 2, 9, 20, tzinfo=UTC)
        assert elapsed < 1.0, f"2000 calls took {elapsed:.2f}s"

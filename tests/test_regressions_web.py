"""Regression tests for the dashboard and JSON API bugs fixed in 0.1.1."""

import re
import uuid

import pytest
from django.contrib.messages import get_messages
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from overseer import stats
from overseer.models import Job, JobStatus, Run, RunStatus, Schedule
from tests import tasks

pytestmark = pytest.mark.django_db(transaction=True)


def messages_of(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def failed_job(worker, task=tasks.always_fails, *args):
    task.enqueue(*args)
    worker()
    return Job.objects.get(task_path=task.module_path)


class TestRedirects:
    @pytest.mark.parametrize(
        "target",
        ["https://evil.example/phish", "//evil.example/x", "http://testserver.evil/x", "ftp://x"],
    )
    def test_off_site_next_is_ignored(self, staff_client, worker, target):
        job = failed_job(worker)
        resp = staff_client.post(reverse("overseer:job-dismiss", args=[job.pk]), {"next": target})
        assert resp.status_code == 302 and resp["Location"] == reverse("overseer:failed")

    def test_on_site_next_is_followed(self, staff_client, worker):
        job = failed_job(worker)
        target = reverse("overseer:job", args=[job.pk])
        resp = staff_client.post(reverse("overseer:job-dismiss", args=[job.pk]), {"next": target})
        assert resp["Location"] == target


class TestRetryActions:
    def test_succeeded_job_is_not_retried(self, staff_client, worker):
        tasks.plain.enqueue(1)
        worker()
        job = Job.objects.get()
        resp = staff_client.post(reverse("overseer:job-retry", args=[job.pk]))
        assert resp.status_code == 302
        assert any("Only failed or cancelled" in m for m in messages_of(resp))
        assert job.runs.count() == 1

    def test_renamed_task_gives_a_message_not_a_500(self, staff_client, worker):
        job = failed_job(worker)
        Job.objects.filter(pk=job.pk).update(task_path="tests.tasks.renamed_away")
        resp = staff_client.post(reverse("overseer:job-retry", args=[job.pk]))
        assert resp.status_code == 302
        assert any("no longer exists" in m for m in messages_of(resp))

    def test_retry_all_skips_bad_jobs_and_retries_the_rest(self, staff_client, worker):
        for _ in range(3):
            tasks.always_fails.enqueue()
        worker()
        bad = Job.objects.order_by("created_at").first()
        Job.objects.filter(pk=bad.pk).update(task_path="tests.tasks.renamed_away")
        resp = staff_client.post(reverse("overseer:failed-retry-all"))
        assert resp.status_code == 302
        (message,) = messages_of(resp)
        assert "Retried 2 job(s)." in message and "1 could not be retried" in message
        assert Job.objects.filter(status=JobStatus.PENDING).count() == 2

    def test_retry_all_is_capped_per_click(self, staff_client, worker, settings):
        settings.OVERSEER_RETRY_ALL_LIMIT = 2
        for _ in range(3):
            tasks.always_fails.enqueue()
        worker()
        resp = staff_client.post(reverse("overseer:failed-retry-all"))
        (message,) = messages_of(resp)
        assert "Retried 2 job(s)." in message and "1 more remain" in message

    def test_dismissing_a_job_that_did_not_fail_says_so(self, staff_client, worker):
        tasks.plain.enqueue(1)
        worker()
        job = Job.objects.get()
        resp = staff_client.post(reverse("overseer:job-dismiss", args=[job.pk]))
        assert messages_of(resp) == ["Only a failed job can be dismissed."]


class TestJobsPage:
    def test_pagination_keeps_the_filters(self, staff_client):
        for i in range(55):
            tasks.plain.enqueue(i)
        html = staff_client.get(reverse("overseer:jobs") + "?status=PENDING").content.decode()
        link = re.search(r'href="([^"]*page=2[^"]*)"', html).group(1)
        assert link == "?status=PENDING&amp;page=2"

    def test_counting_jobs_does_not_join_runs(self, staff_client):
        for i in range(3):
            tasks.plain.enqueue(i)
        with CaptureQueriesContext(connection) as ctx:
            staff_client.get(reverse("overseer:jobs") + "?worker=w1&q=plain")
        counts = [q["sql"] for q in ctx.captured_queries if "COUNT(" in q["sql"].upper()]
        job_counts = [sql for sql in counts if "overseer_job" in sql and "GROUP BY" in sql.upper()]
        assert job_counts == []

    def test_worker_filter_keeps_the_jobs_own_last_run(self, worker):
        tasks.always_fails.enqueue()
        worker(worker_id="w-first")
        job = Job.objects.get()
        Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
        from overseer.retry import retry_job

        retry_job(job)
        latest = job.runs.order_by("-attempt").first()
        row = stats.job_queryset(worker_id="w-first").get()
        assert row.run_count == 2 and row.last_run_at == latest.enqueued_at


class TestWindowSelector:
    def test_labels_and_carried_filters(self, staff_client):
        html = staff_client.get(reverse("overseer:queues") + "?minutes=360&x=1").content.decode()
        labels = re.findall(r"<option value=\"\d+\" ?(?:selected)?>([^<]+)</option>", html)
        assert labels == ["Last 15 minutes", "Last hour", "Last 6 hours", "Last 24 hours"]
        assert '<input type="hidden" name="x" value="1">' in html
        assert 'name="minutes"' in html and 'type="hidden" name="minutes"' not in html


class TestStats:
    def test_cancelled_runs_are_not_processed(self):
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        now = timezone.now()
        for i, status in enumerate(
            [RunStatus.SUCCESSFUL, RunStatus.FAILED] + [RunStatus.CANCELLED] * 3
        ):
            Run.objects.create(
                job=job, result_id=f"r{i}", backend="default", status=status,
                enqueued_at=now, finished_at=now,
            )  # fmt: skip
        ov = stats.overview(60)
        assert ov["processed"] == 2 and ov["failure_rate"] == 0.5
        (row,) = stats.tasks(60)
        assert row["processed"] == 2 and row["failure_rate"] == 0.5
        (queue,) = [q for q in stats.queues(60) if q["queue_name"] == "q"]
        assert queue["processed"] == 2

    def test_queue_depths_cost_one_query_per_backend(self, django_assert_max_num_queries):
        for i in range(20):
            tasks.plain.using(queue_name="default").enqueue(i)
        Schedule.objects.bulk_create(
            [
                Schedule(name=f"s{i}", task_path="tests.tasks.plain", queue_name=f"q{i}")
                for i in range(20)
            ]
        )
        with django_assert_max_num_queries(8):
            rows = stats.queues(60)
        assert len(rows) == 21
        assert next(r for r in rows if r["queue_name"] == "default")["backend_depth"] == 20


class TestJsonApi:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("abc", 50), ("1.5", 50), ("0", 1), ("-3", 1), ("1000", 200), ("7", 7)],
    )
    def test_per_page_never_errors(self, staff_client, value, expected):
        resp = staff_client.get(reverse("overseer:api-jobs") + f"?per_page={value}")
        assert resp.status_code == 200 and resp.json()["per_page"] == expected

    @pytest.mark.parametrize("pk", [str(uuid.uuid4()), "not-a-uuid"])
    def test_unknown_job_is_a_json_404(self, staff_client, pk):
        resp = staff_client.get(reverse("overseer:api-job", args=[pk]))
        assert resp.status_code == 404
        assert resp["Content-Type"] == "application/json" and resp.json()["detail"]

    def test_upper_case_id_is_found(self, staff_client):
        tasks.plain.enqueue(1)
        job = Job.objects.get()
        resp = staff_client.get(reverse("overseer:api-job", args=[str(job.pk).upper()]))
        assert resp.status_code == 200 and resp.json()["id"] == str(job.pk)


class TestSchedulesPage:
    def test_broken_schedule_shows_why_and_cannot_be_resumed(self, staff_client):
        s = Schedule.objects.create(
            name="broken", task_path="tests.tasks.plain", cron="0 0 30 2 mon",
            enabled=False, last_error="Disabled: unknown timezone 'Mars/Base'",
            timezone="Mars/Base",
        )  # fmt: skip
        html = staff_client.get(reverse("overseer:schedules")).content.decode()
        assert "unknown timezone" in html
        resp = staff_client.post(reverse("overseer:schedule-toggle", args=[s.pk]))
        assert any("cannot run" in m for m in messages_of(resp))
        s.refresh_from_db()
        assert s.enabled is False

    def test_resuming_clears_the_error(self, staff_client):
        s = Schedule.objects.create(
            name="ok", task_path="tests.tasks.plain", interval_seconds=60, enabled=False,
            last_error="old", missing_from_code=True,
        )  # fmt: skip
        staff_client.post(reverse("overseer:schedule-toggle", args=[s.pk]))
        s.refresh_from_db()
        assert s.enabled and s.last_error == "" and not s.missing_from_code and s.next_run_at


class TestJobDetail:
    def test_status_and_actions_refresh_with_the_attempts(self, staff_client, worker):
        job = failed_job(worker)
        html = staff_client.get(reverse("overseer:job", args=[job.pk])).content.decode()
        panel = html.split('data-refresh-id="job"', 1)[1]
        assert "badge FAILED" in panel and "job-retry" not in html.split('data-refresh-id="job"')[0]
        assert "Attempts" in panel and 'data-refresh-id="runs"' not in html

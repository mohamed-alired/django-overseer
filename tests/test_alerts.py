from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from overseer import alerts, signals
from overseer.models import Alert, Job, JobStatus, Run, RunStatus, Worker

pytestmark = pytest.mark.django_db


def finished_run(queue, status, minutes_ago=1, wait_ms=100):
    now = timezone.now()
    job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name=queue)
    return Run.objects.create(
        job=job,
        result_id=f"r{job.pk}",
        backend="default",
        status=status,
        enqueued_at=now - timedelta(minutes=minutes_ago, seconds=1),
        started_at=now - timedelta(minutes=minutes_ago),
        finished_at=now - timedelta(minutes=minutes_ago),
        wait_ms=wait_ms,
        duration_ms=10,
    )


class TestDetect:
    def test_failure_rate_needs_sample_and_threshold(self, settings):
        settings.OVERSEER_ALERT_FAILURE_RATE = 0.5
        for _ in range(4):
            finished_run("default", RunStatus.FAILED)
        assert alerts.detect() == []  # four finished runs: below MIN_SAMPLE
        finished_run("default", RunStatus.SUCCESSFUL)
        (cond,) = alerts.detect()  # 4/5 = 80% > 50%
        assert cond.kind == "failure_rate" and cond.value == pytest.approx(0.8)
        settings.OVERSEER_ALERT_FAILURE_RATE = 0.9
        assert alerts.detect() == []

    def test_failure_rate_fires(self, settings):
        settings.OVERSEER_ALERT_FAILURE_RATE = 0.25
        for _ in range(4):
            finished_run("emails", RunStatus.FAILED)
        for _ in range(2):
            finished_run("emails", RunStatus.SUCCESSFUL)
        finished_run("emails", RunStatus.ABANDONED)
        (cond,) = alerts.detect()
        assert cond.kind == "failure_rate" and cond.key == "emails"
        assert cond.value == pytest.approx(5 / 7) and cond.threshold == 0.25
        assert "71%" in cond.message

    def test_old_runs_outside_window_ignored(self, settings):
        settings.OVERSEER_ALERT_WINDOW_MINUTES = 5
        for _ in range(6):
            finished_run("default", RunStatus.FAILED, minutes_ago=10)
        assert alerts.detect() == []

    def test_queue_wait(self, settings):
        settings.OVERSEER_ALERT_WAIT_SECONDS = 30
        finished_run("reports", RunStatus.SUCCESSFUL, wait_ms=45_000)
        (cond,) = alerts.detect()
        assert cond.kind == "queue_wait" and cond.key == "reports" and cond.value == 45

    def test_queue_depth(self, settings):
        settings.OVERSEER_ALERT_QUEUE_DEPTH = 2
        now = timezone.now()
        for i in range(3):
            job = Job.objects.create(
                task_path="t", task_name="t", backend="default", queue_name="q"
            )
            Run.objects.create(
                job=job, result_id=f"d{i}", backend="default", status=RunStatus.READY
            )
        job = Job.objects.create(task_path="t", task_name="t", backend="default", queue_name="q")
        Run.objects.create(  # deferred into the future: not counted
            job=job,
            result_id="later",
            backend="default",
            status=RunStatus.READY,
            run_after=now + timedelta(hours=1),
        )
        (cond,) = alerts.detect()
        assert cond.kind == "queue_depth" and cond.value == 3

    def test_worker_offline(self, settings):
        settings.OVERSEER_WORKER_OFFLINE_AFTER = 60
        Worker.objects.create(worker_id="w-old", last_seen_at=timezone.now() - timedelta(minutes=5))
        Worker.objects.create(
            worker_id="w-stopped",
            stopped_at=timezone.now(),
            last_seen_at=timezone.now() - timedelta(minutes=5),
        )
        Worker.objects.create(worker_id="w-fresh")
        (cond,) = alerts.detect()
        assert cond.kind == "worker_offline" and cond.key == "w-old"


class TestEvaluate:
    @pytest.fixture
    def failing_queue(self, settings):
        settings.OVERSEER_ALERT_FAILURE_RATE = 0.1
        settings.ADMINS = ["ops@example.com"]
        for _ in range(6):
            finished_run("default", RunStatus.FAILED)

    def test_fires_once_then_cooldown_then_resolves(self, failing_queue, settings):
        settings.OVERSEER_ALERT_COOLDOWN_MINUTES = 15
        received = []
        signals.alert_fired.connect(lambda sender, alert, **kw: received.append(alert), weak=False)
        fired = alerts.evaluate()
        assert len(fired) == 1 and fired[0].kind == "failure_rate" and received == fired
        assert alerts.evaluate() == []  # still open -> no duplicate
        assert Alert.objects.count() == 1
        assert len(mail.outbox) == 1 and "failure_rate" in mail.outbox[0].subject
        assert mail.outbox[0].to == ["ops@example.com"]  # ADMINS fallback

        Run.objects.all().delete()  # condition clears
        resolved = []
        signals.alert_resolved.connect(
            lambda sender, alert, **kw: resolved.append(alert), weak=False
        )
        assert alerts.evaluate() == []
        assert Alert.objects.get().resolved_at is not None and len(resolved) == 1

        for _ in range(6):  # recurs within the cooldown -> suppressed
            finished_run("default", RunStatus.FAILED)
        assert alerts.evaluate() == []
        Alert.objects.update(created_at=timezone.now() - timedelta(minutes=16))  # cooldown over
        assert len(alerts.evaluate()) == 1

    def test_email_recipients_setting(self, failing_queue, settings):
        settings.OVERSEER_ALERT_EMAILS = ["a@example.com", "b@example.com"]
        alerts.evaluate()
        assert mail.outbox[0].to == ["a@example.com", "b@example.com"]

    def test_slack_and_custom_notifiers(self, failing_queue, settings, monkeypatch):
        settings.OVERSEER_SLACK_WEBHOOK_URL = "https://hooks.slack.example/abc"
        calls = []
        settings.OVERSEER_NOTIFIERS = [
            "tests.test_alerts.custom_notifier",
            lambda a: calls.append(a),
        ]
        posted = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout):
            posted["url"] = request.full_url
            posted["body"] = request.data
            return FakeResponse()

        monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
        (alert,) = alerts.evaluate()
        assert posted["url"] == settings.OVERSEER_SLACK_WEBHOOK_URL
        assert b"failure_rate" in posted["body"]
        assert [alert] == CUSTOM and calls == [alert]

    def test_failing_notifier_is_logged_not_raised(self, failing_queue, settings, caplog):
        settings.OVERSEER_NOTIFIERS = ["tests.test_alerts.broken_notifier"]
        (alert,) = alerts.evaluate()
        assert "notifier" in caplog.text and "failed" in caplog.text
        assert Alert.objects.filter(pk=alert.pk).exists()

    def test_command(self, failing_queue, capsys):
        call_command("overseer_alerts")
        out = capsys.readouterr().out
        assert "failure_rate default" in out and "1 alert(s) fired." in out


CUSTOM = []


def custom_notifier(alert):
    CUSTOM.append(alert)


def broken_notifier(alert):
    raise RuntimeError("notifier down")


class TestJobFailedSignal:
    def test_sent_when_retries_exhausted(self, worker):
        from tests import tasks

        got = []
        signals.job_failed.connect(
            lambda sender, job, run, **kw: got.append((job, run)), weak=False
        )
        tasks.picky.enqueue("type")
        worker()
        assert len(got) == 1 and got[0][0].status == JobStatus.FAILED

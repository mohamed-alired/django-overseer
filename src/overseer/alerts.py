"""Threshold alerts over the recent window, with cooldowns and pluggable notifiers."""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from django.core.mail import mail_admins, send_mail
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.module_loading import import_string

from . import conf, signals
from .models import Alert, Run, RunStatus, Worker

logger = logging.getLogger("overseer")

MIN_SAMPLE = 5  # finished runs needed before a failure-rate alert can fire


@dataclass(frozen=True)
class Condition:
    kind: str
    key: str
    message: str
    value: float
    threshold: float


def detect(now=None) -> list[Condition]:
    now = now or timezone.now()
    window = timedelta(minutes=conf.get_setting("OVERSEER_ALERT_WINDOW_MINUTES"))
    since = now - window
    found: list[Condition] = []

    rate_threshold = conf.get_setting("OVERSEER_ALERT_FAILURE_RATE")
    finished = (
        Run.objects.filter(finished_at__gte=since)
        .values("job__queue_name")
        .annotate(
            total=Count("id"),
            bad=Count("id", filter=Q(status__in=[RunStatus.FAILED, RunStatus.ABANDONED])),
        )
    )
    for row in finished:
        if row["total"] >= MIN_SAMPLE:
            rate = row["bad"] / row["total"]
            if rate > rate_threshold:
                found.append(
                    Condition(
                        "failure_rate",
                        row["job__queue_name"],
                        f"{rate:.0%} of runs on queue {row['job__queue_name']!r} failed in the "
                        f"last {window.seconds // 60} min",
                        rate,
                        rate_threshold,
                    )
                )

    wait_threshold = conf.get_setting("OVERSEER_ALERT_WAIT_SECONDS")
    waits = (
        Run.objects.filter(started_at__gte=since, wait_ms__isnull=False)
        .values("job__queue_name")
        .annotate(worst=Max("wait_ms"))
    )
    for row in waits:
        worst = row["worst"] / 1000
        if worst > wait_threshold:
            found.append(
                Condition(
                    "queue_wait",
                    row["job__queue_name"],
                    f"A task on queue {row['job__queue_name']!r} waited {worst:.0f}s to start",
                    worst,
                    wait_threshold,
                )
            )

    depth_threshold = conf.get_setting("OVERSEER_ALERT_QUEUE_DEPTH")
    depths = (
        Run.objects.filter(status=RunStatus.READY)
        .filter(Q(run_after__isnull=True) | Q(run_after__lte=now))
        .values("job__queue_name")
        .annotate(depth=Count("id"))
    )
    for row in depths:
        if row["depth"] > depth_threshold:
            found.append(
                Condition(
                    "queue_depth",
                    row["job__queue_name"],
                    f"{row['depth']} tasks are waiting on queue {row['job__queue_name']!r}",
                    row["depth"],
                    depth_threshold,
                )
            )

    offline_after = timedelta(seconds=conf.get_setting("OVERSEER_WORKER_OFFLINE_AFTER"))
    stale = Worker.objects.filter(stopped_at__isnull=True, last_seen_at__lt=now - offline_after)
    for worker in stale:
        gone = (now - worker.last_seen_at).total_seconds()
        found.append(
            Condition(
                "worker_offline",
                worker.worker_id,
                f"Worker {worker.worker_id} has not been seen for {gone:.0f}s",
                gone,
                offline_after.total_seconds(),
            )
        )
    return found


def evaluate(now=None) -> list[Alert]:
    """Detect conditions, open new alerts (respecting the cooldown), resolve cleared ones."""
    now = now or timezone.now()
    cooldown = timedelta(minutes=conf.get_setting("OVERSEER_ALERT_COOLDOWN_MINUTES"))
    conditions = {(c.kind, c.key): c for c in detect(now)}
    open_alerts = {(a.kind, a.key): a for a in Alert.objects.filter(resolved_at__isnull=True)}
    fired = []
    for key, condition in conditions.items():
        if key in open_alerts:
            continue
        recent = Alert.objects.filter(
            kind=condition.kind, key=condition.key, created_at__gte=now - cooldown
        ).exists()
        if recent:
            continue
        alert = Alert.objects.create(
            kind=condition.kind,
            key=condition.key,
            message=condition.message,
            value=condition.value,
            threshold=condition.threshold,
            created_at=now,
        )
        fired.append(alert)
        notify(alert)
    for key, alert in open_alerts.items():
        if key not in conditions:
            alert.resolved_at = now
            alert.save(update_fields=["resolved_at"])
            signals.alert_resolved.send(sender=Alert, alert=alert)
    return fired


def _notify_email(alert: Alert):
    recipients = conf.get_setting("OVERSEER_ALERT_EMAILS")
    subject = f"[overseer] {alert.kind}: {alert.key}"
    if recipients:
        send_mail(subject, alert.message, None, recipients, fail_silently=False)
    else:
        mail_admins(subject, alert.message, fail_silently=False)


def _notify_slack(alert: Alert):
    url = conf.get_setting("OVERSEER_SLACK_WEBHOOK_URL")
    if not url:
        return
    body = json.dumps({"text": f":rotating_light: Overseer {alert.kind}: {alert.message}"})
    request = urllib.request.Request(
        url, data=body.encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def notify(alert: Alert) -> None:
    """Fan the alert out; a failing notifier is logged, never raised."""
    signals.alert_fired.send(sender=Alert, alert=alert)
    notifiers = [_notify_email, _notify_slack] + [
        import_string(p) if isinstance(p, str) else p
        for p in conf.get_setting("OVERSEER_NOTIFIERS")
    ]
    for notifier in notifiers:
        try:
            notifier(alert)
        except Exception:
            logger.exception("Overseer notifier %s failed for alert %s", notifier, alert.pk)

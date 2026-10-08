"""The HTML dashboard."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from . import conf, retry, stats
from .adapters.base import get_adapter
from .exceptions import AdapterUnsupported
from .models import Alert, Job, JobStatus, Run, RunStatus, Schedule
from .scheduling import scheduler as scheduling
from .scheduling.sync import sync_schedules

logger = logging.getLogger("overseer")

WINDOWS = (15, 60, 360, 1440)
WINDOW_LABELS = ((15, "15 minutes"), (60, "hour"), (360, "6 hours"), (1440, "24 hours"))


def text_param(params, name: str, max_length: int = 255) -> str | None:
    """A free-text query parameter, or None when empty. NUL bytes are dropped: PostgreSQL
    rejects them with an error, and no legitimate value contains one."""
    value = (params.get(name) or "").replace("\x00", "").strip()
    return value[:max_length] or None


def user_can_view(user) -> bool:
    if not user.is_authenticated or not user.is_staff:
        return False
    perm = conf.get_setting("OVERSEER_PERMISSION")
    return not perm or user.has_perm(perm)


def user_can_manage(user, codename: str) -> bool:
    return user_can_view(user) and user.has_perm(f"overseer.{codename}")


class AccessMixin(UserPassesTestMixin):
    def test_func(self):
        return user_can_view(self.request.user)


class Page(AccessMixin, TemplateView):
    section = ""

    def window_params(self):
        """Query parameters the window selector must carry along (filters, not paging)."""
        return [
            (key, value)
            for key in self.request.GET
            if key not in {"minutes", "page"}
            for value in self.request.GET.getlist(key)
        ]

    def window(self, default=60):
        try:
            minutes = int(self.request.GET.get("minutes", default))
        except ValueError:
            minutes = default
        return minutes if minutes in WINDOWS else default

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            section=self.section,
            refresh_seconds=conf.get_setting("OVERSEER_REFRESH_SECONDS"),
            can_manage_jobs=user_can_manage(self.request.user, "manage_jobs"),
            can_manage_schedules=user_can_manage(self.request.user, "manage_schedules"),
            nav_failed=Job.objects.filter(status=JobStatus.FAILED, dismissed=False).count(),
            nav_alerts=Alert.objects.filter(resolved_at__isnull=True).count(),
            windows=WINDOWS,
            window_choices=WINDOW_LABELS,
            extra_params=self.window_params(),
            now=timezone.now(),
        )
        return ctx


class OverviewView(Page):
    template_name = "overseer/overview.html"
    section = "overview"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        minutes = self.window(60)
        points = stats.timeseries(minutes)
        ctx.update(
            minutes=minutes,
            overview=stats.overview(minutes),
            queues=stats.queues(minutes),
            points=points,
            points_max=max((p["succeeded"] + p["failed"] for p in points), default=0),
            recent_failures=stats.failed_jobs().select_related("schedule")[:8],
            alerts=Alert.objects.filter(resolved_at__isnull=True)[:5],
        )
        return ctx


class QueuesView(Page):
    template_name = "overseer/queues.html"
    section = "queues"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        minutes = self.window(60)
        ctx.update(minutes=minutes, queues=stats.queues(minutes))
        return ctx


class TasksView(Page):
    template_name = "overseer/tasks.html"
    section = "tasks"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        minutes = self.window(1440)
        ctx.update(minutes=minutes, tasks=stats.tasks(minutes))
        return ctx


class JobsView(Page):
    template_name = "overseer/jobs.html"
    section = "jobs"
    per_page = 50

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        filters = {
            "status": text_param(g, "status"),
            "queue_name": text_param(g, "queue"),
            "task_path": text_param(g, "task"),
            "worker_id": text_param(g, "worker"),
            "search": text_param(g, "q"),
        }
        qs = stats.job_queryset(**filters).select_related("schedule")
        page = Paginator(qs, self.per_page).get_page(g.get("page"))
        ctx.update(
            page=page,
            filters=filters,
            statuses=JobStatus.choices,
            queue_names=stats.distinct_values("queue_name"),
            task_paths=stats.distinct_values("task_path"),
        )
        return ctx


class JobDetailView(Page):
    template_name = "overseer/job_detail.html"
    section = "jobs"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        job = get_object_or_404(Job.objects.select_related("schedule"), pk=kwargs["pk"])
        runs = stats.run_chain(job)
        active = next((r for r in runs if r.status in (RunStatus.READY, RunStatus.RUNNING)), None)
        try:
            adapter = get_adapter(job.backend)
            can_cancel = (
                active is not None and active.status == RunStatus.READY and adapter.supports_cancel
            )
        except Exception:
            can_cancel = False
        ctx.update(job=job, runs=runs, active_run=active, can_cancel=can_cancel)
        return ctx


class FailedView(Page):
    template_name = "overseer/failed.html"
    section = "failed"
    per_page = 50

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = stats.failed_jobs().select_related("schedule")
        page = Paginator(qs, self.per_page).get_page(self.request.GET.get("page"))
        ctx.update(page=page, rows=page.object_list, total=page.paginator.count)
        return ctx


class SchedulesView(Page):
    template_name = "overseer/schedules.html"
    section = "schedules"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(schedules=Schedule.objects.select_related("last_job").order_by("name"))
        return ctx


class WorkersView(Page):
    template_name = "overseer/workers.html"
    section = "workers"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(workers=stats.workers())
        return ctx


class MetricsView(Page):
    template_name = "overseer/metrics.html"
    section = "metrics"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        minutes = self.window(60)
        queue = text_param(self.request.GET, "queue")
        points = stats.timeseries(minutes, queue_name=queue)
        ctx.update(
            minutes=minutes,
            queue=queue,
            queue_names=stats.distinct_values("queue_name"),
            points=points,
            points_max=max((p["succeeded"] + p["failed"] for p in points), default=0),
            runtime_max=max((p["runtime_p95_ms"] for p in points), default=0),
            totals={
                "processed": sum(p["succeeded"] + p["failed"] for p in points),
                "failed": sum(p["failed"] for p in points),
                "enqueued": sum(p["enqueued"] for p in points),
            },
        )
        return ctx


class AlertsView(Page):
    template_name = "overseer/alerts.html"
    section = "alerts"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            open_alerts=Alert.objects.filter(resolved_at__isnull=True),
            resolved=Alert.objects.filter(resolved_at__isnull=False)[:50],
        )
        return ctx


# --------------------------------------------------------------------------- #
# Actions (POST only)
# --------------------------------------------------------------------------- #
class Action(AccessMixin, View):
    permission = "manage_jobs"
    http_method_names = ["post"]

    def test_func(self):
        return user_can_manage(self.request.user, self.permission)

    def get(self, request, *args, **kwargs):
        return HttpResponseNotAllowed(["POST"])

    def back(self, default="overseer:overview"):
        """Redirect to the posted ``next`` URL when it is on this site, else to ``default``."""
        target = self.request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            target = reverse(default)
        return redirect(target)


class JobRetryView(Action):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        try:
            run = retry.retry_job(job)
        except ValueError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            logger.exception("Retrying job %s failed", job.pk)
            messages.error(request, f"Retry failed: {type(exc).__name__}: {exc}")
        else:
            attempt = f" as attempt {run.attempt}" if run is not None else ""
            messages.success(request, f"Retry enqueued{attempt}.")
        return self.back("overseer:failed")


class JobCancelView(Action):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        run = job.runs.filter(status=RunStatus.READY).order_by("-attempt").first()
        if run is None:
            messages.error(request, "Only a job that is waiting to start can be cancelled.")
            return self.back("overseer:jobs")
        try:
            removed = get_adapter(job.backend).cancel(run)
        except AdapterUnsupported as exc:
            messages.error(request, str(exc))
            return self.back("overseer:jobs")
        except Exception as exc:  # the backend alias is gone from TASKS, for instance
            logger.exception("Cancelling job %s failed", job.pk)
            messages.error(request, f"Could not cancel: {type(exc).__name__}: {exc}")
            return self.back("overseer:jobs")
        if removed:
            now = timezone.now()
            Run.objects.filter(pk=run.pk).update(status=RunStatus.CANCELLED, finished_at=now)
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.CANCELLED, finished_at=now, next_retry_at=None
            )
            messages.success(request, "Job cancelled.")
        else:
            messages.error(request, "The task had already been picked up by a worker.")
        return self.back("overseer:jobs")


class JobDismissView(Action):
    def post(self, request, pk):
        if Job.objects.filter(pk=pk, status=JobStatus.FAILED).update(dismissed=True):
            messages.success(request, "Job dismissed from the failed list.")
        else:
            messages.error(request, "Only a failed job can be dismissed.")
        return self.back("overseer:failed")


class FailedRetryAllView(Action):
    def post(self, request):
        """Retry open failed jobs, oldest first, up to ``OVERSEER_RETRY_ALL_LIMIT`` per click.

        One job that cannot be retried never stops the others; it is counted and skipped.
        """
        limit = conf.get_setting("OVERSEER_RETRY_ALL_LIMIT")
        failed = Job.objects.filter(status=JobStatus.FAILED, dismissed=False)
        total = failed.count()
        retried = skipped = 0
        # Jobs that cannot be retried do not use up the budget, so a batch of unretryable
        # jobs at the head of the list never hides the ones behind it.
        for job in failed.order_by("created_at").iterator(chunk_size=200):
            if retried >= limit:
                break
            try:
                retry.retry_job(job)
            except Exception as exc:
                skipped += 1
                if not isinstance(exc, ValueError):
                    logger.exception("Retrying job %s failed", job.pk)
            else:
                retried += 1
        remaining = total - retried - skipped
        message = f"Retried {retried} job(s)."
        if skipped:
            message += f" {skipped} could not be retried."
        if remaining > 0:
            message += f" {remaining} more remain; click again to continue."
        (messages.success if retried or not skipped else messages.error)(request, message)
        return self.back("overseer:failed")


class FailedDismissAllView(Action):
    def post(self, request):
        count = Job.objects.filter(status=JobStatus.FAILED, dismissed=False).update(dismissed=True)
        messages.success(request, f"Dismissed {count} job(s).")
        return self.back("overseer:failed")


class ScheduleToggleView(Action):
    permission = "manage_schedules"

    def post(self, request, pk):
        schedule = get_object_or_404(Schedule, pk=pk)
        if not schedule.enabled:
            try:
                if schedule.next_run_at is None or schedule.next_run_at < timezone.now():
                    schedule.next_run_at = scheduling.compute_next_run(schedule)
            except ValueError as exc:
                messages.error(request, f"Schedule {schedule.name!r} cannot run: {exc}")
                return self.back("overseer:schedules")
            schedule.last_error = ""
            schedule.missing_from_code = False
        schedule.enabled = not schedule.enabled
        schedule.save(
            update_fields=[
                "enabled",
                "next_run_at",
                "last_error",
                "missing_from_code",
                "updated_at",
            ]
        )
        messages.success(request, f"Schedule {'enabled' if schedule.enabled else 'paused'}.")
        return self.back("overseer:schedules")


class ScheduleRunNowView(Action):
    permission = "manage_schedules"

    def post(self, request, pk):
        schedule = get_object_or_404(Schedule, pk=pk)
        try:
            job = scheduling.run_now(schedule)
        except Exception as exc:
            messages.error(request, f"Could not enqueue: {exc}")
        else:
            messages.success(request, f"Enqueued {job.task_name}.")
        return self.back("overseer:schedules")


class SchedulesSyncView(Action):
    permission = "manage_schedules"

    def post(self, request):
        report = sync_schedules()
        summary = ", ".join(f"{k} {len(v)}" for k, v in report.items() if v) or "nothing to do"
        messages.success(request, f"Schedules synced: {summary}.")
        return self.back("overseer:schedules")

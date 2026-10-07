"""The HTML dashboard."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Max
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from . import conf, retry, stats
from .adapters.base import get_adapter
from .exceptions import AdapterUnsupported
from .models import Alert, Job, JobStatus, Run, RunStatus, Schedule
from .scheduling import scheduler as scheduling
from .scheduling.sync import sync_schedules

WINDOWS = (15, 60, 360, 1440)


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
            "status": g.get("status") or None,
            "queue_name": g.get("queue") or None,
            "task_path": g.get("task") or None,
            "worker_id": g.get("worker") or None,
            "search": g.get("q") or None,
        }
        qs = stats.job_queryset(**filters).select_related("schedule")
        page = Paginator(qs, self.per_page).get_page(g.get("page"))
        ctx.update(
            page=page,
            filters=filters,
            statuses=JobStatus.choices,
            queue_names=sorted(Job.objects.values_list("queue_name", flat=True).distinct()),
            task_paths=sorted(Job.objects.values_list("task_path", flat=True).distinct()),
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
        qs = (
            Job.objects.filter(status=JobStatus.FAILED, dismissed=False)
            .annotate(last_run_at=Max("runs__finished_at"))
            .order_by("-last_run_at")
        )
        page = Paginator(qs, self.per_page).get_page(self.request.GET.get("page"))
        rows = []
        for job in page.object_list:
            last = job.runs.order_by("-attempt").first()
            rows.append({"job": job, "run": last})
        ctx.update(page=page, rows=rows, total=qs.count())
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
        queue = self.request.GET.get("queue") or None
        points = stats.timeseries(minutes, queue_name=queue)
        ctx.update(
            minutes=minutes,
            queue=queue,
            queue_names=sorted(Job.objects.values_list("queue_name", flat=True).distinct()),
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
        return redirect(self.request.POST.get("next") or reverse(default))


class JobRetryView(Action):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        try:
            run = retry.retry_job(job)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Retry enqueued as attempt {run.attempt}.")
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
        Job.objects.filter(pk=pk, status=JobStatus.FAILED).update(dismissed=True)
        messages.success(request, "Job dismissed from the failed list.")
        return self.back("overseer:failed")


class FailedRetryAllView(Action):
    def post(self, request):
        count = 0
        for job in Job.objects.filter(status=JobStatus.FAILED, dismissed=False):
            try:
                retry.retry_job(job)
                count += 1
            except ValueError:
                continue
        messages.success(request, f"Retried {count} job(s).")
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
        schedule.enabled = not schedule.enabled
        if schedule.enabled and (
            schedule.next_run_at is None or schedule.next_run_at < timezone.now()
        ):
            schedule.next_run_at = scheduling.compute_next_run(schedule)
        schedule.save(update_fields=["enabled", "next_run_at", "updated_at"])
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

from django.urls import path

from . import api, views

app_name = "overseer"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("queues/", views.QueuesView.as_view(), name="queues"),
    path("tasks/", views.TasksView.as_view(), name="tasks"),
    path("jobs/", views.JobsView.as_view(), name="jobs"),
    path("jobs/<uuid:pk>/", views.JobDetailView.as_view(), name="job"),
    path("jobs/<uuid:pk>/retry/", views.JobRetryView.as_view(), name="job-retry"),
    path("jobs/<uuid:pk>/cancel/", views.JobCancelView.as_view(), name="job-cancel"),
    path("jobs/<uuid:pk>/dismiss/", views.JobDismissView.as_view(), name="job-dismiss"),
    path("failed/", views.FailedView.as_view(), name="failed"),
    path("failed/retry-all/", views.FailedRetryAllView.as_view(), name="failed-retry-all"),
    path("failed/dismiss-all/", views.FailedDismissAllView.as_view(), name="failed-dismiss-all"),
    path("schedules/", views.SchedulesView.as_view(), name="schedules"),
    path("schedules/sync/", views.SchedulesSyncView.as_view(), name="schedules-sync"),
    path("schedules/<int:pk>/toggle/", views.ScheduleToggleView.as_view(), name="schedule-toggle"),
    path("schedules/<int:pk>/run/", views.ScheduleRunNowView.as_view(), name="schedule-run"),
    path("workers/", views.WorkersView.as_view(), name="workers"),
    path("metrics/", views.MetricsView.as_view(), name="metrics"),
    path("alerts/", views.AlertsView.as_view(), name="alerts"),
    # JSON API
    path("api/overview/", api.overview, name="api-overview"),
    path("api/queues/", api.queues, name="api-queues"),
    path("api/tasks/", api.tasks, name="api-tasks"),
    path("api/jobs/", api.jobs, name="api-jobs"),
    # str, not uuid: the API answers malformed or upper-case ids with JSON, not an HTML 404.
    path("api/jobs/<str:pk>/", api.job, name="api-job"),
    path("api/workers/", api.workers, name="api-workers"),
    path("api/schedules/", api.schedules, name="api-schedules"),
    path("api/metrics/", api.metrics, name="api-metrics"),
    path("api/alerts/", api.alerts, name="api-alerts"),
    path("api/health/", api.health, name="api-health"),
]

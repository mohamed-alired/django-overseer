import uuid

from django.db import models
from django.utils import timezone
from django.utils.module_loading import import_string


class JobStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


class RunStatus(models.TextChoices):
    READY = "READY", "Ready"
    RUNNING = "RUNNING", "Running"
    SUCCESSFUL = "SUCCESSFUL", "Successful"
    FAILED = "FAILED", "Failed"
    ABANDONED = "ABANDONED", "Abandoned"
    CANCELLED = "CANCELLED", "Cancelled"


class JobSource(models.TextChoices):
    ENQUEUE = "enqueue", "Enqueued"
    RETRY = "retry", "Retry"
    SCHEDULE = "schedule", "Schedule"
    MANUAL = "manual", "Manual"


class Schedule(models.Model):
    """A recurring enqueue, declared in code with ``overseer.schedule`` or created in the DB."""

    name = models.CharField(max_length=200, unique=True)
    task_path = models.CharField(max_length=255)
    cron = models.CharField(max_length=100, blank=True, help_text="Five-field cron expression")
    interval_seconds = models.PositiveIntegerField(null=True, blank=True)
    args = models.JSONField(default=list, blank=True)
    kwargs = models.JSONField(default=dict, blank=True)
    queue_name = models.CharField(max_length=128, blank=True)
    priority = models.IntegerField(null=True, blank=True)
    backend = models.CharField(max_length=64, blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    enabled = models.BooleanField(default=True)
    declared_in_code = models.BooleanField(default=False)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_job = models.ForeignKey(
        "Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    runs_count = models.PositiveIntegerField(default=0)
    # Why the schedule last failed to enqueue, or why it was disabled ("" when healthy).
    last_error = models.TextField(blank=True)
    # Disabled by sync because its declaration disappeared; re-enabled if it comes back.
    missing_from_code = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        permissions = [("manage_schedules", "Can pause, resume and trigger schedules")]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError

        from .scheduling.validation import check_schedule

        try:
            check_schedule(self)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    @property
    def spec(self):
        return self.cron or f"every {self.interval_seconds}s"


class Job(models.Model):
    """One logical unit of work: a task enqueued once, possibly executed several times."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_path = models.CharField(max_length=255, db_index=True)
    task_name = models.CharField(max_length=255)
    backend = models.CharField(max_length=64)
    queue_name = models.CharField(max_length=128, db_index=True)
    priority = models.IntegerField(default=0)
    args = models.JSONField(default=list, blank=True)
    kwargs = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING, db_index=True
    )
    source = models.CharField(max_length=16, choices=JobSource.choices, default=JobSource.ENQUEUE)
    attempts = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    unique_key = models.CharField(max_length=255, blank=True, db_index=True)
    tags = models.JSONField(default=list, blank=True)
    schedule = models.ForeignKey(
        Schedule, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    dismissed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["queue_name", "status"]),
        ]
        constraints = [
            # At most one active job per unique key, enforced by the database so two
            # processes enqueueing the same unique task at once cannot both win.
            models.UniqueConstraint(
                fields=["unique_key"],
                condition=~models.Q(unique_key="")
                & models.Q(status__in=[JobStatus.PENDING, JobStatus.RUNNING]),
                name="overseer_job_unique_active",
            )
        ]
        permissions = [
            ("view_dashboard", "Can view the Overseer dashboard"),
            ("manage_jobs", "Can retry, cancel and dismiss jobs"),
        ]

    def __str__(self):
        return f"{self.task_name} ({str(self.id)[:8]})"

    @property
    def is_finished(self):
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def can_retry(self):
        return self.status in {JobStatus.FAILED, JobStatus.CANCELLED}

    def get_task(self):
        """Import the Django ``Task`` this job runs, with the job's queue, priority and backend."""
        task = import_string(self.task_path)
        return task.using(queue_name=self.queue_name, priority=self.priority, backend=self.backend)


class RunQuerySet(models.QuerySet):
    def finished(self):
        return self.filter(status__in=[RunStatus.SUCCESSFUL, RunStatus.FAILED])

    def active(self):
        return self.filter(status__in=[RunStatus.READY, RunStatus.RUNNING])


class Run(models.Model):
    """One execution attempt, keyed by the backend's result id."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="runs")
    result_id = models.CharField(max_length=64, unique=True)
    backend = models.CharField(max_length=64)
    attempt = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16, choices=RunStatus.choices, default=RunStatus.READY, db_index=True
    )
    enqueued_at = models.DateTimeField(default=timezone.now, db_index=True)
    run_after = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    worker_id = models.CharField(max_length=255, blank=True, db_index=True)
    exception_class = models.CharField(max_length=255, blank=True)
    traceback = models.TextField(blank=True)
    return_value = models.JSONField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    wait_ms = models.PositiveBigIntegerField(null=True, blank=True)
    retry_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="retries"
    )

    objects = RunQuerySet.as_manager()

    class Meta:
        ordering = ["-enqueued_at"]
        indexes = [
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["job", "attempt"]),
        ]

    def __str__(self):
        return f"{self.job.task_name} attempt {self.attempt} [{self.status}]"

    @property
    def is_finished(self):
        return self.status in {
            RunStatus.SUCCESSFUL,
            RunStatus.FAILED,
            RunStatus.ABANDONED,
            RunStatus.CANCELLED,
        }


class Worker(models.Model):
    """A worker process, as seen through signals (phase 1) or heartbeats (``overseer_worker``)."""

    worker_id = models.CharField(max_length=255, unique=True)
    hostname = models.CharField(max_length=255, blank=True)
    pid = models.PositiveIntegerField(null=True, blank=True)
    backend = models.CharField(max_length=64, blank=True)
    queues = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    current_run = models.ForeignKey(
        Run, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    tasks_processed = models.PositiveIntegerField(default=0)
    tasks_failed = models.PositiveIntegerField(default=0)
    # Set by ``overseer_worker``. Workers without heartbeats (a plain ``db_worker``) are only
    # seen when they run a task, so they are never reported offline.
    heartbeat_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        return self.worker_id

    @property
    def has_heartbeat(self) -> bool:
        return self.heartbeat_seconds is not None

    def offline_after(self) -> float:
        """Seconds of silence after which a heartbeat worker counts as offline."""
        from . import conf

        configured = conf.get_setting("OVERSEER_WORKER_OFFLINE_AFTER")
        return max(configured, 3 * (self.heartbeat_seconds or 0))

    def is_online(self, now=None) -> bool | None:
        """True/False for heartbeat workers; None when it cannot be known."""
        if self.stopped_at is not None:
            return False
        if not self.has_heartbeat:
            return None
        now = now or timezone.now()
        return (now - self.last_seen_at).total_seconds() < self.offline_after()

    def is_silent(self, now=None) -> bool:
        """A worker without heartbeats that has not run anything for a long time."""
        from . import conf

        if self.has_heartbeat or self.stopped_at is not None:
            return False
        now = now or timezone.now()
        silent_after = conf.get_setting("OVERSEER_SILENT_WORKER_AFTER")
        return (now - self.last_seen_at).total_seconds() >= silent_after


class MetricBucket(models.Model):
    """Per-minute rollup per queue and task (``task_path == ""`` means all tasks)."""

    bucket_start = models.DateTimeField(db_index=True)
    queue_name = models.CharField(max_length=128)
    task_path = models.CharField(max_length=255, blank=True)
    enqueued = models.PositiveIntegerField(default=0)
    started = models.PositiveIntegerField(default=0)
    succeeded = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    abandoned = models.PositiveIntegerField(default=0)
    runtime_ms_sum = models.PositiveBigIntegerField(default=0)
    runtime_ms_p50 = models.PositiveBigIntegerField(default=0)
    runtime_ms_p95 = models.PositiveBigIntegerField(default=0)
    runtime_ms_max = models.PositiveBigIntegerField(default=0)
    wait_ms_sum = models.PositiveBigIntegerField(default=0)
    wait_ms_max = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["-bucket_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["bucket_start", "queue_name", "task_path"], name="overseer_bucket_unique"
            )
        ]

    def __str__(self):
        return f"{self.bucket_start:%Y-%m-%d %H:%M} {self.queue_name} {self.task_path or '*'}"

    @property
    def finished(self):
        return self.succeeded + self.failed + self.abandoned

    @property
    def failure_rate(self):
        total = self.finished
        return (self.failed + self.abandoned) / total if total else 0.0


class Alert(models.Model):
    """An alert that fired; kept for the dashboard and for cooldowns."""

    kind = models.CharField(max_length=32, db_index=True)
    key = models.CharField(max_length=255, blank=True)
    message = models.TextField()
    value = models.FloatField(null=True, blank=True)
    threshold = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind}:{self.key}"

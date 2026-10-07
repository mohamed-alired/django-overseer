"""Signals emitted by Overseer. All carry ``sender=<model class>`` plus the kwargs listed."""

from django.dispatch import Signal

#: An alert condition was detected. kwargs: ``alert`` (an ``Alert`` row).
alert_fired = Signal()
#: An open alert's condition cleared. kwargs: ``alert``.
alert_resolved = Signal()
#: A job exhausted its retries (or had none) and is now FAILED. kwargs: ``job``, ``run``.
job_failed = Signal()

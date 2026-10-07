"""Adapter for the reference database backend (``django-tasks-db``)."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import RunStatus
from .base import BaseAdapter


class DatabaseAdapter(BaseAdapter):
    supports_cancel = True
    supports_reset = True

    def _model(self):
        from django_tasks_db.models import DBTaskResult

        return DBTaskResult

    def cancel(self, run) -> bool:
        DBTaskResult = self._model()
        with transaction.atomic():
            deleted, _ = (
                DBTaskResult.objects.select_for_update(skip_locked=True)
                .filter(id=run.result_id, status="READY")
                .delete()
            )
        return bool(deleted)

    def reset(self, run) -> bool:
        DBTaskResult = self._model()
        updated = DBTaskResult.objects.filter(id=run.result_id, status="RUNNING").update(
            status="FAILED",
            finished_at=timezone.now(),
            exception_class_path="overseer.exceptions.RunAbandoned",
            traceback="Marked abandoned by Overseer: the attempt exceeded its timeout.",
        )
        return bool(updated)

    def queue_depth(self, queue_name=None):
        DBTaskResult = self._model()
        qs = DBTaskResult.objects.filter(status="READY", backend_name=self.alias)
        if queue_name:
            qs = qs.filter(queue_name=queue_name)
        return qs.count()


__all__ = ["DatabaseAdapter", "RunStatus"]
